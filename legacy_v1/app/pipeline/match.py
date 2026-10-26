"""Matching Engine: deterministic supplier / PO / receipt matching."""
from difflib import SequenceMatcher

from sqlalchemy import select, delete, func, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Supplier, PurchaseOrder, Invoice, InvoicePOLine, Status
from app.pipeline.checks import Check, PASS, WARN, FAIL
from app.schemas import ExtractedInvoice

# Invoices whose matched quantities count against a PO line's received qty
BILLING_STATUSES = [Status.PROCESSING, Status.PENDING_APPROVAL, Status.AUTO_POST,
                    Status.APPROVED, Status.POSTED]
PO_LOCK = 1   # pg advisory lock namespace


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _hsn_match(a: str | None, b: str | None) -> bool:
    """4-digit PO HSN matches 8-digit invoice HSN and vice versa."""
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def _billed_elsewhere(session: Session, po_line_id: int, inv: Invoice | None) -> float:
    q = (select(func.coalesce(func.sum(InvoicePOLine.quantity), 0))
         .join(Invoice, Invoice.id == InvoicePOLine.invoice_id)
         .where(InvoicePOLine.po_line_id == po_line_id, Invoice.status.in_(BILLING_STATUSES)))
    if inv is not None:
        q = q.where(InvoicePOLine.invoice_id != inv.id)
    return float(session.scalar(q))


def match(session: Session, data: ExtractedInvoice, inv: Invoice | None = None):
    """Pass `inv` to record line matches (needed for the received-qty check across invoices)."""
    checks: list[Check] = []

    supplier = None
    if data.supplier_gstin:
        supplier = session.scalar(select(Supplier).where(Supplier.gstin == data.supplier_gstin))
    if supplier:
        checks.append(Check("supplier_match", PASS, f"Matched supplier {supplier.name}"))
    else:
        checks.append(Check("supplier_match", FAIL,
                            f"No supplier in master with GSTIN {data.supplier_gstin}"))
        return checks, None, None

    po = None
    if data.po_number:
        po = session.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number == data.po_number.strip()))
    if not po:
        checks.append(Check("po_match", WARN, "No matching PO found; non-PO invoice needs approval"))
        return checks, supplier, None
    if po.supplier_id != supplier.id:
        checks.append(Check("po_match", FAIL, f"PO {po.po_number} belongs to a different supplier"))
        return checks, supplier, po
    checks.append(Check("po_match", PASS, f"Matched PO {po.po_number}"))

    # One invoice at a time per PO, until commit, so two workers can't both bill the same goods
    session.execute(text("SELECT pg_advisory_xact_lock(:ns, :key)"), {"ns": PO_LOCK, "key": po.id})
    if inv is not None:
        session.execute(delete(InvoicePOLine).where(InvoicePOLine.invoice_id == inv.id))

    # Line matching: same HSN preferred, then best description similarity.
    # Swap this for an LLM/embedding mapper once descriptions get messy.
    used = set()
    for idx, line in enumerate(data.lines, start=1):
        candidates = [pl for pl in po.lines if pl.id not in used]
        same_hsn = [pl for pl in candidates if _hsn_match(line.hsn_sac, pl.hsn_sac)]
        best = max(same_hsn or candidates, key=lambda pl: _similar(line.description, pl.description),
                   default=None)
        if best and not same_hsn:
            # Different or missing HSN: only accept a close description
            needed = 0.8 if (line.hsn_sac and best.hsn_sac) else 0.6
            if _similar(line.description, best.description) < needed:
                best = None
        if not best:
            checks.append(Check(f"line_{idx}", FAIL, f"'{line.description}' not found on PO"))
            continue
        used.add(best.id)
        if inv is not None:
            session.add(InvoicePOLine(invoice_id=inv.id, po_line_id=best.id, quantity=line.quantity))

        po_price = float(best.unit_price)
        if po_price <= 0:
            if line.unit_price > 0:
                checks.append(Check(f"line_{idx}_price", FAIL,
                                    f"Unit price {line.unit_price} charged for a zero-priced PO line"))
            else:
                checks.append(Check(f"line_{idx}_price", PASS, "Unit price matches PO"))
        else:
            price_diff = (line.unit_price - po_price) / po_price * 100
            if price_diff > settings.price_tolerance_pct:
                checks.append(Check(f"line_{idx}_price", FAIL,
                                    f"Unit price {line.unit_price} is {price_diff:.1f}% above PO {best.unit_price}"))
            elif price_diff > 0:
                checks.append(Check(f"line_{idx}_price", WARN, f"Unit price {price_diff:.1f}% above PO, within tolerance"))
            else:
                checks.append(Check(f"line_{idx}_price", PASS, "Unit price matches PO"))

        received = float(best.qty_received)
        already = _billed_elsewhere(session, best.id, inv)
        if line.quantity + already > received:
            billed = f" ({already:g} already invoiced)" if already else ""
            checks.append(Check(f"line_{idx}_qty", FAIL,
                                f"Invoiced qty {line.quantity:g} exceeds received qty {received:g}{billed}"))
        else:
            checks.append(Check(f"line_{idx}_qty", PASS, "Quantity within received goods"))

    return checks, supplier, po
