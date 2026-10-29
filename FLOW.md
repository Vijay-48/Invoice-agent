# How execution flows

This traces what actually calls what, in which order, across files: one section per entry point.
Function names are given as `module.function` (all under `app/` unless shown otherwise). Line numbers
are left out on purpose because they drift; search for the function name.

The last section, **"You are here"**, shows where current work sits on these paths.

```
                      ┌──────────── browser (demo frontend, public/*.js) ───────────┐
                      │  server.py (port 5173): serves public/, proxies /api/* →     │
                      └──────────────────────────────┬──────────────────────────────┘
                                                     ▼
 Gmail ─(Composio websocket or signed webhook)─► app/api.py  (FastAPI, port 8000) ◄── uploads, reviews, chat
                                                     │
          ┌──────────────────────────────────────────┼──────────────────────────────────┐
          ▼                                          ▼                                  ▼
 app/gmail_listener.py ─► app/gmail.py ─►  app/ingest.py ──► Postgres (invoices: RECEIVED)
 (threads: sync, events)  (Composio calls)  (hash, store file)          │
                                                                        ▼
                                               app/worker.py (WORKER_CONCURRENCY threads)
                                                        │ claim_next (SKIP LOCKED)
                                                        ▼
                         app/pipeline/reading.py: extract.py ─► einvoice.py ─► (second reading)
                                                        ▼
                         app/pipeline/evaluate.py: store ─► validate.py ─► journal.py ─► decide.py
                                                        ▼
                         APPROVED ─► app/excel.py ─► output/invoices.xlsx (EXPORTED)
                         NEEDS_REVIEW ─► a person: PATCH / approve / reject (app/api.py)
```

---

## 1. Startup: `uvicorn app.api:app`

1. Python imports `app.api`, which imports `app.config.settings` (reads `.env`), `app.db.engine` (the
   connection pool, sized by `DB_POOL_SIZE`/`DB_MAX_OVERFLOW`), the models, and `app.background`.
   That in turn imports `worker`, `gmail_listener` and `reports`.
   - Importing `app.pipeline.extract` builds the extraction agents: `extractor` (OPENAI_MODEL) and
     `scan_extractor` (OPENAI_SCAN_MODEL).
2. Uvicorn starts the app and runs **`api.lifespan`**:
   1. `logs.setup_logging()`
   2. raises the thread limit for blocking endpoints to `API_THREADS`
   3. `Base.metadata.create_all(engine)`, which creates any missing tables (never alters them)
   4. `Background().start()`, which spawns daemon threads:
      - `worker`, `worker-2` … `worker-N` → `worker.run(stop)` (N = `WORKER_CONCURRENCY`)
      - `gmail-listener` → `gmail_listener.run(stop)` if `RUN_GMAIL_LISTENER` and `COMPOSIO_API_KEY` are
        set. It starts more threads itself:
        - `gmail-sync` → `_periodic_sync`
        - `gmail-events-1..N` → `_handle_events` (N = `GMAIL_CONCURRENCY`)
        - then, in webhook mode, it just waits. Otherwise it loops: `triggers.subscribe()` →
          `subscription.handle(...)(receive)` → `_watch()` until the connection drops or needs a refresh.
      - `reports` → `reports.run(stop)` if `REPORT_SCHEDULE` is daily, weekly or monthly
3. At shutdown, lifespan calls `Background.shutdown()`. It sets `stop` and joins the threads for up to
   30 s; a document being read goes back to the queue later through `reclaim_stale`.

## 2. A document is uploaded: `POST /api/v1/invoices/upload`

1. `api.current_user` reads `X-User-Id`, loads the user, then **commits**, so the connection goes back to
   the pool.
2. `api.upload` (a sync endpoint, run on a thread):
   1. `ingest.is_accepted(name)`: is the extension allowed?
   2. reads at most `MAX_UPLOAD_MB` + 1 bytes (413 if larger)
   3. `documents.expand(name, bytes)`: a .zip or .eml becomes its inner documents (two levels, one budget:
      at most 3 x `MAX_UPLOAD_MB` and 50 documents, D-80; inline logos ignored, D-81); an email body that is
      an invoice becomes a PDF via `documents.body_to_pdf` (under `PDF_LOCK`)
   4. for each document, `ingest.ingest_document(...)`:
      - SHA-256 of the bytes; if a row with that `file_hash` exists, return it ("duplicate_file")
      - write `STORAGE_DIR/<hash><ext>` and insert an `Invoice` with status **RECEIVED**
      - audit "ingest", then commit
3. Response: the first document's id and status, plus all documents and the skipped ones.

Nothing reads the document here. It now waits in the queue for section 4.

## 3. An email arrives: Gmail → queue

### 3a. Live event (websocket, the default)

1. Composio's websocket thread calls **`gmail_listener.receive(event)`**, which only does `_events.put(event)`
   (never blocks the socket).
2. One of the `gmail-events-N` threads: `_handle_events` → **`on_event(event)`**:
   1. `_first_delivery`: skips a repeat of the same `user/trigger/message_id`
   2. `gmail.handle_event(session, event)`: finds the user from `"ap-user-<id>"` → `gmail.handle_message`
3. **`gmail.handle_message`** → `_one_at_a_time(user, message_id)` (one thread per email) →
   **`_handle_message`**:
   1. `_email_row`: one `email_messages` row per message (`INSERT … ON CONFLICT DO NOTHING`)
   2. keyword filter (`GMAIL_SUBJECT_KEYWORDS`, off by default)
   3. **`_ingest_email`**:
      - **attachments** (`attachments_of`): skips unsupported files and ones already handled.
        Otherwise `execute(GMAIL_GET_ATTACHMENT)` → `download(s3url)` (size limit, `TooLarge`) →
        `documents.expand` → `_ingest_one` → `ingest.ingest_document` (section 2's dedupe)
      - **download links** (only if nothing usable was attached): `links_in(body)` (allow-listed https
        only) → `fetch_link` (redirects checked) → `expand` → `_ingest_one`
      - **body invoice** (still nothing): `looks_like_invoice_text` → `body_to_pdf` → `_ingest_one`
      - saves the per-attachment outcomes on the email row; outcome `done` or `no_documents`
   4. on an exception: the row becomes `failed`, and the error goes back up
4. Back in `on_event`: on failure, `_rewind` moves the user's sync point before this email, so the next
   sync fetches it again. If the network was down, `request_sync` asks for a sync right away.

### 3b. Webhook (`GMAIL_DELIVERY=webhook`)

`POST /webhooks/composio` → `api.composio_webhook` (async):
1. reads the raw body
2. `triggers.verify_webhook(id, timestamp, signature, secret)`: 401 if invalid or more than 5 minutes old
3. maps the connected account to our user
4. `gmail_listener.receive(event)`, so from here it is identical to 3a

### 3c. Safety nets (thread `gmail-sync` → `_periodic_sync`, loops forever)

- **Every 5 minutes (60 s after a network failure)**: `_sync_safely` → `sync_all` → a thread pool of
  `GMAIL_CONCURRENCY` → `_sync_one(user_id)`:
  - skips inboxes that need reconnecting (`gmail_health.needs_reconnect`)
  - `gmail.sync_user`: takes a **per-inbox lock**, refreshes the user, then `_sync_user`:
    1. the query is `GMAIL_QUERY after:<sync point − 10 min>`
    2. pages of `GMAIL_FETCH_EMAILS` → `handle_message` (3a.3) for each email
    3. a failure moves `next_since` back (only during the email's first day: `_failing_for_long`)
    4. if the 500-email cap is hit, `next_since` is the oldest email handled
    5. saves `gmail_last_sync_at`
  - on an access error (401, invalid_grant…), `gmail_health.check` runs right away
- **Every 10 minutes**: `_for_all_health` → `gmail_health.check_all` → `check(user)`, which asks
  Composio for the connected account's status → `record`. A change to "reconnect" logs, and with
  `GMAIL_HEALTH_ALERTS` also calls `_alert` (an email sent from another working inbox).
- **Every 60 s**: `update_all_labels` → `gmail.update_labels`, which puts AP/Processed, AP/Needs review or
  AP/Not invoice on emails whose invoices changed status
- **Daily (first run after 60 minutes)**: `reconcile_all` → `gmail.reconcile_user` → `sync_user(since = 7
  days ago)`, plus a summary

### 3d. Connecting a Gmail account (`POST /users/{id}/gmail/connect`)

1. `gmail.connect_link`: a Composio OAuth URL, plus a watcher thread `_activate_when_connected`
2. the user approves in the browser; Composio redirects to `GET /users/{id}/gmail/callback`
3. the callback or the watcher (whichever comes first, serialized by a per-user lock) calls
   `gmail.activate(if_needed)`:
   - `find_connected_account`
   - saves the account id (the sync point is set only on the first connection)
   - `triggers.create(GMAIL_NEW_GMAIL_MESSAGE)`
   - `gmail_health.record(OK)`
   - `_remove_other_triggers`

### 3e. A WhatsApp message arrives (Twilio): `POST /api/v1/webhook/whatsapp`

1. The router dependency `whatsapp.verify_twilio_signature` checks `X-Twilio-Signature` against
   `PUBLIC_BASE_URL` + path + form fields with `TWILIO_AUTH_TOKEN`. If the check fails (or no token is
   set), the request gets 403 before any field is parsed.
2. `whatsapp.incoming_message` (sync, on a thread) logs metadata only, then
   `sender_user_id` → `parse_senders(WHATSAPP_SENDERS)` → `user_exists`. An unknown sender is ignored.
3. It answers Twilio at once with empty TwiML. For a known sender with media, a FastAPI background
   task runs **`download_all`** after the reply:
   - for each `MediaUrl<i>`: `download_media`, which checks the account/message SIDs, the type and
     `check_media_url`, then `fetch`. That sends credentials only to `api.twilio.com`, follows
     redirects by hand to allowed hosts, and enforces the size limit and the 60 s deadline. Then
     `content_matches` checks the bytes.
   - `ingest_media` → `ingest.ingest_document(source="whatsapp", source_ref="<MessageSid>/<MediaSid>")`
     in its own session (section 2's dedupe)
   - one failed attachment is logged (reason or exception class only) and the next one carries on
4. From here it is section 4. A failed download is not retried by us. With `FOLLOWUP_ENABLED=true` the same
   webhook also schedules `followup.on_incoming` (remember when this number last wrote; answer `1` / `2` /
   `STOP`) and `download_all` sends a receipt or an "unusable attachment" note: see section 3f.

### 3f. Follow-up messages and calls (D-98, D-99): `followup.run` (one loop, only when `FOLLOWUP_ENABLED=true`)

```
every 60 s: run_once ─► discover ─► close_resolved ─► claim_due ─► prepare ─► (send) ─► record
```

1. **`discover`**: for people in `WHATSAPP_SENDERS` only. NEEDS_REVIEW / NEEDS_PASSWORD invoices that have waited
   the first step of `FOLLOWUP_REVIEW_LADDER` get a `follow_ups` row (kind `review`); APPROVED invoices not in Excel
   after the first step of `FOLLOWUP_EXPORT_LADDER` get kind `export`; WhatsApp-sourced invoices that were decided
   in the last 24 h get kind `result` (a receipt of the outcome). One row per (invoice, kind): the unique index
   makes it safe to run in several processes.
2. **`close_resolved`**: follow-ups whose invoice left the waiting state become `done` / `resolved`.
3. **`claim_due`**: active rows with `next_due_at <= now`, claimed with `SKIP LOCKED` and a 10-minute lease.
   Outside `FOLLOWUP_HOURS` only `result` rows are claimed. The transaction ends here.
4. **`prepare`** (reads, no sending): groups what is due by (person, channel). `whatsapp`: `compose` writes one
   message, then the daily cap and WhatsApp's 24-hour window decide *send* / *template* / *skip*. `voice`: the daily
   call cap, then a `voice_calls` row is created. No number in `WHATSAPP_SENDERS` -> `unreachable`.
5. **`_send`** (no transaction open): `outbound.send_whatsapp` or `outbound.place_call` (Twilio REST). With
   `FOLLOWUP_DRY_RUN=true` they return `dry_run` and Twilio is never touched.
6. **`record`**: one `follow_up_events` row per follow-up; WhatsApp: `step + 1`, next step at
   `started_at + minutes` (or `done` / `finished`), `awaiting_reply_since` set for `review`. A failed send retries in
   30 minutes, 3 times, then `unreachable`. A real call is left waiting for Twilio's status callback.

**A reply** (webhook `incoming_message` -> `followup.on_incoming`, background task): `1` pauses the person's
awaiting follow-ups 24 h, `2` snoozes 2 h, `STOP` closes them; anything else gets the options once per 10 minutes.
Nothing here touches the invoice.

**A call** (`app/voice.py`):

```
place_call ─► Twilio ─► POST /webhook/voice?call=<id> (signed) ─► TwiML <ConversationRelay url=wss://.../stream?...&token=...>
                       WebSocket /webhook/voice/stream: setup ─► prompt (what they said) ─► text (what we say) ... ─► end
                       POST /webhook/voice/status ─► followup.finish_call (advance the ladder, or retry once after 1 h)
```

- `stream` refuses a wrong token, a call that is not `in_progress`, and a `setup` with another call id or `CallSid`.
- `Conversation.respond` sends the model (`VOICE_MODEL`) the invoice facts from `followup.call_context` and the
  conversation; its only tool `record_outcome` -> `followup.apply_outcome` (`handling` / `remind_later` move
  reminders, the rest are only recorded). Keypad 1 / 2 do the same with no model.
- The transcript is saved when the socket closes (`GET /api/v1/voice/calls`).

## 4. The worker reads a document: `worker.run` (one loop per worker thread)

```
loop: claim_next ─► process ─► (every 10 s or when idle) export ─► (idle: wait 2 s)
```

1. **`worker.claim_next`**:
   1. `reclaim_stale`: PROCESSING rows older than `PROCESSING_TIMEOUT_MINUTES` go back to RECEIVED, or
      to FAILED after 3 attempts
   2. `SELECT … WHERE status=RECEIVED ORDER BY updated_at, id LIMIT 1 FOR UPDATE SKIP LOCKED`
   3. sets status to PROCESSING, `attempts += 1`, then commits
2. **`worker.process(invoice_id)`**:
   1. remembers the claim `(attempts, updated_at)`
   2. `gmail.email_hints`: subject, sender, body start, the usual supplier of this sender, and
      `learning.correction_notes` for that supplier
   3. `known_passwords` (`PDF_PASSWORDS` plus passwords saved before), then **commit**, so no
      transaction stays open during the AI call
   4. **`reading.read_document(file, name, hints, passwords, extract_fn, supplier_notes)`**:
      1. **`extract.extract_invoice`**:
         - `prepare_pages`, under `PDF_LOCK`: renders up to 5 pages to PNG and reads the text layer.
           `PasswordRequired` if no password opens the PDF. Excel, CSV and Word files contribute text only.
         - picks the agent (scan → `scan_extractor`)
         - builds the prompt: images, text layer, feedback (second reading only), email hints
         - `agent.run(...)` → OpenAI → an `ExtractedInvoice`
         - not an `ExtractedInvoice`: a failed run that looks transient raises `AIServiceUnavailable`;
           anything else raises `ValueError`
      2. **`einvoice.check_document`**: only when an IRN is shown. `find_qr_token` renders the corners
        (under `PDF_LOCK`, one render at a time) and decodes them with OpenCV (zxing-cpp if
        installed). `parse_token` reads the JWT and verifies it with NIC's key if configured (the
        payload can't set `verified` itself). `apply`: a **verified** QR replaces the AI's values; an
        unverified one only compares (a difference is a FAIL with both values, D-82); a malformed one is a
        FAIL, never a crash. Result: the `einvoice_qr` check.
      3. **`reading.fixable_problems`**: runs `validate.validate` offline and keeps only the FAILs a
        misreading can explain
      4. if any remain: `supplier_notes(gstin)` → `extract_invoice(model = AI_SECOND_PASS_MODEL,
        feedback = the failed checks + notes)` → QR applied again → `_escapes_checks` (a reading that
        changed the document type, currency or a sound GSTIN is ignored) → the reading with fewer problems
        wins → if it changed amounts, `_total_in_text` must find the new total in the document's text,
        otherwise WARN, so the invoice goes to review (D-83). Result: the `second_reading` check.
   5. **`worker._still_ours`**: `SELECT … FOR UPDATE`. If the claim changed (reclaimed by another
      worker), the result is dropped.
   6. audits for the QR and the second reading; `pages_check` (WARN if more than 5 pages)
   7. **`evaluate.evaluate(session, inv, data, extra_checks)`**:
      1. `store`: `validate.normalize_extracted` (GSTIN cleanup, state code "7" → "07"…), saves
         `extracted` plus the columns used for search and duplicates
      2. journal voucher → `_evaluate_journal` → `journal.check_journal`: linked to its invoice
         (DUPLICATE) or NEEDS_REVIEW
      3. invoice or invoice-like:
         - **`validate.validate`**:
           - required fields, confidence, GSTIN check digit, billed to `COMPANY_GSTIN` (or the same PAN)
           - CGST+SGST vs IGST by place of supply, `arithmetic_checks`, amount in words, total > 0, date
           - duplicates: under an advisory lock keyed on supplier + number, same financial year; the same
             date and amount make it a DUPLICATE
           - look-alike numbers (`_look_alike_numbers`)
         - `journal.check_invoice_against_journals`
         - **`decide.decide`**:
           - DUPLICATE
           - NOT_INVOICE
           - NEEDS_REVIEW (any FAIL or WARN, credit/debit notes, foreign currency, special kinds, or over
             `AUTO_APPROVE_LIMIT`)
           - otherwise **APPROVED** (`approved_by = "auto"`)
         - `journal.link_waiting_journals`
      4. not an invoice → NOT_INVOICE
      5. audit "decide"
   8. commit, then `_ai_ok()` clears any AI problem shown in `GET /system/status`. On exceptions:
      - `PasswordRequired` → NEEDS_PASSWORD (the attempt is refunded)
      - `AIAccountProblem` (no credits, invalid key) → back to RECEIVED (the attempt is refunded), with the error
        "Waiting: the OpenAI account…"; `worker.ai_status` is set and the worker pauses 5 minutes (D-90)
      - `AIServiceUnavailable` → back to RECEIVED (the attempt is refunded), and the worker pauses 30 s
      - anything else → RECEIVED again, or FAILED after 3 attempts
3. **`worker.export` → `excel.try_export` → `excel.export_pending`**. This happens only in the API process
   (`run(write_excel=True)`, or the `excel-export` thread when `RUN_WORKER=false`); worker processes on
   other PCs never export (D-86):
   1. `pg_advisory_xact_lock(EXCEL_LOCK)`, then select every APPROVED invoice
   2. `_open_workbook` (`PermissionError` → `ExcelLocked`: retried later; the reason is shown on the
      invoices)
   3. skips invoices whose (id, GSTIN, invoice number) is already a row (the id alone isn't enough after
      a database reset, D-88); `_rows` (signs, dates, `_safe` cell text); `_append`
   4. saves to a temp file → `os.replace` → marks the invoices **EXPORTED**, audit "export", commit

## 5. A person reviews: `PATCH / approve / reject / reprocess / process-as-invoice / password`

Every one of these starts with `api._locked_invoice` (`SELECT … FOR UPDATE`), so a second click on the
same invoice waits, then sees the new status and gets 409. approve, reject, reprocess and
process-as-invoice also take `if_unchanged_since` (`api._unchanged`): an assistant's saved suggestion is
refused with 409 if the invoice changed after it was made (D-87).

- **PATCH** (`api.correct`): status must be NEEDS_REVIEW or DUPLICATE.
  1. merges the changes with `schemas.from_stored` (confidence set to 1.0)
  2. audit "correct" (before → after: this feeds `learning.correction_notes`)
  3. `evaluate(after_correction=True)`, so a corrected invoice is never auto-approved and stays
     NEEDS_REVIEW, "ready to approve". `_carried_checks` keeps the page check and compares the e-invoice
     QR (from the audit log) with the corrected values again via `einvoice.recheck` (D-84).
- **approve**: `_transition(NEEDS_REVIEW → APPROVED)`, commit, then `try_export` immediately (section 4.3)
- **reject**: sets REJECTED; invoices that were DUPLICATE of this one go back to NEEDS_REVIEW
- **reprocess**: sets RECEIVED with `attempts = 0`, so the worker reads it again (section 4)
- **password**:
  1. `authenticate` under `PDF_LOCK`
  2. upserts into `pdf_passwords`
  3. NEEDS_PASSWORD → RECEIVED

## 6. The assistant answers: `POST /api/v1/assistant/sessions/{id}/stream`

1. `current_user` (commits) → **`run_in_threadpool(_start_turn)`**:
   1. ownership check (`chat_store.get_owned`)
   2. `add_message(user)`; the chat title comes from the first question
   3. `chat_store.history`
2. A `StreamingResponse` wraps the async generator `events()`:
   1. `async for ev in assistant.stream(convo)`:
      - `assistant._question` checks the history (the last 20 messages) and that it ends with a user message
      - `assistant._agent()` gives the cached agent for **today's date**
      - `.arun(stream=True)` makes async OpenAI calls. Tools are sync functions in `assistant_tools`,
        each with its own `SessionLocal`; Agno runs them through `asyncio.to_thread`.
      - events: `ToolCallStarted` → `{"type":"tool"}`, `RunContent` → `{"type":"delta"}`, `RunError` →
        an exception. At the end, `_done` → `_tool_outputs` (files and proposals).
   2. on `done`: `run_in_threadpool(_save_turn, assistant message + meta)` → `{"type":"saved"}`
   3. on an error: `_save_turn(error)` → `{"type":"error"}`
   4. if the browser leaves: `CancelledError` → a thread saves the partial answer
3. Frontend: `assistant.js send()` reads the stream (`res.body.getReader()`) and `handleEvent` →
   `renderSoon`. A proposal button calls the normal invoice API (section 5), then
   `/assistant/messages/{id}/actions/{k}/done`.

`POST …/messages` (not streamed) and `POST /assistant/chat` (not saved) follow the same path with
`await assistant.chat()`.

Assistant file exports: `assistant_tools.export_invoices` queries invoices, builds invoice and line-item
rows, then applies `excel._safe` to every value before writing XLSX (both sheets) or CSV. Text with
formula prefixes, including after leading whitespace, gets an apostrophe; illegal control characters
are removed. Numbers remain numeric and stored invoice data is unchanged (D-54, 2026-10-16).

## 7. Summary report: thread `reports` → `reports.run`

Every 60 s:
1. `_clear_stuck`: runs left in "sending" for more than 30 minutes become failed
2. **`run_due`**:
   1. `due_slot(now)`, e.g. `weekly:2026-10-19`, from 09:00 onwards
   2. if no run already holds that slot and no failure happened in the last 30 minutes: **`send`**
3. `send`:
   1. inserts a `ReportRun(slot, status="sending")`; the unique slot means only one process sends it
   2. `default_sender` and `default_recipients`
   3. `build` (SQL) → `render` (escaped HTML) → `_send` → `gmail.send_email` (Composio
      `GMAIL_SEND_EMAIL`, never retried automatically)
   4. status sent, or failed with the slot renamed `slot#failed-id` so it can be retried

Recipients come only from `REPORT_RECIPIENTS`. With none set, the schedule waits (one warning) and
hand-sent reports are refused (D-85).

By hand: `POST /api/v1/reports/send` (recipients must be in `REPORT_RECIPIENTS`) → `send(slot=None)`.

## 8. Demo frontend → API

1. The browser opens `server.py` on port 5173; `Handler._route` serves `public/` or calls `_proxy`.
2. `_proxy` forwards the method, path, body and `X-User-Id` to the backend (600 s timeout).
   `text/event-stream` responses go to `_pass_stream`, chunk by chunk, with no timeout.
3. `public/app.js`:
   - `api()` adds `X-User-Id` from `state.userId`
   - `route()` picks the screen from the URL hash
   - every 3 s `loadInvoices` runs (sequence-guarded), and every 30 s `loadUsers` runs (which also
     drives the "Reconnect Gmail" banner)
4. The screens are `assistant.js` (sections 6 and 5), `reports.js` (section 7), `excel.js` (`GET /excel`,
   edits kept in the browser only) and the Gmail screen (section 3d).

## 9. Who holds which lock

| Lock | Kind | Held by | Protects |
|---|---|---|---|
| invoice row `FOR UPDATE SKIP LOCKED` | Postgres row | `worker.claim_next` | one worker per document |
| invoice row `FOR UPDATE` | Postgres row | `api._locked_invoice`, `worker._still_ours` | one decision at a time |
| `pg_advisory_xact_lock(EXCEL_LOCK)` | Postgres advisory | `excel.export_pending` | one workbook writer |
| `pg_advisory_xact_lock(DUPLICATE_LOCK, hash(supplier/number))` | Postgres advisory | `validate._validate` | two copies at once can't both pass |
| journal lock | Postgres advisory | `journal._lock` | journal ↔ invoice linking |
| unique `report_runs.slot` | Postgres constraint | `reports.send` | one scheduled report per slot |
| `PDF_LOCK` | Python RLock | every PyMuPDF call | MuPDF isn't thread-safe |
| `_sync_locks[user]` | Python Lock | `gmail.sync_user` | one sync per inbox |
| `_one_at_a_time(user, message)` | Python Lock | `gmail.handle_message` | one thread per email |
| `_activate_locks[user]` | Python Lock | `gmail.activate` | callback vs watcher |

The Python locks are per process. That is one reason the API must run as one process (D-11).

---

## You are here (2026-10-29, email follow-ups added)

### Email follow-ups (D-100; 417 tests pass)

- A ladder step can be `email@minutes`: §3f sends it with `gmail.send_email` from a connected Gmail
  (`FOLLOWUP_EMAIL_SENDER_USER_ID`, else the report's sender) to the owner's `users.email`, only if
  `FOLLOWUP_EMAIL_TO` lists it. No Twilio needed, so this is the channel that works while the Twilio account is
  a trial.
- A step on a channel the person can't be reached on is now skipped and the ladder moves on (before: closed).
- The Gmail listener (§3b, `_ingest_email`) skips our own reminders by a keyed marker at their foot, so a
  reminder is never read as an invoice in the body.
- Since 2026-10-28, found with real Twilio: a trial account refuses some call parameters (fallback in
  `place_call`), fetches the answer URL unsigned (so the call's TwiML is now sent with the call; a trial refuses
  that too, so calls stay blocked until the account is upgraded), and WhatsApp needs the sandbox re-joined. The
  live call transcript is at `GET /api/v1/voice/calls/{id|latest}/live`. Tests pin every follow-up setting.

## Earlier: 2026-10-28, follow-up WhatsApp messages and calls added

### Follow-ups (D-98, D-99; 396 tests pass)

- New loop §3f (`followup.run`, off by default; `FOLLOWUP_DRY_RUN=true` records what would be sent and sends
  nothing). It reminds the owner of an invoice waiting for review (WhatsApp at 4 h and 24 h, an AI phone call at
  48 h), tells a person whose approved invoice is not in Excel, and tells a WhatsApp sender what became of their
  file. Reply `1` / `2` / `STOP` moves reminders only.
- The call is an AI conversation (Twilio ConversationRelay) whose only tool records one of five outcomes.
- New tables: `follow_ups`, `follow_up_events`, `voice_calls`, `whatsapp_sessions`. New files: `app/followup.py`,
  `app/outbound.py`, `app/voice.py`. New endpoints: `/api/v1/followups`, `/followups/events`,
  `/followups/{id}/stop`, `/voice/calls`, and Twilio's `/api/v1/webhook/voice` (+ `/status`, `/stream`).
- **Not tried with real Twilio yet.** Tests fake Twilio and the model. Before going live: dry-run first, then one
  person, one call; accept the AI/ML addendum; check the language and Indian calling rules (D-99).
- Not on the path: roles (reminders go to the invoice owner), a WhatsApp inbox log, PO/GRN follow-ups,
  automatic retry of a failed media download.

## Earlier: 2026-10-28, WhatsApp channel added

### WhatsApp through Twilio (D-97; 341 tests pass)

- New entry path §3e: Twilio webhook → signature → sender mapping → media download after the reply →
  `ingest_document`. Everything after ingest (§4, §5, Excel) is unchanged.
- Ported unchanged from the earlier Twilio build (`Terasoft/app/whatsapp.py`); wired in by
  `app.include_router(whatsapp.router)` and three settings (`TWILIO_AUTH_TOKEN`, `TWILIO_ACCOUNT_SID`,
  `WHATSAPP_SENDERS`).
- Not on the path yet: replies or status messages to the sender, a WhatsApp message log, retrying a
  failed download, and .zip/.eml over WhatsApp.

## Earlier: 2026-10-26, after the workbook split

### Workbook per financial year (D-96; 231 tests pass)

- §4.3 export: `excel.export_pending` groups the APPROVED invoices by `workbook_for` (financial year of the
  invoice date) and writes each group with `_write_workbook` into `invoices FY<year>.xlsx`. A workbook open
  in Excel only holds back its own year (`ExcelLocked` raised after the others are committed).
- Download: `GET /api/v1/excel[?year=2026-27|all]`, `GET /api/v1/excel/workbooks`; the assistant's
  `read_excel(year=...)`.
- Real run: 2 users × 9 scenarios with the real AI, 18/18 expected outcomes, all in `invoices FY2026-27.xlsx`.
- This removes the main scale limit from D-95 until a single year gets very busy; the full fix is still
  Phase 5 (database register).

## Earlier the same day: review of D-92…D-94 with real runs and load tests

### Review and load test (D-95; 226 tests pass)

- Real runs: golden set 100% key fields, 0 wrong auto-approvals; 2 simulated users × 9 scenarios through
  the API with the real AI: 18/18 expected outcomes, read in 40 s.
- Changes on the paths:
  - §4.2.4 `extract.extract_invoice` drops an HSN summary the text doesn't show (the AI invents one).
  - `reading` won't accept a second reading that only rewrites or drops the summary.
  - §4.2.6 `validate._look_alike_numbers` and `supplier.profile` run in SQL.
  - §4.3 `excel.export_pending(wait=False)` for workers and Approve; `_append` no longer rescans the sheet.
  - The assistant's summary tools read only the columns they need.
- Measured at 50,000 invoices (20,000 from one supplier):

  | Path | Before | After |
  |---|---|---|
  | Supplier check, per document | 4.7 s | 0.07 s |
  | Duplicate + look-alike check, per document | 4.6 s | 0.09 s |
  | First Excel export of 1,000 invoices | 323 s | 8 s |
  | Assistant statistics / supplier summary | ~43 s, ~1 GB | 1–4 s, < 60 MB |

- Weak spot left on the path: §4.3. Every export loads and saves the whole workbook, 37 s per export at
  3,000 invoices and growing. Needs the D-01 decision: workbook per financial year, or database register.

### Roadmap items built earlier today (D-92, D-93, D-94)

Three additions on the paths above:

- §4.2.4 reading: the AI now also returns the HSN-wise tax summary. `validate.arithmetic_checks` adds an
  `hsn_summary` check (summary vs lines vs totals).
- §4.2.1 page choice: `extract.prepare_pages` uses `select_pages` for PDFs over `MAX_PAGES`: first page,
  last page and the pages with most amounts. The `pages` check says which pages were read.
- §4.2.6 checks: `evaluate.evaluate` also calls `supplier.check_supplier`, which compares the invoice with
  the supplier's approved invoices (name on the GSTIN, amount far above the usual). New API:
  `GET /api/v1/suppliers`.
- Still open from the roadmap: the ones that need you (GSP account, PO/GRN source, OCR service, hosting,
  login/multi-company decisions). Edits with an audit trail already existed (`PATCH /invoices/{id}`).

(The 2026-10-16 audit status follows, unchanged.)

## 2026-10-16, after audit fix batch 1

### What has happened

- **Audit finished, in two passes.** The second pass confirmed **47 findings** (2 high, 15 medium,
  30 low; several are duplicates seen from different angles). The completeness critic and the final
  synthesized report didn't run (usage limit), so this section is the working list.
- **Batch 1 fixed** (D-54, D-80…D-89; 209 tests pass, full suite):
  - assistant export formula protection (D-54, done by another session and verified here)
  - zip bombs: one unpacking budget per upload or attachment (§2, §3a) (D-80)
  - signature logos no longer hide an invoice written in the email body (§3a) (D-81)
  - e-invoice QR (§4.2.4) (D-82):
    - an unverified QR only confirms the reading;
    - a forged `"verified"` field and malformed QRs are handled.
  - second reading (§4.2.4) (D-83):
    - it can't escape the checks;
    - changed amounts must appear in the document's text.
  - corrections keep the QR and page checks (§5 PATCH) (D-84)
  - report recipients only from `REPORT_RECIPIENTS` (§7) (D-85)
  - only the API process writes Excel (§4.3) (D-86)
  - assistant proposals carry a version, so a stale button gets 409 (§5, §6) (D-87)
  - workbook rows recognised by id + GSTIN + number, so a database reset doesn't lose rows (§4.3) (D-88)
  - render-size cap, journals matched within the same supplier, requirements.txt complete (D-89)

### Where the remaining findings sit on the paths above (⚠ = still to fix, all low unless marked)

- **§2 upload / §3b webhook**: ⚠ the request body is read with no size limit before the checks (upload is
  spooled to disk, the webhook body goes into memory).
- **§3d Gmail routes**: ⚠ connect, activate, sync, reconcile and check need no `X-User-Id`; each connect
  starts a 15-minute watcher thread.
- **§3 Gmail**: ⚠ the default query includes Spam, removing Gmail's phishing filter in front of auto-approval.
- **§4.1 / §4.2 worker**:
  - ⚠ the AI-down requeue has no upper bound
  - ⚠ `PROCESSING_TIMEOUT_MINUTES` (15) is shorter than the worst AI call
  - ⚠ shutdown consumes an attempt
  - ⚠ sender-controlled file names go into logs raw
- **§4.2.7 checks**: ⚠ `round_off` is unbounded, so any gap can hide in it. ⚠ Learning notes feed outside
  text back into prompts (no length/character limit).
- **§4.3 Excel**: ⚠ a corrupt workbook blocks every export; content users add is dropped on re-save.
- **§7 reports**:
  - ⚠ an ambiguous Gmail failure is re-sent
  - ⚠ credit-note sign in the report totals
  - ⚠ outside text in the report HTML (escaped, but phishing-like wording)
  - ⚠ manual reports label periods in UTC
- **§6 assistant**:
  - ⚠ export downloads have no owner or expiry, and no size cap
  - ⚠ raw exception text is returned
  - ⚠ totals mix currencies and credit-note signs
  - ⚠ questions are cut at 4,000 characters
  - ⚠ deleting a chat while `/messages` saves gives a 500
- **§8 demo frontend and proxy**:
  - ⚠ (medium) any Host header is accepted, so DNS rebinding is possible
  - ⚠ (medium) several files picked: only the first is uploaded
  - ⚠ no frame protection
  - ⚠ `/api/` links in answers
  - ⚠ PDF passwords shown in the request log
  - ⚠ no SRI on ExcelJS
  - ⚠ Excel edits shared between users
  - ⚠ zip/eml results misreported
  - ⚠ stale state after a user switch
  - ⚠ Approve on values the reviewer never saw
  - ⚠ an empty "Send now" prompt
- **Tooling**:
  - ⚠ `setup_db --reset` drops the schema without confirmation
  - ⚠ stored file paths are relative to the working directory
  - ⚠ evaluation predicts decisions without the QR and page checks
- **Accepted by design (not fixed, documented)**: a self-consistent forged invoice of ₹50,000 or less from an
  outside sender can still be auto-approved (D-02). The real fix is the Matching Engine (PO/GRN) or
  approval only for known suppliers. That needs a business decision.

### Testing the paths (2026-10-16)

- `scripts/make_test_invoices.py` builds documents that enter at §2 (upload) with a known outcome at §4.2.7
  (decide). `scripts/simulate_users.py` drives §2 for N users at once and reads the results back.
- `GET /api/v1/system/status` (new) reports the AI account state, from `worker.ai_status` and from queued
  documents waiting on the account, plus the queue sizes. The demo's banner polls it every 30 s.
- Seen in the real database today: the OpenAI account has **no credits**. 5 test invoices are FAILED and 1
  is RECEIVED from before D-90. After adding credits, press "Run AI again" on the FAILED ones.

### Next on the path

1. Batch 2: the two medium frontend/proxy findings (Host check, multi-file upload), then upload and
   webhook body limits, the Gmail route identity, and the worker retry and timeout findings.
2. Batch 3: the remaining low findings.
3. Then roadmap Phase 2, items 5 and 6 (business decisions), and the Matching Engine (needs the PO/GRN
   source).
