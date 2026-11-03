"""What the company owes suppliers (D-110): due dates, payments marked by a person, and AP ageing.

Due date = invoice date + the supplier's payment terms (suppliers.payment_terms_days), else PAYMENT_DAYS.
A micro or small supplier (MSMED Act section 15) must be paid within the agreed terms but never later
than MSME_PAYMENT_DAYS, and within MSME_DEFAULT_DAYS when nothing is agreed; paying later also stops
the expense being deducted for income tax that year (section 43B(h)). The invoice date stands in for the
day the goods or services were accepted, which is the earlier, safer date.

Owed = approved invoices and debit notes, not marked paid, less the TDS to deduct. Credit notes reduce
what is owed to that supplier. Journal entries are not bills, so they are left out."""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Invoice, Status, Supplier, audit
from app.pipeline.journal import JOURNAL
from app.pipeline.journal import doc_type as stored_doc_type
from app.pipeline.validate import parse_date

MSME_COVERED = {"micro", "small"}      # medium enterprises are not covered by section 15
MSME_CATEGORIES = {"micro", "small", "medium"}
OWED = [Status.APPROVED, Status.EXPORTED]
BUCKETS = [("not_due", None), ("1-30", 30), ("31-60", 60), ("61-90", 90), ("over_90", None)]


def terms_days(sup: Supplier | None) -> tuple[int, str]:
    """(days, why) for a supplier."""
    agreed = sup.payment_terms_days if sup else None
    if sup is not None and sup.msme_category in MSME_COVERED:
        if agreed is None:
            return settings.msme_default_days, f"MSME ({sup.msme_category}) with no agreed terms"
        if agreed > settings.msme_payment_days:
            return settings.msme_payment_days, (f"MSME ({sup.msme_category}): agreed {agreed} days, "
                                                f"but the law allows at most {settings.msme_payment_days}")
        return agreed, f"MSME ({sup.msme_category}) terms"
    if agreed is not None:
        return agreed, "supplier's terms"
    return settings.payment_days, "default terms"


def due_date_for(session: Session, inv: Invoice, document_type: str | None = None) -> date | None:
    document_type = document_type or (inv.extracted or {}).get("document_type")
    issued = parse_date(inv.invoice_date)
    if issued is None or document_type in ("credit_note", JOURNAL):
        return None
    sup = session.scalar(select(Supplier).where(Supplier.gstin == inv.supplier_gstin)) if inv.supplier_gstin else None
    days, _why = terms_days(sup)
    return issued + timedelta(days=days)


def set_terms(session: Session, sup: Supplier, *, days: int | None, msme_category: str | None,
              udyam_number: str | None, actor: str) -> int:
    """Changes a supplier's terms and the due dates of its unpaid invoices. Returns how many changed."""
    sup.payment_terms_days, sup.msme_category, sup.udyam_number = days, msme_category, udyam_number
    session.flush()
    changed = 0
    for inv in session.scalars(select(Invoice).where(Invoice.supplier_gstin == sup.gstin, Invoice.paid_on.is_(None),
                                                     Invoice.status.notin_([Status.REJECTED, Status.DUPLICATE]))):
        due = due_date_for(session, inv)
        if due != inv.due_date:
            inv.due_date = due
            audit(session, inv.id, "payables", f"Due date now {due} (supplier terms changed)", actor=actor)
            changed += 1
    return changed


def mark_paid(session: Session, inv: Invoice, paid_on: date, reference: str, actor: str) -> None:
    inv.paid_on, inv.paid_reference, inv.paid_by = paid_on, reference.strip() or None, actor
    audit(session, inv.id, "payment", f"Marked paid on {paid_on}" + (f" (ref {inv.paid_reference})"
                                                                     if inv.paid_reference else ""), actor=actor)


def unmark_paid(session: Session, inv: Invoice, actor: str, note: str = "") -> None:
    was = inv.paid_on
    inv.paid_on = inv.paid_reference = inv.paid_by = None
    audit(session, inv.id, "payment", f"Payment of {was} removed. {note}".strip(), actor=actor)


def net_payable(inv: Invoice) -> float:
    """What is paid to the supplier: the total less TDS; negative for a credit note."""
    total = float(inv.grand_total or 0)
    if (inv.extracted or {}).get("document_type") == "credit_note":
        return -abs(total)
    return round(total - float(inv.tds_amount or 0), 2)


def bucket(due: date | None, today: date) -> str:
    if due is None or due >= today:
        return "not_due"
    late = (today - due).days
    for name, upto in BUCKETS[1:-1]:
        if late <= upto:
            return name
    return "over_90"


def owed(session: Session) -> list[Invoice]:
    return list(session.scalars(select(Invoice).where(
        Invoice.status.in_(OWED), Invoice.paid_on.is_(None), stored_doc_type() != JOURNAL)
        .order_by(Invoice.due_date.nulls_last(), Invoice.id)))


def serialize_owed(inv: Invoice, today: date, msme: str | None = None) -> dict:
    return {"invoice_id": inv.id, "supplier_name": inv.supplier_name, "supplier_gstin": inv.supplier_gstin,
            "invoice_no": inv.invoice_no, "invoice_date": inv.invoice_date,
            "document_type": (inv.extracted or {}).get("document_type"),
            "grand_total": float(inv.grand_total or 0), "tds_section": inv.tds_section,
            "tds_amount": float(inv.tds_amount) if inv.tds_amount is not None else None,
            "net_payable": net_payable(inv), "due_date": inv.due_date,
            "days_overdue": max((today - inv.due_date).days, 0) if inv.due_date else 0,
            "bucket": bucket(inv.due_date, today), "msme": msme}


def ageing(session: Session, today: date | None = None) -> dict:
    """AP ageing: what is owed per supplier, by days past the due date. Credit notes are a separate
    column (they have no due date) and reduce the supplier's total."""
    today = today or date.today()
    msme = {s.gstin: s.msme_category for s in session.scalars(select(Supplier).where(Supplier.msme_category.is_not(None)))}
    names = [n for n, _ in BUCKETS]
    rows: dict[str, dict] = {}
    totals = dict.fromkeys([*names, "credits", "total"], 0.0)
    msme_overdue = []
    for inv in owed(session):
        key = inv.supplier_gstin or f"name:{inv.supplier_name}"
        row = rows.setdefault(key, {"supplier_name": inv.supplier_name, "supplier_gstin": inv.supplier_gstin,
                                    "msme": msme.get(inv.supplier_gstin), "invoices": 0,
                                    **dict.fromkeys([*names, "credits", "total"], 0.0)})
        amount = net_payable(inv)
        col = "credits" if amount < 0 else bucket(inv.due_date, today)
        for target in (row, totals):
            target[col] = round(target[col] + amount, 2)
            target["total"] = round(target["total"] + amount, 2)
        row["invoices"] += 1
        if col not in ("not_due", "credits") and msme.get(inv.supplier_gstin) in MSME_COVERED:
            msme_overdue.append(serialize_owed(inv, today, msme.get(inv.supplier_gstin)))
    suppliers = sorted(rows.values(), key=lambda r: -r["total"])
    return {"as_of": today, "buckets": [*names, "credits"], "totals": totals, "suppliers": suppliers,
            "msme_overdue": msme_overdue}


def due_within(session: Session, days: int, today: date | None = None) -> list[dict]:
    """Unpaid bills due within `days` (and every overdue one), soonest first."""
    today = today or date.today()
    msme = {s.gstin: s.msme_category for s in session.scalars(select(Supplier).where(Supplier.msme_category.is_not(None)))}
    limit = today + timedelta(days=days)
    return [serialize_owed(inv, today, msme.get(inv.supplier_gstin)) for inv in owed(session)
            if inv.due_date is not None and inv.due_date <= limit]
