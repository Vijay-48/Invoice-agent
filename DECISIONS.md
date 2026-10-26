# Decision log

Every meaningful decision made while building this system: what was chosen, why, what else was
considered, and what it costs. Newest first within each section. When a decision is changed later,
the old entry stays and gets a "Superseded by" note, so the reasoning history is never lost.

Format of each entry:

- **Decision**: what we do.
- **Why**: the reason.
- **Instead of**: the alternatives considered.
- **Trade-off**: what we give up or risk.
- **Where**: the files that carry it out.

Dates are when the decision was made (approximate for the first days of the project, marked ≈).

---

## 1. Product scope

### D-01 Excel workbook as the output, not Tally (≈ 2026-10-06)

- **Decision**: Approved invoices are appended to one workbook, `output/invoices.xlsx`, with two sheets:
  "Invoices" (one row per document) and "Line Items" (one row per line).
- **Why**: The business asked for Excel now and Tally later. A workbook can be opened by anyone, needs
  no ERP access, and is easy to check by hand.
- **Instead of**: Tally XML import, or Markdown ledgers per user (the previous version, now in `legacy_v1/`).
- **Trade-off**: A file is not a database. Concurrent writers need a lock (D-24), the file can be open in
  Excel (writes wait), and edits made in Excel never come back to the system. The roadmap (Phase 5)
  plans "database as the register, Excel generated from it".
- **Where**: `app/excel.py`.

### D-02 Auto-approve only when every check passes and the total is at most ₹50,000 (≈ 2026-10-06)

- **Decision**: An invoice is approved without a person only if it is a tax invoice, every check passes
  (confidence, GSTINs, GST type, arithmetic, date, duplicates, amount in words, e-invoice QR when
  present) and `grand_total <= AUTO_APPROVE_LIMIT` (50,000). Anything else goes to NEEDS_REVIEW with
  the reasons.
- **Why**: These are the business's words: "if data was correct add it directly, if not take approval".
  The limit caps the damage a wrong auto-approval can do.
- **Instead of**: Approving everything (too risky) or reviewing everything (no automation gain).
- **Trade-off**: Auto-approval trusts the AI's reading, cross-checked only by arithmetic. A document
  whose wrong numbers happen to add up can pass. That is why the second reading (D-41), the QR
  (D-40) and the words check (D-42) were added.
- **Where**: `app/pipeline/decide.py` (`decide`), `app/pipeline/validate.py`.

### D-03 No login for now; the acting user is the `X-User-Id` header (≈ 2026-10-06)

- **Decision**: Every request names its user in a header, and there are no passwords.
- **Why**: The business said "no login for now". The system runs on one office PC, and login is planned
  for Phase 4.
- **Instead of**: Basic auth or OAuth now.
- **Trade-off**: Anyone who can reach the API can act as any user. This is acceptable only on a
  trusted machine or network. The security audit (2026-10-16) showed that features meant to
  restrict things per user, like report recipients (D-35), are weakened by this.
- **Where**: `app/api.py` (`current_user`).

### D-04 Each user connects their own Gmail through Composio (≈ 2026-10-06)

- **Decision**: Gmail access goes through Composio-managed OAuth. Each user is a Composio user
  `ap-user-<id>` with their own connected account.
- **Why**: There is no Google Cloud project or OAuth app to maintain, and Composio offers triggers (new
  email events) and tools (fetch, download, label, send) ready-made. The business already had a key.
- **Instead of**: The Gmail API with our own OAuth client, or IMAP polling.
- **Trade-off**: A third party sits in the email path. We depend on its SDK behaviour (for example, the
  trigger id being reused for every event, see D-12), and its outages become ours.
- **Where**: `app/gmail.py`.

---

## 2. Architecture and infrastructure

### D-10 Postgres is also the job queue (`FOR UPDATE SKIP LOCKED`) (≈ 2026-10-06)

- **Decision**: A document waiting to be read is a row with status RECEIVED. A worker claims it with
  `SELECT ... FOR UPDATE SKIP LOCKED` and sets it to PROCESSING.
- **Why**: There is no Docker and no Redis on the machine. One fewer service to install and keep
  running. The database row is the job, so there is never a "job without invoice" or the reverse.
- **Instead of**: Celery or RQ with Redis, or an in-memory queue.
- **Trade-off**: Polling (2 s idle wait) instead of push, and a crashed worker leaves PROCESSING rows
  that need reclaiming after a timeout (`reclaim_stale`).
- **Where**: `app/worker.py` (`claim_next`, `reclaim_stale`).

### D-11 Worker, Gmail listener and report scheduler run inside the API process (≈ 2026-10-07)

- **Decision**: `uvicorn app.api:app` starts everything. The FastAPI lifespan starts background
  threads (`app/background.py`). `RUN_WORKER=false` / `RUN_GMAIL_LISTENER=false` allow separate
  processes.
- **Why**: The business asked for it ("why do we need to run three... include all in one").
- **Instead of**: Three commands or a process manager.
- **Trade-off**: You can't run `uvicorn --workers N`, because the listener and the scheduler must
  exist once. Scale by adding `python -m app.worker` processes instead (D-60).
- **Where**: `app/api.py` (`lifespan`), `app/background.py`.

### D-12 Gmail events: websocket subscription, deduplicated by Gmail message id (2026-10-08 → 10-14)

- **Decision**: `triggers.subscribe()` (pysher websocket) receives new-email events, so no public URL
  is needed. Repeats are dropped using the key `user/trigger/message_id`.
- **Why**: Composio's client re-binds its handler when it reconnects and delivers some events twice.
  The first dedupe used `event["id"]`, but that is the **trigger** id, the same for every email. So every
  email after the first was dropped: the "works once, then stops" bug.
- **Instead of**: Polling only.
- **Trade-off**: An in-memory dedupe (last 1000 keys) forgets on restart. The sync and the per-file
  ingest dedupe (D-20) catch what it misses.
- **Where**: `app/gmail_listener.py` (`_first_delivery`).

### D-13 A safety sync every 5 minutes, plus a daily reconciliation (2026-10-08)

- **Decision**: Besides live events, every inbox is fetched since its last sync point every 5 minutes,
  and the last 7 days are compared with the email log once a day.
- **Why**: Websockets drop silently, and events get lost while the server is down. "Never miss an
  email" was the top goal (Phase 1).
- **Instead of**: Trusting live events alone.
- **Trade-off**: More Composio calls. The sync-point logic is delicate: the rewind, overlap and cap
  rules are in D-31.
- **Where**: `app/gmail_listener.py` (`_periodic_sync`), `app/gmail.py` (`sync_user`, `reconcile_user`).

### D-14 Tables are created with `create_all`; no migrations yet (≈ 2026-10-06)

- **Decision**: The schema is created on startup and never altered automatically.
- **Why**: Fast iteration while the schema settles.
- **Instead of**: Alembic now.
- **Trade-off**: An existing table can't change. Every new piece of state since then went into a **new
  table**: `gmail_health`, `chat_sessions`, `chat_messages` and `report_runs`. Learning-from-corrections
  reuses the audit log (D-43) instead of adding columns. Alembic is in Phase 4.
- **Where**: `app/api.py` (`lifespan`), `app/models.py`.

### D-15 Agno + OpenAI with a Pydantic output schema for extraction (≈ 2026-10-06)

- **Decision**: An Agno `Agent` with `output_schema=ExtractedInvoice` reads each document.
  gpt-4o-mini (`OPENAI_MODEL`) handles text PDFs; gpt-4o (`OPENAI_SCAN_MODEL`) handles scans and photos.
- **Why**: Structured output gives validated fields, not text to parse. On the golden set, mini misread a
  GSTIN on the scan and used ~13× the input tokens for images, so scans go to 4o.
- **Instead of**: OCR plus regex, or a single model for everything.
- **Trade-off**: The prompt must stay short (the business asked for concise prompts). A schema change
  changes what the model returns; for example, adding `amount_in_words` was checked on the golden set (D-42).
- **Where**: `app/pipeline/extract.py`, `app/schemas.py`.

### D-16 PDF pages are sent as images AND as their text layer (≈ 2026-10-06)

- **Decision**: For PDFs, up to `MAX_PAGES` pages are rendered to PNG (200 dpi), and the text layer is
  also included for exact digits. When a PDF has under 50 characters of real text, it is a scan.
- **Why**: Images keep the layout (which number is in which column). The text layer gives the
  exact digits.
- **Trade-off**: More tokens. Pages beyond 5 are not read (flagged for review; roadmap item 2.5).
- **Where**: `app/pipeline/extract.py` (`prepare_pages`).

### D-17 Demo frontend: plain HTML/JS plus a Python stdlib proxy (≈ 2026-10-09)

- **Decision**: No build step and no npm. `server.py` serves `public/` and forwards `/api/*` to the
  backend. Since 2026-10-14 it also passes Server-Sent Events through as they arrive.
- **Why**: npm is blocked on the machine by policy. The backend has no CORS, and one origin avoids it.
- **Trade-off**: The proxy forwards anything to the API with no auth. The audit found it accepts any
  Host header (DNS rebinding) and sends no frame protection: medium/low findings, still to fix.
- **Where**: `A:\Project\practice\Demo\Invoice demosite\server.py`.

---

## 3. Ingestion

### D-20 One entry point for every channel, deduplicated by file hash (≈ 2026-10-06)

- **Decision**: Uploads, Gmail attachments, documents inside zip/eml files, invoice links and email
  bodies all go through `ingest_document`. The SHA-256 of the bytes is unique, and the file is stored
  as `<hash><ext>`.
- **Why**: The same PDF often arrives twice (forwarded, re-sent, or via another user). One record, one
  AI call.
- **Trade-off**: A re-sent file on another email is recorded as an audit note on the first invoice
  ("received again"), not as a new record.
- **Where**: `app/ingest.py`.

### D-21 An email log table records what happened to every email (2026-10-08)

- **Decision**: The `email_messages` table holds one row per Gmail message, with an outcome per
  attachment: invoice, same_file, unpacked, unsupported, too_large or failed.
- **Why**: The business asked "I got 5 invoices but it shows 4". Without the log, nobody could see why.
- **Where**: `app/gmail.py` (`_email_row`, `_ingest_email`), `GET /api/v1/emails`.

### D-22 Containers are unpacked: .zip and forwarded .eml, two levels deep (2026-10-08)

- **Decision**: `expand()` returns the documents inside a container. A forwarded email inside an email
  is read too (fixed 2026-10-15). An email body that is itself an invoice is rendered to PDF.
- **Trade-off**: Memory. The whole container and its contents are held in memory. The audit flagged
  zip-bomb risk; there is still no per-archive total limit (open).
- **Where**: `app/documents.py`.

### D-23 Invoices behind download links: allow-list, https only, manual redirects (2026-10-15)

- **Decision**: Links in the email text are fetched only if their host is in `GMAIL_LINK_DOMAINS`
  (empty means off), use https, and every redirect stays inside the allow-list. A portal login
  page is logged as "not a document".
- **Why**: An outside sender controls the link. Following it blindly would let them make our server
  fetch internal addresses (SSRF).
- **Instead of**: Following any link, or ignoring links.
- **Trade-off**: Supplier portals must be listed by hand. Portals behind a login can't be read at all.
- **Where**: `app/gmail.py` (`links_in`, `fetch_link`, `_ingest_link`).

### D-24 Excel writes: a Postgres advisory lock, temp file, `os.replace` (≈ 2026-10-06)

> Weak spot fixed by D-88.
- **Decision**: `export_pending` takes `pg_advisory_xact_lock`, appends to the workbook, saves to a
  temp file and atomically replaces the real one. If the file is open in Excel, it raises
  `ExcelLocked`, and the worker retries every 10 s.
- **Why**: Several threads or processes could export at once. A half-written workbook would be
  corrupt.
- **Trade-off**: Every export reloads and resaves the whole workbook (slower as it grows). Rows already
  present are recognised **only by invoice id**. The audit confirmed this breaks after a database reset
  (ids restart at 1): to fix.
- **Where**: `app/excel.py`.

---

## 4. Correctness fixes that became rules (2026-10-15 review)

### D-30 Every status change locks the invoice row

- **Decision**: approve, reject, correct, reprocess, process-as-invoice and password all load the invoice
  with `SELECT ... FOR UPDATE`.
- **Why**: Two people clicking Approve and Reject at once could leave an invoice REJECTED but already
  in Excel.
- **Where**: `app/api.py` (`_locked_invoice`).

### D-31 Sync point rules: continue past the cap, 10-minute overlap, stop rewinding after a day

- **Decision**:
  - A sync takes up to 500 emails. If more are waiting, the next sync starts from the oldest one handled.
  - Every sync re-reads the last 10 minutes.
  - A failing email moves the sync point back, but only for its first day; after that, the daily check owns it.
  - Files over `MAX_UPLOAD_MB` are logged as "too_large", not as failures.
- **Why**: The review found that more than 100 emails meant the oldest were skipped forever, and that one
  permanently broken email pinned the sync point and made every sync re-read a growing window.
- **Where**: `app/gmail.py` (`_sync_user`, `_failing_for_long`, `TooLarge`).

### D-32 A worker only writes its result if it still owns the invoice

- **Decision**: The worker remembers `(attempts, updated_at)` when it claims an invoice. Before writing,
  it re-reads the row with a lock, and drops its result if the row was reclaimed meanwhile. The
  database transaction is also committed before the AI call.
- **Why**: An AI call longer than the processing timeout could be redone by another worker, and the
  slower one would overwrite the newer result. An open transaction during a 20 s AI call wastes a
  connection.
- **Where**: `app/worker.py` (`_still_ours`, `process`).

### D-33 "AI service down" only when Agno reports a failed run

- **Decision**: An invoice is requeued without using up an attempt only if the run status is error AND
  the message looks transient (timeout, 429, 5xx…). Requeued invoices go to the back of the queue
  (the queue is ordered by `updated_at`).
- **Why**: The model's own text ("INV/500, payment timeout 30 days") matched the "transient" pattern. That
  invoice was retried forever at the head of the queue, and nothing else was processed.
- **Where**: `app/pipeline/extract.py`, `app/worker.py` (`claim_next`).

### D-34 Excel cells are made safe; one bad invoice can't block exports

- **Decision**:
  - Control characters are removed from cells.
  - Text starting with = + - @ is stored with a leading apostrophe.
  - An invoice whose row can't be built goes back to review instead of failing the whole export.
  - Credit notes are always negative (`-abs`).
- **Why**: Text from outside senders went straight into cells. A stray control character stopped all
  exports, and a value starting with "=" became a formula.
- **Trade-off**: A leading apostrophe is visible in some tools. The assistant export gap was closed
  on 2026-10-16 by D-54, which also covers formula prefixes after whitespace.
- **Where**: `app/excel.py` (`_safe`, `_rows`).

### D-35 Report "send now" only to people of this company

> Superseded by D-85.
- **Decision**: Hand-sent reports may only go to users of the app or `REPORT_RECIPIENTS`.
- **Why**: Reports go out from a colleague's Gmail, and could be sent to any address.
- **Superseded by**: the audit (2026-10-16) showed anyone can create a user with any email, so "users of
  the app" is no protection. To fix: recipients only from `REPORT_RECIPIENTS` (or verified addresses).
- **Where**: `app/reports.py` (`allowed_recipients`), `app/api.py` (`report_send`).

---

## 5. Gmail operations

### D-36 New-email events are queued and handled by a thread pool, not on the websocket thread (2026-10-15)

- **Why**: Composio calls our handler on its socket thread and waits for it. A slow download made the
  connection miss its pings and drop.
- **Where**: `app/gmail_listener.py` (`receive`, `_handle_events`).

### D-37 Gmail connection health in its own table (2026-10-15)

- **Decision**:
  - Every 10 minutes, and right after a sync fails with an access error, the connected account's status
    is checked at Composio.
  - A broken inbox shows "Reconnect Gmail" and is skipped by the sync.
  - The alert email is opt-in (`GMAIL_HEALTH_ALERTS`, off).
- **Why**: Expired Google access failed silently, every 5 minutes, forever.
- **Instead of**: A column on `users` (impossible without migrations, D-14).
- **Trade-off**: The alert email is sent from another user's inbox, which is why it is off by default.
- **Where**: `app/gmail_health.py`.

### D-38 Webhook delivery for production, verified with Composio's SDK (2026-10-15)

- **Decision**: `POST /webhooks/composio` is active only with `GMAIL_DELIVERY=webhook` and
  `COMPOSIO_WEBHOOK_SECRET`. The signature and timestamp (5 minutes) are verified with
  `triggers.verify_webhook`. The event then joins the same queue as websocket events.
- **Why**: A server needs a public endpoint, not a websocket. Using the SDK's verifier means we don't
  re-implement HMAC rules.
- **Trade-off**: The old webhook format (V1) names only the connected account, so the user is looked
  up from it.
- **Where**: `app/api.py` (`composio_webhook`).

---

## 6. Reading accuracy (Phase 2, 2026-10-15)

### D-40 E-invoice QR: OpenCV, searched only when an IRN is shown, values override the AI

> Partly superseded by D-82 (an unverified QR no longer overrides).
- **Decision**:
  - When the AI read an IRN or the text layer shows one, the page corners are rendered at 400/600 dpi and
    decoded with OpenCV's QR detector (zxing-cpp if installed).
  - The signed token's values (GSTINs, number, date, total, IRN, document type) replace the AI's.
  - The signature is verified with RS256 if NIC's public key is in `EINVOICE_KEYS_DIR`.
- **Why**: The QR is machine-readable data signed by the government portal. Exact data is better
  than a careful reading.
- **Instead of**: zxing-cpp as a required dependency, which is more robust but a new package. OpenCV
  was already installed.
- **Trade-off**: OpenCV only decoded the real HFCL QR at ≥400 dpi with thresholding, about 1–4 s per
  e-invoice, hence "only when an IRN is shown". **Without the NIC key the QR is used unverified**: a
  forged QR could set the values. The check says "signature not checked". The audit flagged this; it is
  being verified.
- **Where**: `app/pipeline/einvoice.py`.

### D-41 Second reading by gpt-4o when checks fail, told exactly what failed

> Refined by D-83.
- **Decision**: If a check that a misreading can explain FAILs, the document is read once more by
  `AI_SECOND_PASS_MODEL` with the failed checks listed. The reading with fewer such failures is kept.
  These checks count: totals, lines, tax, GST type, a GSTIN with a wrong check digit, date, confidence,
  and amount in words.
- **Why**: Most review cases were misread digits. A second guided look fixes many of them without a person.
- **Trade-off**: One extra AI call, only for failing documents. **Risk:** the model could "fix" numbers
  to make them add up. The prompt explicitly forbids it ("never change a number just to make the totals
  add up"). A missing GSTIN, foreign currency and business rules never trigger it.
- **Where**: `app/pipeline/reading.py`.

### D-42 Cross-checks inside the document: amount in words, look-alike invoice numbers

- **Decision**: The AI also returns the total written in words, and it must match the figure (an Indian
  lakh/crore parser). An invoice number that differs from an earlier one from the same supplier only
  by O/0, I/1, S/5 or B/8 goes to review.
- **Why**: Words and figures are printed separately, so a misread digit shows up as a mismatch.
- **Trade-off**: A new schema field (D-15). Measured on the golden set: no regression. The ITI scan's
  misses of place of supply and PO number existed before.
- **Where**: `app/pipeline/words.py`, `app/pipeline/validate.py`.

### D-43 Learning from corrections reuses the audit log

- **Decision**: A reviewer's PATCH is already audited with before → after values. For a supplier with
  past corrections, those notes are added to the email hints and to the second reading.
- **Why**: Suppliers keep their layouts, so mistakes repeat. Reusing the audit log needs no new table.
- **Trade-off**: A careless or malicious correction becomes a hint for future readings. The audit
  flagged this as "reviewer corrections become persistent prompts"; it is being verified.
- **Where**: `app/pipeline/learning.py`.

### D-44 The worker and the evaluation script share one reading path

- **Decision**: `read_document()` (extract → QR → second reading) is used by both.
- **Why**: Measure what actually runs. Before this, the evaluation tested extraction alone.
- **Where**: `app/pipeline/reading.py`, `scripts/evaluate.py`.

---

## 7. AI assistant

### D-54 Assistant exports share the register's cell protection (2026-10-16)

- **Decision**: Apply `excel._safe` to every assistant CSV value and both XLSX sheets before writing.
  The shared sanitizer removes illegal control characters and prefixes an apostrophe when the first
  non-whitespace character is `=`, `+`, `-` or `@`. Numeric values, including negatives, stay numeric.
- **Why**: Supplier names, invoice numbers, filenames and line descriptions are untrusted text. Raw
  spreadsheet exports could interpret that text as formulas; CSV quoting alone does not prevent this.
- **Instead of**: A second sanitizer for assistant exports or changing stored invoice values.
- **Trade-off**: The apostrophe can be visible in spreadsheet/import tools. Existing export files are
  not rewritten; regenerate them to get this protection.
- **Where**: `app/assistant_tools.py` (`export_invoices`), `app/excel.py` (`_safe`),
  `tests/test_assistant.py` (XLSX/CSV regression coverage).

### D-50 The assistant can read but not change; actions are proposals a person confirms (2026-10-13)

> Refined by D-87.
- **Decision**: The assistant's tools are read-only queries plus file exports. `propose_action` only
  creates a button, and clicking it calls the normal API after a confirmation.
- **Why**: Text in documents and emails comes from outside the company (prompt injection). A model
  must never be able to approve or reject by itself.
- **Trade-off**: The audit found proposals stay clickable later and act on the invoice's current state,
  not the one the assistant judged (to fix: store a version and check it).
- **Where**: `app/assistant_tools.py`, `app/assistant.py`.

### D-51 Saved chats in the database; answers streamed (SSE); fully async (2026-10-14 → 15)

- **Decision**: The `chat_sessions` and `chat_messages` tables, owned per user. Answers stream as
  Server-Sent Events. The endpoints are `async` and use Agno's `arun`; database work happens in short
  thread calls before and after.
- **Why**: The business asked for chats saved in the database. Async means a chat waiting 10 s for
  OpenAI holds neither a server thread nor a database connection. The load test showed it matters.
- **Trade-off**: If the browser disconnects, the partial answer is saved from a separate thread,
  because the request's task is being cancelled.
- **Where**: `app/api.py` (`ask_in_chat_streamed`, `_start_turn`, `_save_turn`), `app/chat_store.py`.

### D-52 Assistant links only inside the app (2026-10-15)

- **Decision**: The frontend renders only `#/…` and `/api/…` links. Any other link becomes plain text.
- **Why**: An answer can be steered by text in an email, so an outside link could leak data in its URL.
- **Trade-off**: The audit notes `/api/` links can still point at side-effecting GET endpoints (low; to
  tighten to `/api/v1/assistant/files/` and `/api/v1/excel`).

### D-53 Reports are built from the database, not by the AI (2026-10-14)

- **Decision**: The summary email's numbers come from SQL, the HTML is escaped, and it is sent via
  Composio. The schedule is off by default. A slot such as `daily:2026-10-14` is claimed with a unique
  row before sending.
- **Why**: Financial numbers must be reproducible, and a report must not go out twice.
- **Trade-off**: A send that times out after Gmail accepted it is retried 30 minutes later, so it can
  go out twice. The audit confirmed this (to fix: treat ambiguous failures as "unknown", not "failed").
- **Where**: `app/reports.py`.

---

## 8. Concurrency: many users at once (2026-10-15)

### D-60 Threads, not a full async rewrite

- **Decision**:
  - Keep synchronous SQLAlchemy and blocking libraries in threads; make only the assistant async.
  - Run `WORKER_CONCURRENCY` worker threads.
  - Sync inboxes in parallel with one sync per inbox (a lock).
  - Handle Gmail events with a pool of threads.
  - Raise the thread limit for blocking endpoints.
- **Why**: The AI (sync tools), Composio, openpyxl and PyMuPDF all block anyway, so an async database
  driver would add risk without making anything faster. The load test proved the real problem was
  connections held while waiting.
- **Instead of**: Async SQLAlchemy everywhere.
- **Trade-off**: Thread-safety must be handled by hand (D-61), and Python's GIL limits CPU-heavy work
  in one process.
- **Where**: `app/background.py`, `app/worker.py`, `app/gmail_listener.py`, `app/api.py`, `app/db.py`.

### D-61 One lock around every PyMuPDF call

- **Decision**: `PDF_LOCK` (an RLock in `app/documents.py`) is held for every MuPDF call. QR decoding
  runs outside it.
- **Why**: During the 100-user load test, parallel workers crashed the whole server process inside
  MuPDF, which is not thread-safe.
- **Trade-off**: PDF rendering is serialized, which costs throughput when many PDFs arrive at once.
  Correctness first.
- **Where**: `app/documents.py`, `app/pipeline/extract.py`, `app/pipeline/einvoice.py`, `app/api.py`.

### D-62 Requests release their database connection early

- **Decision**: `current_user` commits right after loading the user. The Excel download reads the file
  into memory instead of streaming it from disk.
- **Why**: Measured: 30 users exhausted the 15-connection pool, and 33 requests failed. A streamed
  workbook could arrive cut off if a worker replaced it mid-download.
- **Where**: `app/api.py`, `app/db.py` (`DB_POOL_SIZE` / `DB_MAX_OVERFLOW`).

---

## 9. Quality process

### D-70 Tests use a real Postgres; only the outside services are faked

- **Decision**: The `ap_test` database is emptied per test. OpenAI, Composio and downloads are fakes.
- **Why**: Locks, `SKIP LOCKED`, advisory locks and JSONB behave differently in SQLite or mocks, and the
  bugs that mattered were in exactly those.
- **Where**: `tests/conftest.py`.

### D-71 The golden set measures accuracy, so changes are judged on data

- **Decision**: The `golden/` folder holds verified documents; `python -m scripts.evaluate` reports
  per-field accuracy, decisions and wrong auto-approvals.
- **Why**: "More accurate" is a guess without a measurement. Phase 2 changes were all checked against it.
- **Trade-off**: Only 7 documents. 50–100 are needed (the business's help is needed).

### D-72 Reviews and the security audit use independent agents that must agree (2026-10-15 / 16)

- **Decision**: Finders per subsystem and lens (security and correctness), then 2–3 skeptical verifiers
  per finding, each with a different lens (reproduce / existing guard / impact). A finding is
  confirmed only if most verifiers can't refute it.
- **Why**: One reviewer produces plausible-but-wrong findings. Of 87 candidates in the first audit
  round, 58 did not survive or were not yet verified.

---

## 10. Audit fixes, batch 1 (2026-10-16)

The audit's second pass confirmed 47 findings (2 high, 15 medium, 30 low; several duplicates). This batch
fixes the high ones and the mediums that change money or data outcomes. Each has a regression test in
`tests/test_audit_fixes.py`, `tests/test_einvoice.py`, `tests/test_reading.py`, `tests/test_reports.py` or
`tests/test_background.py`.

### D-80 One unpacking budget per upload/attachment (zip bombs)

- **Decision**:
  - A single budget covers everything one upload or attachment unpacks, nested containers included:
    at most 3 × `MAX_UPLOAD_MB` in total and 50 documents.
  - Each entry is read only up to its declared (budgeted) size.
  - A damaged zip is reported, not raised.
- **Why**: A 150 KB zip of zeros unpacked to 150 MB (measured). Anyone who can email the inbox could
  exhaust the memory of the one process that hosts the API, workers and listener. 1,000 small PDFs in a zip
  would also mean 1,000 AI calls.
- **Instead of**: Streaming entries one by one. That's better for memory but a larger change; the budget
  bounds memory to ~45 MB.
- **Trade-off**: A real archive with more than 50 invoices is cut off (the rest are listed as skipped).
- **Where**: `app/documents.py` (`_Budget`, `expand`).

### D-81 Logos are not documents

- **Decision**:
  - Inline images in a forwarded .eml are ignored.
  - Images under 30 KB (`LOGO_MAX_BYTES`), in an .eml or attached to a Gmail email, are treated as a
    logo or signature.
- **Why**: A signature logo counted as "a document", so an invoice written in the email body was never
  read. The logo went to the AI instead (NOT_INVOICE, and money spent).
- **Trade-off**: A genuine invoice photo under 30 KB would be skipped, but that is unreadably small in
  practice.
- **Where**: `app/documents.py` (`_is_logo`), `app/gmail.py` (`_ingest_email`).

### D-82 An unverified e-invoice QR confirms, it never overrides (supersedes part of D-40)

- **Decision**:
  - Only a QR whose signature is verified with NIC's key replaces the AI's values.
  - Without a key, the QR is compared: a match is a PASS, and any difference is a FAIL listing both
    values, so a person decides.
  - A malformed QR is a FAIL check, not a crash.
  - The document's own payload can no longer set `verified`.
- **Why**: Anyone can print a QR. Before, an unsigned QR could turn a credit note into a payable
  invoice or change the total, and the check still said PASS. A real bug was found while fixing this:
  `{..., "verified": verified, **data}` let a forged payload's own `"verified": true` overwrite our result.
- **Trade-off**: Without `EINVOICE_KEYS_DIR`, a QR can no longer silently fix a misread total. That case
  now goes to review. Get NIC's key to get the automatic correction back.
- **Where**: `app/pipeline/einvoice.py` (`parse_token`, `apply`, `check_document`).

### D-83 The second reading can't escape the checks, and changed amounts must be in the document (refines D-41)

- **Decision**:
  - A second reading that changes the document type, the currency, or a GSTIN that wasn't the
    problem is ignored.
  - If it changed amounts, the corrected total must appear in the document's text (PDF text layer,
    Excel, CSV, Word). Otherwise a WARN sends the invoice to review.
- **Why**: "Fewer failed checks" could be reached by becoming a USD invoice or a non-invoice (checks
  skipped), or by a model inventing numbers that add up. The text layer is independent evidence; a
  model's rewrite isn't.
- **Trade-off**: Scans and photos have no text layer, so a corrected scan always goes to review.
  Correctness first. The match is exact: 65,908.16 is not "found" inside 10,65,908.16.
- **Where**: `app/pipeline/reading.py` (`_escapes_checks`, `_total_in_text`).

### D-84 A correction keeps the checks only the worker can make

- **Decision**: When a person's PATCH or process-as-invoice re-runs the checks, the page-limit check is
  kept, and the e-invoice QR (from the audit log) is compared again against the corrected values.
- **Why**: Re-checking silently erased a failed QR check, the one warning that said "this document
  contradicts its own signed QR".
- **Where**: `app/pipeline/evaluate.py` (`_carried_checks`), `app/pipeline/einvoice.py` (`recheck`).

### D-85 Report recipients only from `REPORT_RECIPIENTS` (supersedes D-35)

- **Decision**: Scheduled and hand-sent reports go only to the addresses set in `REPORT_RECIPIENTS`.
  With none set, the schedule waits (one warning) and "send now" refuses.
- **Why**: With no login, anyone could create a user with an outside address and then receive the
  company's payables summary from a colleague's Gmail.
- **Trade-off**: One more setting is required before reports work.
- **Where**: `app/reports.py` (`default_recipients`, `allowed_recipients`, `run_due`).

### D-86 Only the API process writes Excel

- **Decision**: `python -m app.worker` processes read documents but never export. The API process exports,
  from its own workers or (with `RUN_WORKER=false`) from an `excel-export` thread (`RUN_EXCEL_EXPORT`).
- **Why**: The README endorses workers on other PCs. Each would have written approved invoices into its
  own local workbook, which the accounts team never sees.
- **Where**: `app/worker.py` (`run(write_excel)`, `export_loop`), `app/background.py`.

### D-87 Assistant proposals carry a version; a stale button is refused (refines D-50)

- **Decision**:
  - A proposal stores the invoice's `updated_at`.
  - approve, reject, reprocess and process-as-invoice accept `if_unchanged_since` and answer 409 if the
    invoice changed since then.
  - The confirm dialog shows the invoice's own supplier, number and total, not just the model's reason.
- **Why**: A saved chat's Approve button acted on whatever the invoice had become days later. The confirm
  showed only text the model wrote, which document text can influence.
- **Where**: `app/assistant_tools.py` (`propose_action`), `app/api.py` (`Review`, `_unchanged`),
  `public/assistant.js` (`runAction`).

### D-88 Workbook rows are recognised by id + GSTIN + invoice number (fixes D-24's weak spot)

- **Decision**: An invoice is "already in the workbook" only if a row has the same id, supplier GSTIN and
  invoice number. Same id with other values: it is written anyway, with a warning in the log.
- **Why**: After `setup_db --reset`, ids restart at 1. New invoices were marked EXPORTED but never
  written: silent loss.
- **Trade-off**: After a reset, the workbook can hold two rows with the same Invoice ID (old and new).
  Visible and explainable, unlike a missing row.
- **Where**: `app/excel.py` (`export_pending`, `_key`).

### D-89 Other fixes in this batch

- **Render cap**: `render_dpi` (`app/documents.py`) caps a rendered page or crop at 25 megapixels. A
  1 KB PDF declaring a 200-inch page would otherwise need ~8 GB, under the global PDF lock.
- **Journal matching**: journals match only invoices from the same supplier (`journal.same_supplier`:
  GSTINs when both are known, otherwise the names' significant words: "ITI Limited" ~ "ITI Ltd.
  Raebareli"). Before, a journal could be marked DUPLICATE of another supplier's invoice with the same
  number and amount.
- **requirements.txt**: now lists numpy, opencv-python-headless (≥ 4.8, for `QRCodeDetectorAruco`), PyJWT,
  cryptography, python-docx and anyio. A fresh install failed on import.

Not yet fixed (next batches): the remaining low findings, among them:

- upload/webhook body size before parsing
- Gmail admin routes without `X-User-Id`
- ambiguous report re-sends
- credit-note sign in reports and assistant totals
- the export download having no owner or expiry
- the demo proxy's Host check and frame protection
- relative stored file paths
- the AI-down retry with no limit
- the processing timeout shorter than the worst AI call
- shutdown consuming an attempt
- log forging via file names
- the Spam folder being included by default

---

## 11. Testing with generated invoices (2026-10-16)

### D-90 An unusable OpenAI account makes documents wait, not fail

- **Decision**:
  - "No credits remaining", insufficient quota, an invalid or revoked key, or a disabled account (only
    when Agno reports a failed run) raises `AIAccountProblem`.
  - The document goes back to the queue without using up an attempt, the workers pause 5 minutes, and
    `GET /api/v1/system/status` plus a banner in the demo say why.
- **Why**: Seen in the real database: the account ran out of credits, and 5 test invoices became FAILED
  after 3 attempts each, as if the documents were broken. Nothing about the document can fix an empty account.
- **Instead of**: Treating it as a failed attempt (old behaviour, D-33), or as a short outage (30 s retries
  would only hammer the API).
- **Trade-off**: An invalid key now also waits instead of failing. That changes the earlier test, where
  a 401 counted as a permanent failure, but it is the same "fix the account, not the document" situation.
- **Where**: `app/pipeline/extract.py` (`AIAccountProblem`), `app/worker.py` (`ai_status`), `app/api.py`
  (`system_status`), `public/app.js` (`loadSystemStatus`).

### D-91 Test invoices that follow the rules, with an expected outcome; simulated users

- **Decision**:
  - `scripts/make_test_invoices.py` generates GST-correct invoices (valid GSTIN check digits, billed to
    `COMPANY_GSTIN`, place of supply, CGST+SGST/IGST, totals in words) and one deliberate defect per
    scenario, each with its expected status.
  - `scripts/simulate_users.py` has N users upload them at once and compares the outcomes.
  - `tests/test_generated_invoices.py` proves, without OpenAI, that every promised outcome matches the rules
    (a perfect stand-in reader), single user and 3 users with 4 parallel workers.
- **Why**: The fake invoices arriving by email (random "99…" GSTINs with wrong check digits, no buyer GSTIN,
  ₹4–5 lakh) all fail the checks by design, so they could only ever test the review path, never
  auto-approval, duplicates or decisions per scenario.
- **Instead of**: A "test mode" in the app that relaxes the checks. Rejected: a switch that weakens
  approval rules in production code is a risk, and it would test a different system.
- **Trade-off**: The generated layout is clean billing-software output. It tests the rules and
  concurrency, not the AI's reading of messy scans (the golden set covers that).
- **Found while building it**: a quotation with a GSTIN, lines and a total "looks like an invoice" and goes
  to review on purpose (D-02's safety net), so the not-an-invoice scenario uses a terms letter instead.
- **Measured**:
  - 4 users × 18 documents (72) uploaded at the same moment, 4 workers: 72/72 got their expected outcome,
    and the same file from two users stayed one record.
  - Uploads took 2.5 s; everything was read in 6 s with the stand-in reader (with the real AI, expect
    ~5–20 s per document, 4 at a time).
- **Where**: `scripts/make_test_invoices.py`, `scripts/simulate_users.py`, `tests/test_generated_invoices.py`.
