"""Supplier master, built from the invoices already approved: no separate data entry.

A supplier (by GSTIN) is "known" once it has approved invoices. Later invoices are compared with
that history: a new supplier, a different name on a known GSTIN (a misread GSTIN, or someone
borrowing it) and an amount far above the supplier's usual are all shown to the reviewer."""
import re
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Invoice, Status
from app.pipeline.checks import Check, PASS, WARN
from app.pipeline.journal import doc_type as journal_doc_type
from app.schemas import ExtractedInvoice

RECORDED = [Status.APPROVED, Status.EXPORTED]


@dataclass
class SupplierProfile:
    gstin: str
    invoices: int
    names: list[str] = field(default_factory=list)   # most used first
    max_total: float = 0.0
    avg_total: float = 0.0
    last_invoice_date: str | None = None


def _name_key(name: str | None) -> str:
    """Names compared without case, punctuation and company suffixes ('Pvt. Ltd.' = 'Private Limited')."""
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(r"\b(private|pvt|limited|ltd|llp|inc|the|and)\b", " ", s)
    return " ".join(s.split())


def profile(session: Session, gstin: str | None, exclude_id: int | None = None) -> SupplierProfile | None:
    """What the approved invoices say about a supplier; None when there are none."""
    if not gstin or len(gstin) > 15:   # stored as none when too long (evaluate.store)
        return None
    # Aggregated in the database, one row per name: a supplier with thousands of invoices costs the
    # same as one with three (loading the rows took ~5 s at 20,000 invoices, D-95)
    rows = session.execute(select(
        Invoice.supplier_name, func.count(), func.max(Invoice.grand_total), func.sum(Invoice.grand_total),
        func.count(Invoice.grand_total), func.max(Invoice.invoice_date))
        .where(Invoice.supplier_gstin == gstin, Invoice.status.in_(RECORDED),
               Invoice.id != (exclude_id or 0), journal_doc_type() != "journal_entry")
        .group_by(Invoice.supplier_name).order_by(func.count().desc())).all()
    if not rows:
        return None
    totals_counted = sum(r[4] for r in rows)
    dates = [r[5] for r in rows if r[5]]
    return SupplierProfile(
        gstin=gstin, invoices=sum(r[1] for r in rows), names=[r[0] or "" for r in rows],
        max_total=max((float(r[2]) for r in rows if r[2] is not None), default=0.0),
        avg_total=sum(float(r[3] or 0) for r in rows) / totals_counted if totals_counted else 0.0,
        last_invoice_date=max(dates, default=None))


def check_supplier(session: Session, inv: Invoice, data: ExtractedInvoice) -> list[Check]:
    """Compare a new invoice with its supplier's history. Credit notes and the like are compared too:
    the name and the usual items still apply, but the amount rule is for invoices only."""
    known = profile(session, data.supplier_gstin, exclude_id=inv.id)
    if known is None:
        if not data.supplier_gstin:
            return []
        # Information only: warning here would send every first invoice to review (D-02)
        return [Check("supplier_history", PASS,
                      f"New supplier {data.supplier_name} ({data.supplier_gstin}): no approved invoice from it yet")]
    checks: list[Check] = []
    if _name_key(data.supplier_name) not in {_name_key(n) for n in known.names}:
        checks.append(Check("supplier_name", WARN,
                            f"GSTIN {data.supplier_gstin} was approved before as '{known.names[0]}', "
                            f"this invoice says '{data.supplier_name}'"))
    if (known.invoices >= settings.supplier_min_history and data.is_invoice
            and data.document_type != "credit_note" and known.max_total > 0
            and data.grand_total > known.max_total * settings.supplier_amount_factor):
        checks.append(Check("supplier_amount", WARN,
                            f"Total {data.grand_total:,.2f} is far above this supplier's usual "
                            f"(highest so far {known.max_total:,.2f}, average {known.avg_total:,.2f}, "
                            f"{known.invoices} invoices)"))
    if not checks:
        checks.append(Check("supplier_history", PASS, f"Known supplier ({known.invoices} approved invoice(s))"))
    return checks


def list_suppliers(session: Session) -> list[dict]:
    """One row per supplier GSTIN with approved invoices, biggest spend first."""
    rows = session.execute(select(
        Invoice.supplier_gstin, func.count(), func.max(Invoice.grand_total), func.avg(Invoice.grand_total),
        func.sum(Invoice.grand_total), func.max(Invoice.invoice_date), func.max(Invoice.supplier_name))
        .where(Invoice.supplier_gstin.is_not(None), Invoice.status.in_(RECORDED), journal_doc_type() != "journal_entry")
        .group_by(Invoice.supplier_gstin).order_by(func.sum(Invoice.grand_total).desc())).all()
    return [{"gstin": g, "name": n, "invoices": c, "highest_total": float(mx or 0), "average_total": float(av or 0),
             "total_spend": float(sm or 0), "last_invoice_date": ld} for g, c, mx, av, sm, ld, n in rows]
