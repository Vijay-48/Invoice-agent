"""Pulls RECEIVED invoices and runs: extract -> match -> validate -> decide -> post.

Uses Postgres as the queue (FOR UPDATE SKIP LOCKED), so you can run several
workers safely without Redis/Celery. Run: python -m app.worker
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import Invoice, Status, audit
from app.pipeline.checks import Check, WARN
from app.pipeline.decide import decide, RULES_VERSION
from app.pipeline.extract import extract_invoice, page_count
from app.pipeline.match import match
from app.pipeline.post import post_invoice
from app.pipeline.validate import validate, normalize_invoice_no, normalize_extracted, parse_date

log = logging.getLogger("worker")
MAX_ATTEMPTS = 3


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


def _fit(value: str | None, size: int) -> str | None:
    """Values that don't fit a column are stored as NULL; the checks still see the real value."""
    return value if value is not None and len(value) <= size else None


def claim_next() -> int | None:
    reclaim_stale()
    with SessionLocal() as session:
        inv = session.scalar(
            select(Invoice).where(Invoice.status == Status.RECEIVED)
            .order_by(Invoice.created_at).limit(1).with_for_update(skip_locked=True)
        )
        if not inv:
            return None
        inv.status = Status.PROCESSING
        inv.attempts += 1
        session.commit()
        return inv.id


def process(invoice_id: int):
    with SessionLocal() as session:
        inv = session.get(Invoice, invoice_id)
        try:
            data = normalize_extracted(extract_invoice(inv.file_path))
            inv.extracted = data.model_dump()
            inv.supplier_gstin = _fit(data.supplier_gstin, 15)
            inv.invoice_no = data.invoice_number[:100]
            inv.invoice_no_norm = normalize_invoice_no(data.invoice_number)[:100]
            inv.invoice_date = data.invoice_date if parse_date(data.invoice_date) else None
            inv.grand_total = data.grand_total
            audit(session, inv.id, "extract", "Extracted invoice data", {"confidence": data.confidence})
            session.flush()

            # Validation first: duplicates/GST failures are cheap to detect
            checks = validate(session, inv, data)
            match_checks, supplier, po = match(session, data, inv)
            checks += match_checks
            inv.supplier_id = supplier.id if supplier else None
            inv.po_id = po.id if po else None
            pages = page_count(inv.file_path)
            if pages > settings.max_pages:
                checks.append(Check("pages", WARN, f"Only the first {settings.max_pages} of {pages} pages were read"))

            status, reason = decide(checks, data.grand_total)
            inv.checks = [c.dict() for c in checks]
            inv.status = status
            inv.error = None
            audit(session, inv.id, "decide", f"{status}: {reason}", {"rules_version": RULES_VERSION})
            session.commit()
            log.info("Invoice %s -> %s (%s)", inv.id, status, reason)
        except Exception as e:
            session.rollback()
            inv = session.get(Invoice, invoice_id)
            inv.error = str(e)[:2000]
            inv.status = Status.RECEIVED if inv.attempts < MAX_ATTEMPTS else Status.FAILED
            audit(session, inv.id, "error", f"Attempt {inv.attempts} failed: {e}")
            session.commit()
            log.exception("Invoice %s failed", invoice_id)
            return

        if inv.status == Status.AUTO_POST:
            try:
                post_invoice(session, inv, actor="system", how="auto-post")
                log.info("Invoice %s posted to ledgers", inv.id)
            except Exception as e:
                # Stays AUTO_POST; retry with POST /invoices/{id}/post
                session.rollback()
                inv = session.get(Invoice, invoice_id)
                inv.error = f"Posting failed: {e}"[:2000]
                audit(session, inv.id, "post", inv.error)
                session.commit()
                log.exception("Invoice %s posting failed", invoice_id)


def main():
    logging.basicConfig(level=logging.INFO)
    Base.metadata.create_all(engine)
    log.info("Worker started")
    while True:
        invoice_id = claim_next()
        if invoice_id is None:
            time.sleep(2)
            continue
        process(invoice_id)


if __name__ == "__main__":
    main()
