"""Branches, purchase orders and goods receipts (D-111, D-112).

A PO says what was ordered; a GRN, confirmed by the branch that received the goods (or the person who saw
the service done), says how much arrived and how much of it was accepted. Matching an invoice
(app/pipeline/matching.py) compares it with both. Quantities received and invoiced are added up from
the GRN and invoice rows when needed, never stored on the PO, so they can't drift apart.

Who may create or confirm a GRN is not enforced yet (no login): the GRN records the names."""
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuditLog, GoodsReceipt, GrnLine, Invoice, InvoicePoLine, PoLine, PurchaseOrder, Status, audit
from app.pipeline.validate import normalize_invoice_no

PO_LOCK = 5                       # pg advisory lock namespace: one change of a PO's quantities at a time
CLOSED = ("CLOSED", "CANCELLED")
# Invoices whose matched quantities count as billed: recorded, and waiting for review (so two invoices
# for the same goods can't both pass while the first waits)
BILLED = [Status.NEEDS_REVIEW, Status.APPROVED, Status.EXPORTED]


class ProcurementError(ValueError):
    """A request that can't be done as asked; the message says why. The API answers 422 (wrong values)..."""
    status = 422


class Conflict(ProcurementError):
    """...or 409: not possible in the PO's or GRN's present state."""
    status = 409


def normalize_po(number: str | None) -> str:
    return normalize_invoice_no(number or "")[:50]


def lock_po(session: Session, po_id: int) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:ns, :id)"), {"ns": PO_LOCK, "id": po_id})


@dataclass
class LineTotals:
    ordered: float
    accepted: float = 0.0     # confirmed GRNs
    rejected: float = 0.0
    invoiced: float = 0.0     # matched invoice lines (BILLED invoices)

    @property
    def to_receive(self) -> float:
        return max(self.ordered - self.accepted, 0.0)

    @property
    def to_invoice(self) -> float:
        """Accepted but not billed yet: what the next invoice may bill."""
        return max(self.accepted - self.invoiced, 0.0)


def line_totals(session: Session, po_id: int, exclude_invoice: int | None = None) -> dict[int, LineTotals]:
    lines = session.scalars(select(PoLine).where(PoLine.po_id == po_id).order_by(PoLine.line_no)).all()
    totals = {ln.id: LineTotals(float(ln.quantity)) for ln in lines}
    for line_id, accepted, rejected in session.execute(
            select(GrnLine.po_line_id, func.sum(GrnLine.accepted_qty), func.sum(GrnLine.rejected_qty))
            .join(GoodsReceipt, GoodsReceipt.id == GrnLine.grn_id)
            .where(GoodsReceipt.po_id == po_id, GoodsReceipt.status == "CONFIRMED").group_by(GrnLine.po_line_id)):
        totals[line_id].accepted, totals[line_id].rejected = float(accepted), float(rejected)
    for line_id, qty in session.execute(
            select(InvoicePoLine.po_line_id, func.sum(InvoicePoLine.quantity))
            .join(Invoice, Invoice.id == InvoicePoLine.invoice_id)
            .join(PoLine, PoLine.id == InvoicePoLine.po_line_id)
            .where(PoLine.po_id == po_id, Invoice.status.in_(BILLED), Invoice.id != (exclude_invoice or 0))
            .group_by(InvoicePoLine.po_line_id)):
        totals[line_id].invoiced = float(qty)
    return totals


def find_po(session: Session, number: str | None) -> PurchaseOrder | None:
    norm = normalize_po(number)
    return session.scalar(select(PurchaseOrder).where(PurchaseOrder.po_number_norm == norm)) if norm else None


# ---------- purchase orders

def create_po(session: Session, *, po_number: str, supplier_name: str, supplier_gstin: str | None, po_date: date,
              kind: str, branch_id: int | None, notes: str | None, lines: list[dict], actor: str) -> PurchaseOrder:
    norm = normalize_po(po_number)
    if not norm:
        raise ProcurementError("The PO number has no letters or digits")
    if find_po(session, po_number):
        raise Conflict(f"PO {po_number} already exists")
    if not lines:
        raise ProcurementError("A PO needs at least one line")
    po = PurchaseOrder(po_number=po_number.strip(), po_number_norm=norm, supplier_name=supplier_name.strip(),
                       supplier_gstin=(supplier_gstin or "").upper() or None, po_date=po_date, kind=kind,
                       branch_id=branch_id, notes=notes, status="OPEN", created_by=actor)
    session.add(po)
    session.flush()
    for i, ln in enumerate(lines, start=1):
        session.add(PoLine(po_id=po.id, line_no=i, **ln))
    session.flush()
    return po


def set_po_status(session: Session, po: PurchaseOrder, totals: dict[int, LineTotals] | None = None) -> None:
    """OPEN / PARTIALLY_RECEIVED / RECEIVED from the confirmed GRNs (CLOSED and CANCELLED stay)."""
    if po.status in CLOSED:
        return
    totals = totals or line_totals(session, po.id)
    if all(t.to_receive == 0 for t in totals.values()):
        po.status = "RECEIVED"
    elif any(t.accepted > 0 for t in totals.values()):
        po.status = "PARTIALLY_RECEIVED"
    else:
        po.status = "OPEN"


def end_po(session: Session, po: PurchaseOrder, status: str, actor: str, note: str) -> None:
    """CLOSED: nothing more will come (short-closed). CANCELLED: only before anything was received."""
    if po.status in CLOSED:
        raise Conflict(f"PO {po.po_number} is already {po.status}")
    if status == "CANCELLED" and any(t.accepted or t.rejected for t in line_totals(session, po.id).values()):
        raise Conflict("Goods were already received against this PO: close it instead")
    po.status = status
    po.notes = "\n".join(filter(None, [po.notes, f"{status} by {actor}: {note}".strip()]))


# ---------- goods receipts

def _over_received(po_line: PoLine, totals: LineTotals, accepted: float) -> str | None:
    allowed = float(po_line.quantity) * (1 + settings.quantity_tolerance_pct / 100)
    if totals.accepted + accepted > allowed + 1e-9:
        return (f"line {po_line.line_no} ({po_line.description}): accepting {accepted:g} would make "
                f"{totals.accepted + accepted:g} of {float(po_line.quantity):g} ordered")
    return None


def create_grn(session: Session, po: PurchaseOrder, *, branch_id: int | None, received_on: date,
               challan_number: str | None, notes: str | None, lines: list[dict], actor: str,
               actor_id: int | None = None, source: str = "api") -> GoodsReceipt:
    """`lines`: {line_no, received_qty, accepted_qty (default all received), rejected_qty, reason}."""
    if po.status in CLOSED:
        raise Conflict(f"PO {po.po_number} is {po.status}")
    if received_on > date.today():
        raise ProcurementError("The receipt date is in the future")
    if not lines:
        raise ProcurementError("A GRN needs at least one line")
    lock_po(session, po.id)
    po_lines = {ln.line_no: ln for ln in session.scalars(select(PoLine).where(PoLine.po_id == po.id))}
    totals = line_totals(session, po.id)
    grn = GoodsReceipt(po_id=po.id, branch_id=branch_id or po.branch_id, received_on=received_on,
                       challan_number=challan_number, notes=notes, status="DRAFT", created_by=actor,
                       created_by_id=actor_id, source=source)
    session.add(grn)
    session.flush()
    problems = []
    seen = set()
    for ln in lines:
        po_line = po_lines.get(ln["line_no"])
        if po_line is None:
            raise ProcurementError(f"PO {po.po_number} has no line {ln['line_no']}")
        if ln["line_no"] in seen:
            raise ProcurementError(f"Line {ln['line_no']} is listed twice")
        seen.add(ln["line_no"])
        received, rejected = ln["received_qty"], ln.get("rejected_qty") or 0
        accepted = ln.get("accepted_qty")
        accepted = received - rejected if accepted is None else accepted
        if received <= 0 or accepted < 0 or rejected < 0 or abs(accepted + rejected - received) > 1e-9:
            raise ProcurementError(f"Line {ln['line_no']}: received must be more than 0 and equal accepted "
                                   f"+ rejected ({received:g} ≠ {accepted:g} + {rejected:g})")
        if rejected and not (ln.get("reason") or "").strip():
            raise ProcurementError(f"Line {ln['line_no']}: say why {rejected:g} were rejected")
        if problem := _over_received(po_line, totals[po_line.id], accepted):
            problems.append(problem)
        session.add(GrnLine(grn_id=grn.id, po_line_id=po_line.id, received_qty=received, accepted_qty=accepted,
                            rejected_qty=rejected, reason=(ln.get("reason") or "").strip() or None))
    if problems:
        raise ProcurementError("More than ordered: " + "; ".join(problems))
    session.flush()
    return grn


def confirm_grn(session: Session, grn: GoodsReceipt, actor: str) -> PurchaseOrder:
    """The branch confirms what it received: only now does it count for matching."""
    if grn.status != "DRAFT":
        raise Conflict(f"GRN {grn.id} is {grn.status}")
    po = session.get(PurchaseOrder, grn.po_id)
    if po.status in CLOSED:
        raise Conflict(f"PO {po.po_number} is {po.status}")
    lock_po(session, po.id)
    totals = line_totals(session, po.id)       # other GRNs may have been confirmed since this was drafted
    lines = session.scalars(select(GrnLine).where(GrnLine.grn_id == grn.id)).all()
    problems = [p for gl in lines
                if (p := _over_received(session.get(PoLine, gl.po_line_id), totals[gl.po_line_id], float(gl.accepted_qty)))]
    if problems:
        raise ProcurementError("More than ordered: " + "; ".join(problems))
    grn.status, grn.confirmed_by, grn.confirmed_at = "CONFIRMED", actor, datetime.now(timezone.utc)
    session.flush()
    set_po_status(session, po)
    return po


def cancel_grn(session: Session, grn: GoodsReceipt, actor: str, note: str) -> None:
    """Only a draft: a confirmed GRN may already have let invoices through."""
    if grn.status != "DRAFT":
        raise Conflict(f"Only a draft GRN can be cancelled (this one is {grn.status})")
    grn.status = "CANCELLED"
    grn.notes = "\n".join(filter(None, [grn.notes, f"Cancelled by {actor}: {note}".strip()]))


# ---------- invoices waiting for a PO or a GRN

def waiting_invoices(session: Session, po: PurchaseOrder) -> list[Invoice]:
    """Invoices in review that quote this PO: a new GRN, or the PO itself arriving, may let them through."""
    candidates = session.scalars(select(Invoice).where(
        Invoice.status == Status.NEEDS_REVIEW,
        (Invoice.po_id == po.id) | Invoice.extracted["po_number"].astext.is_not(None)).order_by(Invoice.id))
    return [inv for inv in candidates
            if inv.po_id == po.id or normalize_po((inv.extracted or {}).get("po_number")) == po.po_number_norm]


def recheck_waiting(session: Session, po: PurchaseOrder, actor: str) -> list[tuple[int, str]]:
    """Runs the checks again on the invoices waiting for this PO. Returns (invoice id, new status)."""
    if not settings.po_matching:
        return []
    from app.pipeline.evaluate import recheck  # avoid an import cycle
    done = []
    for inv in waiting_invoices(session, po):
        audit(session, inv.id, "matching", f"Checked again: PO {po.po_number} changed", actor=actor)
        done.append((inv.id, recheck(session, inv, actor=actor)))
    return done


def after_po_change(session: Session, po: PurchaseOrder, actor: str) -> list[dict]:
    """Commits, after checking again the invoices waiting for this PO; any now approved go to Excel."""
    from app.excel import try_export  # avoid an import cycle
    rechecked = recheck_waiting(session, po, actor)
    session.commit()
    if any(status == Status.APPROVED for _, status in rechecked):
        try_export(session, wait=False)
    return [{"invoice_id": i, "status": status} for i, status in rechecked]


def serialize_po(session: Session, po: PurchaseOrder, detail: bool = False) -> dict:
    out = {"id": po.id, "po_number": po.po_number, "supplier_name": po.supplier_name,
           "supplier_gstin": po.supplier_gstin, "branch_id": po.branch_id, "po_date": po.po_date, "kind": po.kind,
           "status": po.status, "notes": po.notes, "created_by": po.created_by, "created_at": po.created_at}
    if not detail:
        return out
    totals = line_totals(session, po.id)
    lines = session.scalars(select(PoLine).where(PoLine.po_id == po.id).order_by(PoLine.line_no))
    out["lines"] = [{"line_no": ln.line_no, "id": ln.id, "description": ln.description, "hsn_sac": ln.hsn_sac,
                     "unit": ln.unit, "unit_price": float(ln.unit_price),
                     "gst_rate": float(ln.gst_rate) if ln.gst_rate is not None else None,
                     "branch_id": ln.branch_id or po.branch_id, "ordered": float(ln.quantity),
                     "accepted": totals[ln.id].accepted, "rejected": totals[ln.id].rejected,
                     "invoiced": totals[ln.id].invoiced, "to_receive": totals[ln.id].to_receive,
                     "to_invoice": totals[ln.id].to_invoice} for ln in lines]
    grns = session.scalars(select(GoodsReceipt).where(GoodsReceipt.po_id == po.id).order_by(GoodsReceipt.id))
    out["grns"] = [serialize_grn(session, g) for g in grns]
    out["invoices"] = [{"invoice_id": i.id, "invoice_no": i.invoice_no, "status": i.status,
                        "match_status": i.match_status, "grand_total": float(i.grand_total or 0)}
                       for i in session.scalars(select(Invoice).where(Invoice.po_id == po.id).order_by(Invoice.id))]
    return out


def serialize_grn(session: Session, grn: GoodsReceipt) -> dict:
    rows = session.execute(select(GrnLine, PoLine.line_no, PoLine.description).join(PoLine, PoLine.id == GrnLine.po_line_id)
                           .where(GrnLine.grn_id == grn.id).order_by(PoLine.line_no)).all()
    return {"id": grn.id, "po_id": grn.po_id, "branch_id": grn.branch_id, "received_on": grn.received_on,
            "challan_number": grn.challan_number, "status": grn.status, "notes": grn.notes,
            "created_by": grn.created_by, "source": grn.source, "confirmed_by": grn.confirmed_by,
            "confirmed_at": grn.confirmed_at,
            "lines": [{"line_no": no, "description": desc, "received": float(gl.received_qty),
                       "accepted": float(gl.accepted_qty), "rejected": float(gl.rejected_qty), "reason": gl.reason}
                      for gl, no, desc in rows]}


def corrected(session: Session, inv: Invoice) -> bool:
    return session.scalar(select(AuditLog.id).where(AuditLog.invoice_id == inv.id, AuditLog.stage == "correct")
                          .limit(1)) is not None
