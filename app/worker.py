"""Pulls RECEIVED invoices and runs: extract -> validate -> decide, then writes
APPROVED invoices to the Excel workbook.

Started automatically by the API (app/background.py). Uses Postgres as the queue
(FOR UPDATE SKIP LOCKED), so extra workers can run safely: python -m app.worker
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.excel import try_export
from app.logs import setup_logging, timed
from app.gmail import email_hints
from app.models import Invoice, PdfPassword, Status, audit
from app.pipeline.learning import correction_notes
from app.pipeline.reading import read_document
from app.pipeline.evaluate import evaluate, pages_check
from app.pipeline.extract import AIAccountProblem, AIServiceUnavailable, PasswordRequired, extract_invoice, page_count

log = logging.getLogger("worker")
MAX_ATTEMPTS = 3
AI_DOWN_PAUSE_SECONDS = 30   # wait this long before the next invoice when the AI service is down
AI_ACCOUNT_PAUSE_SECONDS = 300   # no credits / invalid key: a person has to fix the account first

# Shown by GET /api/v1/system/status (this process): why documents aren't being read right now
ai_status: dict = {"problem": None, "kind": None, "since": None, "last_ok_at": None}


def _ai_problem(kind: str, message: str) -> None:
    if ai_status["kind"] != kind:
        ai_status["since"] = datetime.now(timezone.utc)
    ai_status.update(problem=message[:300], kind=kind)


def _ai_ok() -> None:
    if ai_status["problem"]:
        log.info("AI service works again")
    ai_status.update(problem=None, kind=None, since=None, last_ok_at=datetime.now(timezone.utc))


def reclaim_stale() -> None:
    """Invoices left in PROCESSING by a crashed worker go back to the queue (or FAILED)."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.processing_timeout_minutes)
    with SessionLocal() as session:
        stale = session.scalars(
            select(Invoice).where(Invoice.status == Status.PROCESSING, Invoice.updated_at < cutoff)
            .with_for_update(skip_locked=True)
        ).all()
        for inv in stale:
            inv.status = Status.RECEIVED if inv.attempts < MAX_ATTEMPTS else Status.FAILED
            inv.error = "Worker stopped while processing"
            audit(session, inv.id, "error", f"Stale PROCESSING after attempt {inv.attempts} -> {inv.status}")
            log.warning("Invoice %s was stuck in PROCESSING -> %s", inv.id, inv.status)
        session.commit()


def claim_next() -> int | None:
    reclaim_stale()
    with SessionLocal() as session:
        inv = session.scalar(
            select(Invoice).where(Invoice.status == Status.RECEIVED)
            .order_by(Invoice.updated_at, Invoice.id).limit(1).with_for_update(skip_locked=True)
        )
        if not inv:
            return None
        inv.status = Status.PROCESSING
        inv.attempts += 1
        session.commit()
        log.info("Invoice %s: processing '%s' (attempt %d of %d)", inv.id, inv.original_filename,
                 inv.attempts, MAX_ATTEMPTS)
        return inv.id


def known_passwords(session) -> list[str]:
    """PDF_PASSWORDS from .env, then passwords that opened protected PDFs before (most recent first)."""
    configured = [p.strip() for p in settings.pdf_passwords.split(",") if p.strip()]
    saved = session.scalars(select(PdfPassword.password).order_by(PdfPassword.last_used_at.desc())).all()
    return list(dict.fromkeys(configured + list(saved)))


def _still_ours(session, invoice_id: int, claim: tuple) -> Invoice | None:
    """The invoice, locked, if this worker's claim still holds. An AI call that took longer than
    PROCESSING_TIMEOUT_MINUTES may have been handed to another worker meanwhile: then this
    result is dropped instead of overwriting that one."""
    inv = session.scalar(select(Invoice).where(Invoice.id == invoice_id).with_for_update()
                         .execution_options(populate_existing=True))
    if inv is None or inv.status != Status.PROCESSING or (inv.attempts, inv.updated_at) != claim:
        log.warning("Invoice %s was taken over while the AI was reading it (now %s); result dropped",
                    invoice_id, inv.status if inv else "deleted")
        session.rollback()
        return None
    return inv


def _notes(session, gstin: str) -> str | None:
    """Reviewers' corrections for this supplier; the transaction is closed again before the AI call."""
    try:
        return correction_notes(session, gstin)
    finally:
        session.commit()


def process(invoice_id: int) -> bool:
    """Process one invoice. Returns False when the AI service is unavailable (the invoice
    goes back to the queue without using up an attempt, and the worker should pause)."""
    with SessionLocal() as session, timed(log, f"Invoice {invoice_id} processing", logging.INFO):
        inv = session.get(Invoice, invoice_id)
        claim = (inv.attempts, inv.updated_at)
        try:
            hints = email_hints(session, inv)
            passwords = known_passwords(session)
            file_path, name = inv.file_path, inv.original_filename
            session.commit()   # no database transaction held open during the AI call
            reading = read_document(file_path, name, hints=hints, passwords=passwords,
                                    extract_fn=extract_invoice,   # extract_invoice: replaceable in tests
                                    supplier_notes=lambda gstin: _notes(session, gstin))
            data = reading.data
            inv = _still_ours(session, invoice_id, claim)
            if inv is None:
                return True
            audit(session, inv.id, "extract", f"Extracted: {data.document_type}",
                  {"confidence": data.confidence, "model": data._model or settings.openai_model,
                   "email_hints": bool(hints)})
            if reading.qr:
                audit(session, inv.id, "einvoice", "E-invoice QR read", {k: v for k, v in reading.qr.items() if k != "kid"})
            if reading.second_pass:
                audit(session, inv.id, "extract", "Second reading: " + (
                    f"fixed {', '.join(reading.second_pass['fixed'])}" if reading.second_pass["fixed"]
                    else "same problems, first reading kept"), reading.second_pass)
            total_pages = data._usage.get("pages") or page_count(inv.file_path)
            pages = pages_check(total_pages, settings.max_pages, data._usage.get("read_pages"))
            evaluate(session, inv, data, extra_checks=pages + reading.checks)
            _ai_ok()
            inv.error = None
            session.commit()
            return True
        except PasswordRequired:
            # Not a failure: a person enters the password (POST /invoices/{id}/password)
            session.rollback()
            inv = _still_ours(session, invoice_id, claim)
            if inv is None:
                return True
            inv.attempts = max(0, inv.attempts - 1)
            inv.status = Status.NEEDS_PASSWORD
            inv.status_reason = "The PDF is password-protected: enter its password to read it"
            inv.error = None
            audit(session, inv.id, "extract", "Password-protected PDF; waiting for a password")
            session.commit()
            log.warning("Invoice %s is a password-protected PDF; waiting for a password", invoice_id)
            return True
        except AIServiceUnavailable as e:
            # Internet or OpenAI down: not the document's fault, so it doesn't count as an attempt
            session.rollback()
            inv = _still_ours(session, invoice_id, claim)
            if inv is None:
                return True
            inv.attempts = max(0, inv.attempts - 1)
            inv.status = Status.RECEIVED
            account = isinstance(e, AIAccountProblem)
            inv.error = (f"Waiting: the OpenAI account can't be used ({e})" if account
                         else f"Waiting for the AI service: {e}")[:2000]
            session.commit()
            _ai_problem("account" if account else "unavailable", str(e))
            log.warning("Invoice %s back in the queue: %s; pausing %ss", invoice_id, e,
                        AI_ACCOUNT_PAUSE_SECONDS if account else AI_DOWN_PAUSE_SECONDS)
            return False
        except Exception as e:
            session.rollback()
            inv = _still_ours(session, invoice_id, claim)
            if inv is None:
                return True
            inv.error = str(e)[:2000]
            inv.status = Status.RECEIVED if inv.attempts < MAX_ATTEMPTS else Status.FAILED
            audit(session, inv.id, "error", f"Attempt {inv.attempts} failed: {e}")
            session.commit()
            log.exception("Invoice %s attempt %d failed -> %s", invoice_id, inv.attempts, inv.status)
            return True


def export_loop(stop: threading.Event) -> None:
    """In the API process when its workers run elsewhere (RUN_WORKER=false): write approved
    invoices to Excel every 10 seconds."""
    while not stop.is_set():
        try:
            export()
        except Exception:
            log.exception("Excel export loop error; retrying")
        stop.wait(10)


def export(wait: bool = False) -> None:
    """Worker threads don't wait for an export another thread is running (it writes theirs too)."""
    with SessionLocal() as session:
        try_export(session, wait)


def run(stop: threading.Event, write_excel: bool = True) -> None:
    """Worker loop until `stop` is set. Runs inside the API process (app/background.py)
    or on its own via `python -m app.worker`. Only the API process writes the Excel workbook
    (`write_excel`): a worker on another PC would otherwise write into its own local copy."""
    log.info("Worker started")
    last_export = 0.0
    while not stop.is_set():
        try:
            invoice_id = claim_next()
            if invoice_id is not None and not process(invoice_id):
                # AI down / account unusable: don't burn through the queue
                stop.wait(AI_ACCOUNT_PAUSE_SECONDS if ai_status["kind"] == "account" else AI_DOWN_PAUSE_SECONDS)
            # Also retries invoices that waited because the workbook was open in Excel
            if write_excel and (invoice_id is None or time.monotonic() - last_export > 10):
                export()
                last_export = time.monotonic()
        except Exception:
            log.exception("Worker loop error (database down?); retrying in 5s")
            stop.wait(5)
            continue
        if invoice_id is None:
            stop.wait(2)
    log.info("Worker stopped")


def main():
    """Worker process on its own (the API already runs WORKER_CONCURRENCY workers): also reads
    WORKER_CONCURRENCY documents at a time. Several processes, even on several PCs, can share
    one database: each document is still read once."""
    setup_logging()
    Base.metadata.create_all(engine)
    stop = threading.Event()
    # Approved invoices are written to Excel by the API process (its workers or its export loop)
    threads = [threading.Thread(target=run, args=(stop, False), name="worker" if i == 0 else f"worker-{i + 1}",
                                daemon=True) for i in range(max(1, settings.worker_concurrency))]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join(30)


if __name__ == "__main__":
    main()
