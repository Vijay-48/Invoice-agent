"""Learning from reviewers' corrections (Phase 2 item 7).

Every correction is already in the audit log (stage "correct": before -> after). For a supplier
whose invoices were corrected before, those corrections are shown to the AI as hints next time
("invoice_date was read as 2026-03-07, correct was 2026-07-03"): suppliers keep their layouts, so
the same misreadings tend to repeat.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Invoice

MAX_NOTES = 6
SKIP_FIELDS = {"confidence", "low_confidence_fields"}


def correction_notes(session: Session, supplier_gstin: str | None, limit: int = MAX_NOTES) -> str | None:
    """Recent corrections reviewers made on this supplier's documents, as a short hint (or None)."""
    if not supplier_gstin:
        return None
    rows = session.execute(
        select(AuditLog.data, Invoice.id).join(Invoice, Invoice.id == AuditLog.invoice_id)
        .where(AuditLog.stage == "correct", Invoice.supplier_gstin == supplier_gstin)
        .order_by(AuditLog.id.desc()).limit(20)).all()
    notes: list[str] = []
    for data, _ in rows:
        before, after = (data or {}).get("before") or {}, (data or {}).get("after") or {}
        for field, right in after.items():
            if field in SKIP_FIELDS:
                continue
            note = ("line items had to be corrected (check quantities, rates and amounts per line)" if field == "lines"
                    else f"{field} was read as {before.get(field)!r}, the correct value was {right!r}")
            if note not in notes:
                notes.append(note)
            if len(notes) >= limit:
                break
        if len(notes) >= limit:
            break
    if not notes:
        return None
    return ("Reviewers corrected earlier documents from this supplier; avoid the same mistakes:\n"
            + "\n".join(f"- {n}" for n in notes))
