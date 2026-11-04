"""Three-way matching (D-112): the invoice against its purchase order and what the branches accepted (GRNs).

For each invoice line, the PO lines with the same HSN/SAC (first 4 digits) and the nearest price (several when
one item was ordered for several branches: the quantity is spread over them); then:
- price: above the PO price by more than PRICE_TOLERANCE_PCT needs review (a lower price is fine);
- quantity: at most what was accepted and not billed yet by another invoice, plus QUANTITY_TOLERANCE_PCT.
  Nothing accepted yet = the goods haven't arrived (or the GRN isn't confirmed): the invoice waits in review
  and is checked again when a GRN for its PO is confirmed (procurement.recheck_waiting).
The matched quantities are kept (invoice_po_lines) so the next invoice for the same PO sees them as billed.
On only when PO_MATCHING=true: without POs entered every invoice would wait."""
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import procurement
from app.config import settings
from app.models import Branch, InvoicePoLine, PoLine
from app.pipeline.checks import FAIL, PASS, WARN, Check
from app.schemas import ExtractedInvoice

MATCHED_TYPES = {"tax_invoice", "bill_of_supply", "debit_note"}


def clear(session: Session, inv) -> None:
    session.execute(delete(InvoicePoLine).where(InvoicePoLine.invoice_id == inv.id))
    inv.po_id = inv.match_status = None
    inv.branch_id = None


def branch_by_gstin(session: Session, gstin: str | None) -> int | None:
    if not gstin:
        return None
    ids = session.scalars(select(Branch.id).where(Branch.gstin == gstin, Branch.active.is_(True))).all()
    return ids[0] if len(ids) == 1 else None      # several branches on one GSTIN: the PO must say which


def _same_code(a: str | None, b: str | None) -> bool:
    """Both empty, or the same first 4 digits (the heading): '84713010' ~ '8471'."""
    if not a or not b:
        return True
    return a[:4] == b[:4]


def check_po(session: Session, inv, data: ExtractedInvoice) -> list[Check]:
    clear(session, inv)
    inv.branch_id = branch_by_gstin(session, data.buyer_gstin)
    if not settings.po_matching or data.document_type not in MATCHED_TYPES:
        return []
    if not procurement.normalize_po(data.po_number):
        inv.match_status = "no_po"
        if settings.po_required:
            return [Check("po", WARN, "No PO number on the invoice: check it was ordered")]
        return [Check("po", PASS, "No PO number on the invoice")]
    po = procurement.find_po(session, data.po_number)
    if po is None:
        inv.match_status = "unmatched"
        return [Check("po", WARN, f"PO {data.po_number} is not in the system: enter it, or check the number")]
    inv.po_id, inv.branch_id = po.id, po.branch_id or inv.branch_id
    if po.status == "CANCELLED":
        inv.match_status = "unmatched"
        return [Check("po", FAIL, f"PO {po.po_number} was cancelled")]
    if po.supplier_gstin and data.supplier_gstin and po.supplier_gstin != data.supplier_gstin:
        inv.match_status = "unmatched"
        return [Check("po", FAIL, f"PO {po.po_number} is for {po.supplier_name} ({po.supplier_gstin}), "
                                  f"not this supplier ({data.supplier_gstin})")]

    procurement.lock_po(session, po.id)
    totals = procurement.line_totals(session, po.id, exclude_invoice=inv.id)
    po_lines = session.scalars(select(PoLine).where(PoLine.po_id == po.id).order_by(PoLine.line_no)).all()
    price_tol, qty_tol = settings.price_tolerance_pct / 100, settings.quantity_tolerance_pct / 100
    problems, matched = [], 0
    for i, ln in enumerate(data.lines, start=1):
        candidates = [p for p in po_lines if _same_code(ln.hsn_sac, p.hsn_sac)]
        if not candidates:
            problems.append(f"line {i} ({ln.description}) is not on PO {po.po_number}")
            continue
        # The PO lines at the nearest price: one item ordered for several branches is one PO line per
        # branch, and the supplier bills it as one line ("50 laptops"), so the quantity is spread over them
        nearest = min(abs(float(c.unit_price) - ln.unit_price) for c in candidates)
        group = [c for c in candidates if abs(float(c.unit_price) - ln.unit_price) <= nearest + 0.005]
        sums = procurement.LineTotals(sum(totals[c.id].ordered for c in group),
                                      sum(totals[c.id].accepted for c in group), 0.0,
                                      sum(totals[c.id].invoiced for c in group))
        left = ln.quantity
        for c in sorted(group, key=lambda c: (-totals[c.id].to_invoice, c.line_no)):
            take = min(left, totals[c.id].to_invoice)
            if take > 1e-9:
                session.add(InvoicePoLine(invoice_id=inv.id, invoice_line=i, po_line_id=c.id, quantity=take,
                                          unit_price=ln.unit_price))
                totals[c.id].invoiced += take      # a later invoice line sees this one as billed
                left -= take
        if left > 1e-9:   # billed beyond what was received: kept as billed, so the next invoice sees it too
            extra = max(group, key=lambda c: totals[c.id].ordered)
            session.add(InvoicePoLine(invoice_id=inv.id, invoice_line=i, po_line_id=extra.id, quantity=left,
                                      unit_price=ln.unit_price))
            totals[extra.id].invoiced += left
        line_problems = []
        po_price = float(group[0].unit_price)
        if ln.unit_price > po_price * (1 + price_tol) + 0.005:
            line_problems.append(f"price {ln.unit_price:,.2f} is above the PO's {po_price:,.2f}")
        if left > ln.quantity * qty_tol + 1e-9:
            if sums.accepted == 0:
                line_problems.append(f"bills {ln.quantity:g} but nothing has been received yet (no confirmed GRN)")
            else:
                line_problems.append(f"bills {ln.quantity:g} but only {sums.to_invoice:g} is received and not "
                                     f"billed (ordered {sums.ordered:g}, accepted {sums.accepted:g}, "
                                     f"billed before {sums.invoiced:g})")
        if line_problems:
            where = ", ".join(str(c.line_no) for c in group)
            problems.append(f"line {i} ({ln.description}) vs PO line {where}: " + "; ".join(line_problems))
        else:
            matched += 1
    session.flush()
    n = len(data.lines)
    if not problems:
        inv.match_status = "matched"
        return [Check("po", PASS, f"Matches PO {po.po_number}: {n} line(s) within the received quantities and prices")]
    inv.match_status = "partial" if matched else "unmatched"
    return [Check("po", WARN, f"PO {po.po_number}: " + "; ".join(problems))]
