from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
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
    # WhatsApp number (+919876543210) this user sends invoices from and is reminded at; WHATSAPP_SENDERS
    # in .env still works too (D-106)
    whatsapp_number: Mapped[str | None] = mapped_column(String(20), unique=True)
    # Highest invoice total this user may approve; none = any amount, 0 = may not approve (D-108)
    approval_limit: Mapped[float | None] = mapped_column(Numeric(14, 2))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"))   # where this person works

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def composio_user_id(self) -> str:
        return f"ap-user-{self.id}"


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)  # uploader / inbox owner
    source: Mapped[str] = mapped_column(String(20))                   # upload | gmail | whatsapp
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

    # TDS the company deducts when paying (app/pipeline/tax.py, D-109); a suggestion until the CA confirms
    tds_section: Mapped[str | None] = mapped_column(String(10))      # 194C | 194J | 194I | 194H
    tds_base: Mapped[float | None] = mapped_column(Numeric(14, 2))   # taxable value of the lines it covers
    tds_rate: Mapped[float | None] = mapped_column(Numeric(5, 2))    # percent; none = below the threshold
    tds_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    # Payables (app/payables.py, D-110)
    due_date: Mapped[date | None] = mapped_column(Date, index=True)
    paid_on: Mapped[date | None] = mapped_column(Date)
    paid_reference: Mapped[str | None] = mapped_column(String(100))  # UTR / cheque number
    paid_by: Mapped[str | None] = mapped_column(String(100))
    # Purchase order matching (app/pipeline/matching.py, D-112)
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"), index=True)
    match_status: Mapped[str | None] = mapped_column(String(20))    # matched | partial | unmatched | no_po

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


class FollowUp(Base):
    """One reminder ladder for one invoice and reason: kind "review" (waiting for a person), "export"
    (approved but not in Excel) or "result" (tell a WhatsApp sender what became of their file).
    `step` = ladder steps already taken; `next_due_at` is when the next one is due (a snooze or an
    acknowledgment just moves it later)."""
    __tablename__ = "follow_ups"
    __table_args__ = (UniqueConstraint("invoice_id", "kind"), Index("ix_follow_ups_due", "state", "next_due_at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    target_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String(20), default="active")    # active | done
    done_reason: Mapped[str | None] = mapped_column(String(40))          # resolved | stopped | finished | unreachable
    step: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    awaiting_reply_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FollowUpEvent(Base):
    """What was sent, received or skipped, per follow-up: answers "did anyone tell them?"."""
    __tablename__ = "follow_up_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    follow_up_id: Mapped[int | None] = mapped_column(ForeignKey("follow_ups.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    channel: Mapped[str] = mapped_column(String(20))        # whatsapp | voice
    direction: Mapped[str] = mapped_column(String(5))       # out | in
    status: Mapped[str] = mapped_column(String(20))         # sent | dry_run | skipped | failed | reply
    detail: Mapped[str | None] = mapped_column(Text)        # the reason, or a short note: never a phone number
    provider_sid: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class VoiceCall(Base):
    """One follow-up phone call (an AI conversation through Twilio ConversationRelay)."""
    __tablename__ = "voice_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    follow_up_ids: Mapped[list] = mapped_column(JSONB)       # the follow-ups this call is about
    call_sid: Mapped[str | None] = mapped_column(String(64), index=True)
    # placing | dry_run | ringing | in_progress | completed | no_answer | busy | failed | canceled
    status: Mapped[str] = mapped_column(String(20), default="placing")
    # handling | remind_later | wrong_person | needs_human | none (nothing was agreed)
    outcome: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[list | None] = mapped_column(JSONB)   # [{"role": "assistant"|"user", "text": ...}], capped
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WhatsAppSession(Base):
    """When a number last wrote to us. WhatsApp allows free-form messages only for 24 hours after that;
    later ones must be an approved template."""
    __tablename__ = "whatsapp_sessions"
    number: Mapped[str] = mapped_column(String(20), primary_key=True)      # +919876543210
    last_inbound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WhatsAppMessage(Base):
    """One incoming WhatsApp message (Twilio's MessageSid is unique, so a webhook Twilio retries is handled
    once). Holds no text: only who (masked), what kind and how many attachments (D-106)."""
    __tablename__ = "whatsapp_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_sid: Mapped[str] = mapped_column(String(64), unique=True)
    sender: Mapped[str] = mapped_column(String(40))                      # masked: whatsapp:+91******3210
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)   # None = unknown sender
    kind: Mapped[str] = mapped_column(String(20))                        # files | text | ignored
    text_length: Mapped[int] = mapped_column(Integer, default=0)
    media_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class WhatsAppMedia(Base):
    """One attachment of a WhatsApp message and what became of it. A download that failed for a passing
    reason (network, Twilio, database) is tried again later from this row (app/whatsapp.py retry_due)."""
    __tablename__ = "whatsapp_media"
    __table_args__ = (UniqueConstraint("message_id", "position"), Index("ix_whatsapp_media_retry", "status", "next_try_at"))
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("whatsapp_messages.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(Text)                               # api.twilio.com media URL (needs our credentials)
    content_type: Mapped[str] = mapped_column(String(100))
    # pending | ingested | duplicate | rejected (the file itself is not usable) | failed (will be tried again) | gave_up
    status: Mapped[str] = mapped_column(String(20), default="pending")
    reason: Mapped[str | None] = mapped_column(String(300))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_try_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Supplier(Base):
    """Supplier master, one row per GSTIN, made when its first invoice is approved. Holds the bank account
    payments go to: set from the first approved invoice that shows one, changed only by a person who
    confirmed the change with the supplier (app/pipeline/supplier.py, D-104)."""
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    gstin: Mapped[str] = mapped_column(String(15), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    pan: Mapped[str | None] = mapped_column(String(10))                   # characters 3-12 of the GSTIN
    bank_account: Mapped[str | None] = mapped_column(String(34))
    bank_ifsc: Mapped[str | None] = mapped_column(String(11))
    bank_set_by: Mapped[str | None] = mapped_column(String(100))          # "auto" (first invoice) or a user
    bank_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bank_source_invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    # Payment terms (D-110): days after the invoice date; none = PAYMENT_DAYS. MSME: micro | small | medium
    payment_terms_days: Mapped[int | None] = mapped_column(Integer)
    msme_category: Mapped[str | None] = mapped_column(String(10))
    udyam_number: Mapped[str | None] = mapped_column(String(19))     # UDYAM-XX-00-0000000
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SupplierEvent(Base):
    """What happened to a supplier's record: bank details recorded, a change held, a change confirmed."""
    __tablename__ = "supplier_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))        # created | bank_recorded | bank_confirmed
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    actor: Mapped[str] = mapped_column(String(100), default="system")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Gstr2bReturn(Base):
    """One uploaded GSTR-2B (the GST portal's monthly statement of what suppliers filed for us). Uploading the
    same GSTIN and month again replaces it (app/gstr2b.py, D-105)."""
    __tablename__ = "gstr2b_returns"
    __table_args__ = (UniqueConstraint("gstin", "period"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    gstin: Mapped[str] = mapped_column(String(15))                 # ours (the recipient)
    period: Mapped[str] = mapped_column(String(7))                 # YYYY-MM
    generated_on: Mapped[str | None] = mapped_column(String(10))   # as the portal states it
    file_name: Mapped[str] = mapped_column(String(255))
    file_hash: Mapped[str] = mapped_column(String(64))
    documents: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Gstr2bDocument(Base):
    """One supplier document listed in a GSTR-2B: an invoice, or a credit/debit note."""
    __tablename__ = "gstr2b_documents"
    __table_args__ = (Index("ix_gstr2b_documents_match", "supplier_gstin", "number_norm"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    return_id: Mapped[int] = mapped_column(ForeignKey("gstr2b_returns.id", ondelete="CASCADE"), index=True)
    section: Mapped[str] = mapped_column(String(10))               # b2b | b2ba | cdnr | cdnra
    doc_type: Mapped[str] = mapped_column(String(15))              # invoice | credit_note | debit_note
    supplier_gstin: Mapped[str] = mapped_column(String(15))
    supplier_name: Mapped[str | None] = mapped_column(String(255))
    number: Mapped[str] = mapped_column(String(100))
    number_norm: Mapped[str] = mapped_column(String(100))
    doc_date: Mapped[str | None] = mapped_column(String(10))       # YYYY-MM-DD
    taxable: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    igst: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    cgst: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    sgst: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    cess: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    total: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    itc_available: Mapped[bool] = mapped_column(default=True)
    itc_reason: Mapped[str | None] = mapped_column(String(100))
    reverse_charge: Mapped[bool] = mapped_column(default=False)
    supplier_filed_on: Mapped[str | None] = mapped_column(String(10))


class Branch(Base):
    """A place of the company: head office, a branch, a warehouse (D-111). Its GSTIN is the registration
    used there (one per state); goods are delivered to branches and received there (GRNs)."""
    __tablename__ = "branches"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)       # HYD, BLR-WH
    name: Mapped[str] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(60))
    state_code: Mapped[str | None] = mapped_column(String(2))        # GST state code, 36 = Telangana
    gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    address: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PurchaseOrder(Base):
    """What the company ordered from a supplier (D-112). Received quantities come from confirmed GRNs,
    invoiced ones from matched invoices: both are added up when needed, never stored twice."""
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_number: Mapped[str] = mapped_column(String(50))
    po_number_norm: Mapped[str] = mapped_column(String(50), unique=True)
    supplier_gstin: Mapped[str | None] = mapped_column(String(15), index=True)
    supplier_name: Mapped[str] = mapped_column(String(255))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"))   # ordering / billing branch
    po_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(10), default="goods")      # goods | services
    # OPEN | PARTIALLY_RECEIVED | RECEIVED | CLOSED (no more expected) | CANCELLED
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PoLine(Base):
    __tablename__ = "po_lines"
    __table_args__ = (UniqueConstraint("po_id", "line_no"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(500))
    hsn_sac: Mapped[str | None] = mapped_column(String(10))
    quantity: Mapped[float] = mapped_column(Numeric(14, 3))
    unit: Mapped[str | None] = mapped_column(String(20))
    unit_price: Mapped[float] = mapped_column(Numeric(14, 2))            # before tax
    gst_rate: Mapped[float | None] = mapped_column(Numeric(5, 2))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"))   # delivered to; none = the PO's


class GoodsReceipt(Base):
    """A GRN (D-112): what a branch received against a PO, or a service confirmed as done. Counts for
    matching only once CONFIRMED."""
    __tablename__ = "goods_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"))
    received_on: Mapped[date] = mapped_column(Date)
    challan_number: Mapped[str | None] = mapped_column(String(50))     # supplier's delivery challan
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")     # DRAFT | CONFIRMED | CANCELLED
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(100))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(10), default="api")      # api | whatsapp
    confirmed_by: Mapped[str | None] = mapped_column(String(100))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GrnLine(Base):
    __tablename__ = "grn_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    grn_id: Mapped[int] = mapped_column(ForeignKey("goods_receipts.id", ondelete="CASCADE"), index=True)
    po_line_id: Mapped[int] = mapped_column(ForeignKey("po_lines.id"), index=True)
    received_qty: Mapped[float] = mapped_column(Numeric(14, 3))
    accepted_qty: Mapped[float] = mapped_column(Numeric(14, 3))
    rejected_qty: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    reason: Mapped[str | None] = mapped_column(Text)                    # why rejected: damaged, wrong item


class InvoicePoLine(Base):
    """Which PO line an invoice line was matched to, and how much of it the invoice bills (D-112)."""
    __tablename__ = "invoice_po_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    invoice_line: Mapped[int] = mapped_column(Integer)                  # 1-based, as in the Excel sheet
    po_line_id: Mapped[int] = mapped_column(ForeignKey("po_lines.id"), index=True)
    quantity: Mapped[float] = mapped_column(Numeric(14, 3))
    unit_price: Mapped[float] = mapped_column(Numeric(14, 2))


class Approval(Base):
    """One person's approval of an invoice (D-108). Invoices above SECOND_APPROVAL_ABOVE need two, by
    different people. Cleared when the invoice is corrected or read again: the values approved changed."""
    __tablename__ = "approvals"
    __table_args__ = (UniqueConstraint("invoice_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    user_name: Mapped[str] = mapped_column(String(100))
    amount: Mapped[float | None] = mapped_column(Numeric(14, 2))     # the total when approved
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def audit(session, invoice_id: int, stage: str, message: str, data: dict | None = None, actor="system"):
    session.add(AuditLog(invoice_id=invoice_id, stage=stage, message=message, data=data, actor=actor))
