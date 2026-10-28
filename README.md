# Invoice Processing → Excel

Invoices arrive by **upload** or **Gmail** (each user connects their own inbox through
Composio). AI reads each document, it is checked, and correct invoices are written to an
**Excel workbook** automatically. Anything doubtful waits for a user to approve.

```
Upload API ─┐
            ├─> ingest (hash dedupe) ─> queue ─> AI: classify + extract ─> checks ─> decide
Gmail ──────┘   (Composio trigger)                                                    │
                                     ┌────────────────────────────────────────────────┤
                                     ▼                       ▼                        ▼
                              APPROVED ("auto")        NEEDS_REVIEW          NOT_INVOICE / DUPLICATE
                                     │              user corrects, approves
                                     ▼                       │
                    output/invoices FY2026-27.xlsx  <──────┘
```

**Written to Excel without approval** when the document is a tax invoice and every
check passes: AI confidence ≥ 0.85, valid supplier GSTIN, billed to `COMPANY_GSTIN`,
CGST+SGST vs IGST matches the place of supply, each line's qty × rate, the totals and
the tax add up, the date is valid, it isn't a duplicate, and the total is at most
`AUTO_APPROVE_LIMIT` (₹50,000). Otherwise it goes to `NEEDS_REVIEW` with the reasons.

## How a document is read

1. **AI reading** (`OPENAI_MODEL`; scans and photos `OPENAI_SCAN_MODEL`): classify and extract every field,
   including the total written in words.
2. **E-invoice QR**: when the document shows an IRN, its signed QR code is read and its values (supplier and
   buyer GSTIN, invoice number, date, total, IRN, document type) replace the AI's reading; the check
   `einvoice_qr` lists what was corrected. Put NIC's public key/certificate (`*.pem`, from the e-invoice
   portal) in `EINVOICE_KEYS_DIR` to verify the signature: a QR with a wrong signature is then not used and
   the invoice goes to review. `EINVOICE_QR=false` turns this off. Installing `zxing-cpp`
   (`pip install zxing-cpp`) makes the QR reading more reliable on poor scans; OpenCV is used otherwise.
3. **Second reading**: if a check fails that a misreading can explain (totals that don't add up, a GSTIN with
   a wrong check digit, the total in words differing from the figure, an unreadable date, low confidence),
   the document is read once more by `AI_SECOND_PASS_MODEL` (gpt-4o), told exactly what failed, plus the
   corrections reviewers made before on that supplier's documents. The reading with fewer problems is kept.
   Business rules (over the limit, credit notes, foreign currency) never trigger it. `AI_SECOND_PASS=false`
   turns it off.
4. **Checks and decision** as above, plus: the total in words must match the figure, and an invoice number
   that differs from an earlier one from the same supplier only by look-alike characters (O/0, I/1, S/5,
   B/8) goes to review as a possible duplicate.

In the demo's Fields tab, the values to check first are outlined: fields the AI was unsure of and fields a
failed check points at (hover for the reason).

## Setup (Windows, no Docker)

Needs Python 3.11+ and a local PostgreSQL.

    pip install -r requirements.txt pytest
    python -m scripts.setup_db

`setup_db` asks for your `postgres` superuser password, then creates the `ap` user
(password `ap`), the `ap` and `ap_test` databases, `.env` and the tables. Safe to run
again. If it reports tables from the previous version, run it with `--reset`.

In `.env` set `OPENAI_API_KEY`, `COMPOSIO_API_KEY` (dashboard.composio.dev → Platform)
and `COMPANY_GSTIN`.

## Run

One command starts everything:

    uvicorn app.api:app --reload

That runs the API (docs at http://localhost:8000/docs) and, in the same process, the
**worker** (AI processing + Excel writing) and the **Gmail listener** (only when
`COMPOSIO_API_KEY` is set). On Ctrl+C or a reload they stop cleanly: the worker
finishes the invoice it is reading (up to 30 s) and the listener disconnects.

Optional, for heavier loads: run extra workers with `python -m app.worker` (they share
the queue safely). To run the worker or listener only as separate processes, set
`RUN_WORKER=false` or `RUN_GMAIL_LISTENER=false` in `.env` and start them with
`python -m app.worker` / `python -m app.gmail_listener`.

## Use it (cmd examples)

There is no login yet: the acting user is sent in the `X-User-Id` header.

    curl -X POST localhost:8000/api/v1/users -H "Content-Type: application/json" -d "{\"name\":\"Priya\",\"email\":\"priya@company.com\"}"

    curl -H "X-User-Id: 1" -F "file=@invoice.pdf" localhost:8000/api/v1/invoices/upload
    curl localhost:8000/api/v1/invoices/1
    curl localhost:8000/api/v1/invoices?status=NEEDS_REVIEW

Review a doubtful invoice: fix fields (checks run again), then approve or reject:

    curl -X PATCH localhost:8000/api/v1/invoices/1 -H "X-User-Id: 1" -H "Content-Type: application/json" -d "{\"grand_total\": 25370, \"note\": \"misread\"}"
    curl -X POST localhost:8000/api/v1/invoices/1/approve -H "X-User-Id: 1"
    curl -X POST localhost:8000/api/v1/invoices/1/reject  -H "X-User-Id: 1"
    curl -X POST localhost:8000/api/v1/invoices/1/reprocess -H "X-User-Id: 1"   # run the AI again

Download the workbook: `GET /api/v1/excel` (this financial year's; `?year=2025-26` for another, `GET /api/v1/excel/workbooks` lists them), or open `output/invoices FY2026-27.xlsx`.

### Connect a user's Gmail

    curl -X POST localhost:8000/api/v1/users/1/gmail/connect     # open the returned connect_url, allow access
    curl -X POST "localhost:8000/api/v1/users/1/gmail/sync?days=7"   # optional: pull the last 7 days now

Activation is automatic: after the user allows access, Composio returns the browser to
`/api/v1/users/{id}/gmail/callback`, which turns on new-email events and shows "Gmail
connected". If that page can't be reached, a background check activates it anyway
within a few seconds. `POST /api/v1/users/{id}/gmail/activate` is only needed to retry.
If the server isn't at http://localhost:8000, set `PUBLIC_BASE_URL` in `.env`.

From then on, while the server is running, every new email matching `GMAIL_QUERY`
(default `in:anywhere has:attachment -in:sent -in:drafts -in:trash`: Inbox, Spam and filtered/archived mail; not sent mail, drafts or trash) is picked up. The same file sent again in another email is noted on the existing record ("received again") instead of creating a second one. Composio checks each inbox every
`GMAIL_POLL_MINUTES` (default 1). Each PDF/image attachment becomes its own invoice
job; attachments that aren't invoices (terms, quotations…) end as `NOT_INVOICE`.
The listener also catches up on emails that arrived while it was stopped.

### What the Gmail automation does

- **Email log**: every email seen is recorded with what happened to each attachment (new document,
  same file as an earlier one, unpacked, not supported, too large for `MAX_UPLOAD_MB`, failed). `GET /api/v1/emails`, and the
  "Email log" table on the demo's Users & Gmail screen.
- **Daily check**: once a day the last `GMAIL_RECONCILE_DAYS` (7) days of Gmail are compared with the
  log; anything missed or failed is processed. Run it now with `POST /api/v1/users/{id}/gmail/reconcile`.
- **File types**: PDF, images, Excel (.xlsx/.xlsm), CSV and Word (.docx) are read; .zip files and
  forwarded emails (.eml) are unpacked and each document inside is processed; an invoice written in
  the email body (no attachment) is turned into a PDF.
- **Download links**: an invoice sent as a link (supplier portals) is downloaded when the link's domain
  is in `GMAIL_LINK_DOMAINS` (e.g. `zoho.in`; empty = off). https only; redirects are followed only
  within those domains; a portal page that needs a login is logged as "not a document".
- **Protected PDFs**: tried with `PDF_PASSWORDS` and passwords entered before; otherwise the document
  waits as `NEEDS_PASSWORD` until someone enters it (`POST /api/v1/invoices/{id}/password`, or the
  Unlock box in the demo). The password is remembered for later PDFs.
- **Email hints**: subject, sender and the start of the body go to the AI as context, plus the supplier
  that earlier approved invoices from the same sender came from.
- **Gmail labels**: emails get `AP/Processed`, `AP/Needs review` or `AP/Not invoice` in Gmail, updated
  within a minute when the invoice's status changes (`GMAIL_APPLY_LABELS=false` to turn off).
- **Network trouble**: Composio calls and downloads are retried; after the internet comes back the
  listener reconnects and syncs at once; a sync that failed is retried every minute.
- **Expired Gmail access**: every 10 minutes (and right after a sync fails with an access error) each
  inbox's connection is checked at Composio. An expired or revoked one shows "Gmail needs to be
  reconnected" in the app and "Reconnect Gmail" in the email report, and is skipped by the sync until
  the user connects again (then the emails from the outage are fetched). `POST /api/v1/users/{id}/gmail/check`
  checks now. With `GMAIL_HEALTH_ALERTS=true` the user also gets one email about it, sent from
  another inbox that still works.
- **Webhook delivery (production)**: with `GMAIL_DELIVERY=webhook` and `COMPOSIO_WEBHOOK_SECRET`, Composio
  sends new-email events to `PUBLIC_BASE_URL/webhooks/composio` (set that URL as a webhook subscription
  in the Composio dashboard; the server must be reachable over HTTPS). Every request's signature and
  timestamp are verified; anything else gets 401. The 5-minute safety sync, labels and daily check run
  as before. The default, `websocket`, needs no public address (development, one PC).
- **Nothing skipped**: a sync takes up to 500 emails; if more are waiting, the next sync carries on from
  there. Each sync overlaps the previous one by 10 minutes (Gmail search can lag). An email that keeps
  failing is retried with every sync for a day, then by the daily check; reconnecting Gmail after an
  outage fetches the emails that arrived meanwhile.
- **Staying alive**: the realtime connection is watched (replaced if it is stuck reconnecting for
  90 s) and refreshed every 30 min; a safety sync runs every 5 min, so an email is never more than
  a few minutes late even if live events stop. Check it with `GET /api/v1/gmail/status` (shown on
  the demo's Gmail screen): connected or not, last event, last sync, reconnects, last error.

## AI assistant

`POST /api/v1/assistant/chat` (and the **Assistant** screen in the demo) answers questions about the
invoices in plain language, e.g. "what needs my attention?", "GST by month", "total spend per supplier",
"read the Excel sheet", "export last month's approved invoices". The model (`ASSISTANT_MODEL`, default
gpt-4o) answers only from tools in `app/assistant_tools.py`:

| Tool | What it reads or does |
|---|---|
| search_invoices / get_invoice | invoices with any filter; one invoice in full (values, items, checks, history, email) |
| invoice_statistics / supplier_summary | totals by status, supplier, month, source, type, GST rate (duplicates left out) |
| read_excel | the workbook on disk, with column totals |
| list_emails / gmail_overview | the email log and whether the Gmail automation is running |
| needs_attention | review queue, passwords needed, failures, not yet in Excel, unusually large invoices |
| export_invoices | creates an .xlsx/.csv of any filtered set, downloadable from the chat |
| propose_action | suggests approve / reject / run again; **only a person's click does it** |

The assistant can't change data by itself. Text inside documents and emails is treated as data, never
as instructions. Exports are written to `EXPORT_DIR` (`output/exports`).

**Saved chats.** Conversations are stored in the database (`chat_sessions`, `chat_messages`), per user:
each user sees only their own. The demo shows them in a sidebar (open, rename, delete); a chat is named
after its first question, and a suggested action stays marked as done when the chat is reopened.

| Endpoint | |
|---|---|
| `GET / POST /api/v1/assistant/sessions` | list your chats / start one |
| `GET / PATCH / DELETE /api/v1/assistant/sessions/{id}` | open (with messages) / rename / delete |
| `POST /api/v1/assistant/sessions/{id}/stream` | ask; the answer streams as Server-Sent Events: `tool` (what it is looking up), `delta` (the next piece of text), then `saved` (the stored message) or `error` |
| `POST /api/v1/assistant/sessions/{id}/messages` | ask and get the whole answer at once |
| `POST /api/v1/assistant/messages/{id}/actions/{n}/done` | remember that a suggested action was carried out |

`POST /api/v1/assistant/chat` still works without saving (the caller sends the conversation).

## Summary report by email

A summary of the period: documents received, approved (automatically or by whom) with totals, what is
waiting for review (oldest first, with the reason), failures, top suppliers, and the Gmail inboxes. The
numbers come from the database, not the AI. It is sent from a connected Gmail account (Composio
`GMAIL_SEND_EMAIL`), so at least one user must have Gmail connected.

- **On a schedule:** off by default. Set `REPORT_SCHEDULE=daily|weekly|monthly`, `REPORT_TIME` (local,
  09:00) and `REPORT_WEEKDAY` (weekly), then restart. Daily covers yesterday, weekly the 7 days before, monthly
  the previous month. Each report is sent once (recorded in `report_runs`); if the server was down at the
  send time it goes out when it is back the same day; a failed one is retried after 30 minutes.
- **By hand:** the demo's **Reports** screen shows a preview and has **Send now** (last day / 7 / 30 days);
  or `GET /api/v1/reports/preview?period=week` and `POST /api/v1/reports/send` `{"period": "week",
  "recipients": [...]}`. It is sent from your Gmail if connected, else from the usual sender.
  Recipients must be users of the app or in `REPORT_RECIPIENTS` (it may go out from a colleague's mailbox).
- Recipients: `REPORT_RECIPIENTS` (comma-separated), else every user's email. Sender:
  `REPORT_SENDER_USER_ID`, else the first user with Gmail connected. `REPORT_APP_URL` (the frontend's
  address, e.g. `http://localhost:5173`) makes invoice numbers in the email clickable.
- `GET /api/v1/reports`: the schedule, next send time, sender, recipients and the last reports sent.

## Excel workbook

One workbook per financial year (April–March, by invoice date): `output/invoices FY2026-27.xlsx`, next to
`EXCEL_PATH`. Set `EXCEL_PER_FINANCIAL_YEAR=false` for the single `EXCEL_PATH` workbook. Each has:

- **Invoices**: one row per invoice: supplier, GSTINs, number, date, taxable value,
  CGST/SGST/IGST, round off, total, approved by (`auto` or the user), source, sender, user, file.
- **Line Items**: one row per item, linked by Invoice ID.

If the file is open in Excel when an invoice is approved, it can't be saved: the
invoice stays `APPROVED` and is written as soon as the file is closed (the worker
retries every few seconds). Only that year's invoices wait; other years are written. Rows are never written twice.

## Many users at once

One server process (`uvicorn app.api:app`) serves many people at the same time:

- **AI chat is async**: while the assistant waits for OpenAI, it holds no server thread and no database
  connection, so many people can chat at once.
- **Documents are read in parallel**: `WORKER_CONCURRENCY` (4) documents at a time. The Postgres queue
  gives each document to exactly one worker, also across extra worker processes (`python -m app.worker`,
  even on other PCs sharing the database).
- **Gmail**: inboxes are synced `GMAIL_CONCURRENCY` (4) at a time, never two syncs of the same inbox at once
  ("Sync now" during the background sync waits for it); new-email events are handled in parallel, each email
  once.
- **Database connections** are only used for short moments (never while waiting for the AI or Gmail):
  `DB_POOL_SIZE` (10) + `DB_MAX_OVERFLOW` (30) serve many users. Other blocking work (files, Excel, Gmail)
  runs in up to `API_THREADS` (100) threads.
- **Safe under load**: the PDF library (MuPDF) can't be used by two threads at once, so it is used one
  call at a time; approving/rejecting the same invoice at once is refused for the second person (409); the
  Excel download is never cut off by a worker updating the workbook.

Measured on one Windows PC with the AI simulated at real speed (1.5 s per document, 3 s per chat answer):
100 people at once, each uploading 3 invoices, browsing, chatting and downloading the Excel: 0 errors,
all 300 invoices processed and exported, chat answers starting after a median of 2.6 s. Before these
changes, 30 people at once already caused failed requests (database connections ran out).

Run only one API process (no `--workers N`): the Gmail listener and the report schedule must run once.
Add worker processes instead if documents queue up.

## Logs

Every step logs one line per invoice (queued, AI result, checks, decision, Excel,
reviews) with a timestamp. Set `LOG_LEVEL=DEBUG` in `.env` to also see each check's
result, every Composio call with its timing, and email attachment details.

## Statuses

| Status | Meaning |
|---|---|
| RECEIVED / PROCESSING | queued / being read by the AI |
| NEEDS_REVIEW | a check failed or amount over the limit: correct, approve or reject |
| APPROVED | approved (auto or by a user), waiting to be written to Excel |
| EXPORTED | in the workbook |
| DUPLICATE | same supplier, invoice number, date and amount as an earlier invoice |
| NOT_INVOICE | not a tax invoice (terms, quotation, PO, statement…) |
| REJECTED | rejected by a user |
| FAILED | technical failure after 3 attempts (e.g. OpenAI key invalid); can be reprocessed |

## Accuracy (golden set)

`golden/` holds real documents with verified correct values. `python -m scripts.evaluate` reads them
with the AI and reports accuracy per field, decisions, wrong auto-approvals, time and tokens
(`reports/`). Run it after any change to the prompt, model or rules; add reviewed invoices with
`python -m scripts.golden_add <invoice id>`. See [golden/README.md](golden/README.md).

## Testing with generated invoices (several users at once)

Random fake invoices (random GSTINs, no buyer GSTIN, lakhs of rupees) can never be approved
automatically: the checks rightly fail on them. To test the decisions, use invoices that follow the
rules, each with a known expected outcome:

    python -m scripts.make_test_invoices                # one of each scenario -> test_invoices/<time>/
    python -m scripts.simulate_users --users 3          # 3 users upload at the same time, results compared

`make_test_invoices` writes PDFs (with a real text layer) plus `manifest.json` with the expected status
of each:
- clean same-state and other-state invoices under the limit → EXPORTED
- over the limit, a wrong total, a bad GSTIN check digit, billed to another company, a credit note → NEEDS_REVIEW
- the same invoice sent again → DUPLICATE
- a terms-and-conditions letter → NOT_INVOICE

The invoices are billed to your `COMPANY_GSTIN`; supplier GSTINs get a valid check digit (`make_gstin`, the
reference for an outside generator such as an n8n workflow).

`simulate_users` needs the API running and calls the real AI (1–2 OpenAI calls per document). It:
1. creates the users "Test user 1..N";
2. uploads everyone's set at the same moment;
3. uploads one file from two users (it must stay one record);
4. waits for the worker, prints right/wrong per scenario, and writes `reports/sim-<time>.json`.

Run it against a test database if you don't want test data in your real one.
`tests/test_generated_invoices.py` runs the same check without OpenAI (a perfect stand-in reader), so the
expected outcomes and the rules are known to agree.

**OpenAI account problems** (no credits, invalid key) don't fail documents. They wait in the queue
(attempts aren't used up), the workers pause 5 minutes between tries, and the demo shows
"Documents aren't being read" with the reason (`GET /api/v1/system/status`). Add credits and they are
read automatically.

## Tests

    pytest

Uses the `ap_test` database (emptied on every run, never point it at `ap`). OpenAI and
Composio are faked, so no keys are used. Other database:
`set TEST_DATABASE_URL=postgresql+psycopg://...` (cmd) or `$env:TEST_DATABASE_URL = "..."` (PowerShell).

## Layout

    app/api.py               REST API (/api/v1)
    app/ingest.py            one entry point for every channel (hash, store, queue)
    app/gmail.py             Composio: connect, trigger, read emails, download attachments
    app/gmail_listener.py    receives Composio events (websocket, or webhook in production)
    app/gmail_health.py      expired/revoked Gmail access: check, show, alert
    app/background.py        starts/stops the worker and Gmail listener with the API
    app/worker.py            Postgres queue (SKIP LOCKED) + Excel retries
    app/pipeline/reading.py  reading a document: AI, e-invoice QR, second reading (worker + evaluation)
    app/pipeline/extract.py  AI step (Agno + OpenAI): classify and extract
    app/pipeline/einvoice.py e-invoice QR: find, decode, verify signature, correct the reading
    app/pipeline/words.py    amounts in words (lakh/crore) -> numbers
    app/pipeline/learning.py reviewers' past corrections per supplier, as hints for the AI
    app/pipeline/validate.py GSTIN checksum, GST type, arithmetic, dates, duplicates
    app/pipeline/decide.py   auto-approve / review / duplicate / not an invoice
    app/pipeline/evaluate.py stores values, runs checks, sets status (AI and corrections)
    app/excel.py             workbook writer
    app/assistant.py         AI chat (Agno tools in app/assistant_tools.py), streamed answers
    app/chat_store.py        saved chats (chat_sessions / chat_messages)
    app/reports.py           summary report by email + its schedule
    legacy_v1/               previous version (PO matching, Markdown ledgers); safe to delete
"# Invoice-agent" 
