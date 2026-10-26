from datetime import datetime

from sqlalchemy import String, Numeric, ForeignKey, DateTime, Text, Integer, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Status:
    RECEIVED = "RECEIVED"            # waiting for the worker
    PROCESSING = "PROCESSING"
    NEEDS_REVIEW = "NEEDS_REVIEW"    # a check failed or amount over the limit: a user decides
    APPROVED = "APPROVED"            # auto or by a user; waiting to be written to Excel
    EXPORTED = "EXPORTED"            # row is in the Excel workbook
    DUPLICATE = "DUPLICATE"          # same invoice already received
    NOT_INVOICE = "NOT_INVOICE"      # e.g. terms.pdf, quotation, statement
    REJECTED = "REJECTED"
    FAILED = "FAILED"                # technical failure after retries (e.g. OpenAI down)
    NEEDS_PASSWORD = "NEEDS_PASSWORD"  # protected PDF: a user enters the password, then it is read


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255), unique=True)

    # Gmail via Composio. The Composio user id is derived from our id.
    gmail_account_id: Mapped[str | None] = mapped_column(String(100))   # Composio connected account
    gmail_trigger_id: Mapped[str | None] = mapped_column(String(100))
    gmail_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    gmail_last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def composio_user_id(self) -> str:
        return f"ap-user-{self.id}"


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)  # uploader / inbox owner
    source: Mapped[str] = mapped_column(String(20))                   # upload | gmail
    source_ref: Mapped[str | None] = mapped_column(String(255), index=True)  # gmail: "<message id>/<file name>"
    sender: Mapped[str | None] = mapped_column(String(255))           # gmail: From header
    email_subject: Mapped[str | None] = mapped_column(String(500))
    original_filename: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(String(500))
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    status: Mapped[str] = mapped_column(String(30), default=Status.RECEIVED, index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)

    extracted: Mapped[dict | None] = mapped_column(JSONB)   # current values (AI output + user corrections)
    checks: Mapped[list | None] = mapped_column(JSONB)      # validation results

    # Copied out of `extracted` for listing and duplicate detection
    supplier_name: Mapped[str | None] = mapped_column(String(255))
    supplier_gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    invoice_no: Mapped[str | None] = mapped_column(String(100))
    invoice_no_norm: Mapped[str | None] = mapped_column(String(100), index=True)
    invoice_date: Mapped[str | None] = mapped_column(String(10))
    grand_total: Mapped[float | None] = mapped_column(Numeric(14, 2))
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))

    approved_by: Mapped[str | None] = mapped_column(String(100))   # "auto" or a user's name
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    stage: Mapped[str] = mapped_column(String(50))
    actor: Mapped[str] = mapped_column(String(100), default="system")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EmailMessage(Base):
    """One row per Gmail message seen, with what happened to each attachment. Answers
    "why didn't this email become an invoice?" without reading logs."""
    __tablename__ = "email_messages"
    __table_args__ = (UniqueConstraint("user_id", "message_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    message_id: Mapped[str] = mapped_column(String(64))
    thread_id: Mapped[str | None] = mapped_column(String(64))
    sender: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(500))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    folders: Mapped[list | None] = mapped_column(JSONB)          # Gmail labels: INBOX, SPAM, ...
    body_snippet: Mapped[str | None] = mapped_column(Text)       # start of the body, used as an AI hint
    # done (documents queued) | no_documents | skipped (keyword filter) | failed
    outcome: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    # [{"ref", "name", "result": invoice|same_file|unsupported|failed, "invoice_id", "note"}]
    attachments: Mapped[list | None] = mapped_column(JSONB)
    ap_label: Mapped[str | None] = mapped_column(String(60))      # our Gmail label currently on it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GmailLabel(Base):
    """Gmail label ids we created per inbox (AP/Processed, ...)."""
    __tablename__ = "gmail_labels"
    __table_args__ = (UniqueConstraint("user_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    label_id: Mapped[str] = mapped_column(String(100))


class GmailHealth(Base):
    """Is the user's Gmail connection still usable? Checked regularly at Composio; an expired
    or revoked connection is shown to the user ("Reconnect Gmail") instead of failing quietly."""
    __tablename__ = "gmail_health"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="ok")   # ok | reconnect
    detail: Mapped[str | None] = mapped_column(Text)                # Composio's status / the error
    problem_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # alert email sent


class PdfPassword(Base):
    """Passwords that opened protected PDFs before; tried on new protected PDFs."""
    __tablename__ = "pdf_passwords"
    id: Mapped[int] = mapped_column(primary_key=True)
    password: Mapped[str] = mapped_column(String(200), unique=True)
    sender: Mapped[str | None] = mapped_column(String(255))     # who sent the PDF it opened
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatSession(Base):
    """A saved assistant conversation of one user."""
    __tablename__ = "chat_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="New chat")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))            # user | assistant | error
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONB)          # tools_used, files, actions (+ done marks)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReportRun(Base):
    """A summary report sent by email (scheduled or on demand); one per schedule slot."""
    __tablename__ = "report_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    slot: Mapped[str | None] = mapped_column(String(40), unique=True)   # e.g. "weekly:2026-10-19"; None = sent by hand
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recipients: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))          # sent | failed
    error: Mapped[str | None] = mapped_column(Text)
    sent_by: Mapped[str] = mapped_column(String(100), default="schedule")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def audit(session, invoice_id: int, stage: str, message: str, data: dict | None = None, actor="system"):
    session.add(AuditLog(invoice_id=invoice_id, stage=stage, message=message, data=data, actor=actor))
