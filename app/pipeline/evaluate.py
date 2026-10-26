"""Store extracted values on the invoice, run the checks and pick the status.
Used after AI extraction (worker) and after a user's corrections (API)."""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Invoice, Status, audit
from app.pipeline import einvoice, journal
from app.pipeline.checks import Check, WARN
from app.pipeline.decide import decide, RULES_VERSION
from app.pipeline.validate import validate, normalize_extracted, normalize_invoice_no, parse_date
from app.schemas import ExtractedInvoice


log = logging.getLogger("decide")


def _fit(value: str | None, size: int) -> str | None:
    """Values that don't fit a column are stored as NULL; the checks still see the real value."""
    return value if value is not None and len(value) <= size else None


def store(inv: Invoice, data: ExtractedInvoice) -> ExtractedInvoice:
    data = normalize_extracted(data)
    inv.extracted = data.model_dump()
    inv.supplier_name = data.supplier_name[:255] or None
    inv.supplier_gstin = _fit(data.supplier_gstin, 15)
    inv.invoice_no = data.invoice_number[:100] or None
    inv.invoice_no_norm = normalize_invoice_no(data.invoice_number)[:100] or None
    inv.invoice_date = data.invoice_date if parse_date(data.invoice_date) else None
    inv.grand_total = data.grand_total
    return data


def evaluate(session: Session, inv: Invoice, data: ExtractedInvoice, *, actor: str = "system",
             extra_checks: list[Check] | None = None, after_correction: bool = False) -> str:
    """Returns the new status. A corrected invoice is never auto-approved: the
    reviewer who corrected it approves it explicitly."""
    if after_correction and extra_checks is None:
        extra_checks = _carried_checks(session, inv, data)
    data = store(inv, data)
    session.flush()
    if data.document_type == journal.JOURNAL:
        status, reason = _evaluate_journal(session, inv, data, extra_checks or [])
    else:
        checked = data.is_invoice or data.looks_like_invoice
        result = validate(session, inv, data) if checked else None
        if result is not None:
            result.checks += journal.check_invoice_against_journals(session, inv, data) + (extra_checks or [])
            status, reason = decide(data, result)
            inv.checks = [c.dict() for c in result.checks]
            inv.duplicate_of_id = result.duplicate_of
        else:
            status, reason = decide(data, None)
            inv.checks = []
            inv.duplicate_of_id = None
        if status not in (Status.NOT_INVOICE, Status.DUPLICATE):
            journal.link_waiting_journals(session, inv, data)

    if after_correction and status in (Status.APPROVED, Status.NEEDS_REVIEW):
        status = Status.NEEDS_REVIEW
        if reason == "All checks passed" or reason.startswith("Amount"):
            reason = f"Corrected; {reason[0].lower()}{reason[1:]}. Ready to approve"

    inv.status = status
    inv.status_reason = reason
    if status == Status.APPROVED:
        inv.approved_by = "auto"
        inv.approved_at = datetime.now(timezone.utc)
    audit(session, inv.id, "decide", f"{status}: {reason}", {"rules_version": RULES_VERSION}, actor=actor)
    log.info("Invoice %s -> %s (%s)", inv.id, status, reason)
    return status


def _evaluate_journal(session: Session, inv: Invoice, data: ExtractedInvoice,
                      extra_checks: list[Check]) -> tuple[str, str]:
    checks, invoice_id = journal.check_journal(session, inv, data)
    checks += extra_checks
    inv.checks = [c.dict() for c in checks]
    inv.duplicate_of_id = invoice_id
    if invoice_id:
        if any(c.name == "journal_copy" for c in checks):
            return Status.DUPLICATE, f"Same journal entry as #{invoice_id}"
        return Status.DUPLICATE, f"Journal entry for invoice #{invoice_id}"
    problems = [c.message for c in checks if c.status != "PASS"]
    return Status.NEEDS_REVIEW, "Journal entry: " + "; ".join(problems)


def _carried_checks(session: Session, inv: Invoice, data: ExtractedInvoice) -> list[Check]:
    """Checks only the worker can make, kept when a person's correction re-runs the checks: the page
    limit as it was, and the e-invoice QR compared again with the corrected values."""
    carried = [Check(**c) for c in (inv.checks or []) if c.get("name") == "pages"]
    qr = session.scalar(select(AuditLog.data).where(AuditLog.invoice_id == inv.id, AuditLog.stage == "einvoice")
                        .order_by(AuditLog.id.desc()).limit(1))
    if qr:
        carried.append(einvoice.recheck(normalize_extracted(data), qr))
    return carried


def pages_check(pages: int, max_pages: int) -> list[Check]:
    if pages > max_pages:
        return [Check("pages", WARN, f"Only the first {max_pages} of {pages} pages were read")]
    return []
