from datetime import datetime
from sqlalchemy import String, Numeric, ForeignKey, DateTime, Text, Integer, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Status:
    RECEIVED = "RECEIVED"                  # waiting for the worker
    PROCESSING = "PROCESSING"
    AUTO_POST = "AUTO_POST"                # passed everything, ready to post
    PENDING_APPROVAL = "PENDING_APPROVAL"
    EXCEPTION = "EXCEPTION"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    POSTED = "POSTED"                      # voucher written to the ledger files
    FAILED = "FAILED"                      # technical failure (e.g. OpenAI error)


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    gstin: Mapped[str] = mapped_column(String(15), unique=True, index=True)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    lines: Mapped[list["POLine"]] = relationship(back_populates="po", cascade="all, delete-orphan")


class POLine(Base):
    """qty_received is a simplified stand-in for GRN data; replace with a GRN table later."""
    __tablename__ = "po_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"))
    description: Mapped[str] = mapped_column(String(500))
    hsn_sac: Mapped[str | None] = mapped_column(String(10))
    qty_ordered: Mapped[float] = mapped_column(Numeric(14, 3))
    qty_received: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    unit_price: Mapped[float] = mapped_column(Numeric(14, 2))
    gst_rate: Mapped[float] = mapped_column(Numeric(5, 2))
    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(20))               # gmail | upload
    source_ref: Mapped[str | None] = mapped_column(String(255))   # gmail message id, uploader, ...
    original_filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(500))
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    status: Mapped[str] = mapped_column(String(30), default=Status.RECEIVED, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    extracted: Mapped[dict | None] = mapped_column(JSONB)          # raw LLM output
    checks: Mapped[list | None] = mapped_column(JSONB)             # validation results

    # Denormalised for querying & duplicate detection
    supplier_gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    invoice_no: Mapped[str | None] = mapped_column(String(100))
    invoice_no_norm: Mapped[str | None] = mapped_column(String(100), index=True)
    invoice_date: Mapped[str | None] = mapped_column(String(10))
    grand_total: Mapped[float | None] = mapped_column(Numeric(14, 2))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class InvoicePOLine(Base):
    """Which PO line each invoice line matched, so later invoices can't bill more than was received."""
    __tablename__ = "invoice_po_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    po_line_id: Mapped[int] = mapped_column(ForeignKey("po_lines.id"), index=True)
    quantity: Mapped[float] = mapped_column(Numeric(14, 3))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    stage: Mapped[str] = mapped_column(String(50))
    actor: Mapped[str] = mapped_column(String(100), default="system")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def audit(session, invoice_id: int, stage: str, message: str, data: dict | None = None, actor="system"):
    session.add(AuditLog(invoice_id=invoice_id, stage=stage, message=message, data=data, actor=actor))
