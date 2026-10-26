"""Posting: AUTO_POST / APPROVED invoices -> Markdown ledger books -> POSTED."""
from datetime import datetime

from sqlalchemy.orm import Session

from app.ledger import Voucher, VoucherLine, write_voucher
from app.models import Invoice, Supplier, PurchaseOrder, Status, audit
from app.schemas import ExtractedInvoice

POSTABLE = {Status.AUTO_POST, Status.APPROVED}


def post_invoice(session: Session, inv: Invoice, actor: str, how: str) -> list[str]:
    """Write the voucher files, then mark the invoice POSTED and commit.
    Files are written first; a retry after a failed commit does not write them again."""
    if inv.status not in POSTABLE:
        raise ValueError(f"Invoice {inv.id} is {inv.status}, only {sorted(POSTABLE)} can be posted")
    data = ExtractedInvoice(**inv.extracted)
    supplier = session.get(Supplier, inv.supplier_id) if inv.supplier_id else None
    po = session.get(PurchaseOrder, inv.po_id) if inv.po_id else None

    files = write_voucher(Voucher(
        invoice_id=inv.id,
        invoice_no=data.invoice_number,
        invoice_date=data.invoice_date,
        supplier_name=supplier.name if supplier else data.supplier_name,
        supplier_gstin=supplier.gstin if supplier else data.supplier_gstin,
        po_number=po.po_number if po else None,
        taxable_total=data.taxable_total,
        cgst=data.cgst, sgst=data.sgst, igst=data.igst,
        grand_total=data.grand_total,
        lines=[VoucherLine(l.description, l.hsn_sac, l.quantity, l.unit_price,
                           l.taxable_value, l.gst_rate) for l in data.lines],
        posted_by=actor,
        posted_at=datetime.now().astimezone(),
        how=how,
    ))

    old = inv.status
    inv.status = Status.POSTED
    inv.error = None
    audit(session, inv.id, "post", f"{old} -> {Status.POSTED} ({how})", {"files": files}, actor=actor)
    session.commit()
    return files
