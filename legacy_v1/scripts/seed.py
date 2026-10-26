"""Creates tables and sample master data. Run: python -m scripts.seed"""
from app.db import Base, engine, SessionLocal
from app.models import Supplier, PurchaseOrder, POLine

Base.metadata.create_all(engine)

with SessionLocal() as s:
    if not s.query(Supplier).first():
        sup = Supplier(name="Sri Lakshmi Steels Pvt Ltd", gstin="36AAACS1234A1Z3")
        s.add(sup)
        s.flush()
        s.add(PurchaseOrder(po_number="PO-2026-0042", supplier_id=sup.id, lines=[
            POLine(description="MS Pipe 25mm", hsn_sac="7306", qty_ordered=100,
                   qty_received=100, unit_price=450, gst_rate=18),
            POLine(description="GI Elbow 25mm", hsn_sac="7307", qty_ordered=200,
                   qty_received=150, unit_price=35, gst_rate=18),
        ]))
        s.commit()
        print("Seeded supplier and PO-2026-0042")
    else:
        print("Already seeded")
