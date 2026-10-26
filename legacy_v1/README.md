# Invoice AP Automation — starter

Gmail + upload ingestion -> Agno/OpenAI extraction -> GST & duplicate validation
-> PO matching -> rule-based decision (AUTO_POST / PENDING_APPROVAL / EXCEPTION)
-> posting to Markdown ledger files (no ERP needed).

## Run locally

Needs Python 3.11+ and a local PostgreSQL install (no Docker).

    pip install -r requirements-dev.txt
    python -m scripts.setup_db

`setup_db` asks for the password of the `postgres` superuser (the one you chose when
installing PostgreSQL). It then creates the `ap` user (password `ap`), the `ap` and
`ap_test` databases, a `.env` file, the tables and a sample supplier and PO. You can
run it again safely. Then put your real `OPENAI_API_KEY` and `COMPANY_GSTIN` in `.env`.

Then start three processes in separate terminals:

    uvicorn app.api:app --reload            # API at http://localhost:8000/docs
    python -m app.worker                    # processing pipeline
    python -m app.ingest.gmail_poller       # optional, needs Gmail setup below

Try it:

    curl -F "file=@invoice.pdf" localhost:8000/invoices/upload
    curl localhost:8000/invoices/1

## Tests

    pytest

The tests use the `ap_test` database (they empty it each run, so never point them at
`ap`) and fake the OpenAI and Gmail calls, so no API keys are needed. For a different
database, set `TEST_DATABASE_URL` first:

    set TEST_DATABASE_URL=postgresql+psycopg://ap:ap@localhost:5432/ap_test       (cmd)
    $env:TEST_DATABASE_URL = "postgresql+psycopg://ap:ap@localhost:5432/ap_test"  (PowerShell)

## Ledger files (posting)

AUTO_POST invoices are posted by the worker as user `system`. PENDING_APPROVAL
invoices are posted when approved, under the reviewer's name. Each posting is a
Tally-style purchase voucher (Dr Purchase + Input GST, Cr supplier) and is added to:

    ledgers/daybook.md               every voucher
    ledgers/suppliers/<gstin>.md     one party ledger per supplier
    ledgers/users/<user>.md          one book per user who posted ("system" = auto-post)

The invoice then moves to POSTED. Posting can be retried safely; a voucher is never
written twice. View the files directly or via the API:

    curl -X POST localhost:8000/invoices/3/approve -H "Content-Type: application/json"          -d '{"reviewer": "priya", "note": "ok"}'
    curl localhost:8000/ledgers                 # which books exist
    curl localhost:8000/ledgers/users/priya
    curl localhost:8000/ledgers/suppliers/36aaacs1234a1z3
    curl localhost:8000/ledgers/daybook

If a ledger write fails, the invoice stays AUTO_POST/APPROVED with `error` set;
retry with `POST /invoices/{id}/post`.

## Gmail setup

1. Google Cloud Console -> new project -> enable "Gmail API".
2. OAuth consent screen -> External/Internal, add your inbox as a test user.
3. Credentials -> OAuth client ID -> "Desktop app" -> download as `credentials.json`
   into the project root.
4. First run of the poller opens a browser for consent and saves `token.json`.
5. Narrow `GMAIL_QUERY` in `.env`, e.g. `to:invoices@yourco.com has:attachment -label:ap-processed`.

## Layout

    app/ingest/common.py     one ingest function for all channels (hash, store, record)
    app/ingest/gmail_poller.py
    app/api.py               upload, list, detail, approve/reject/reprocess
    app/worker.py            Postgres-backed queue (SKIP LOCKED)
    app/pipeline/extract.py  Agno agent (the only LLM step)
    app/pipeline/validate.py GSTIN checksum, CGST/SGST vs IGST, arithmetic, duplicates
    app/pipeline/match.py    supplier, PO, line price/qty (qty counts earlier invoices on the PO)
    app/pipeline/decide.py   versioned rules
    app/pipeline/post.py     AUTO_POST/APPROVED -> voucher -> POSTED
    app/ledger.py            Markdown day book / supplier / user ledger files
    tests/                   pytest suite (Postgres + fake extractor)

Several workers can run at once: the queue uses SKIP LOCKED, and duplicate and
PO-quantity checks take Postgres advisory locks. An invoice stuck in PROCESSING for
more than `PROCESSING_TIMEOUT_MINUTES` (default 15) is put back in the queue.
