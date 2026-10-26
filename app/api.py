"""Run: uvicorn app.api:app --reload   (docs at http://localhost:8000/docs)

This one command also starts the worker and the Gmail listener (app/background.py).
No login yet: the acting user is passed in the X-User-Id header.
"""
import asyncio
import html

import anyio
import json
import logging
import threading
import time

import pymupdf
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, UploadFile, File, Depends, Header, HTTPException, APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, EmailStr, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app import gmail
from app.background import Background
from app.config import settings
from app.db import Base, SessionLocal, engine, get_session
from app.excel import try_export
from app.logs import setup_logging
from app.documents import PDF_LOCK, expand
from app.ingest import ingest_document, is_accepted
from app import chat_store
from app.models import (Invoice, AuditLog, ChatMessage, ChatSession, EmailMessage, PdfPassword, User, Status,
                        audit)
from app.pipeline.evaluate import evaluate
from app.pipeline.supplier import list_suppliers
from app.schemas import InvoiceCorrection, from_stored


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()   # worker/Gmail logs next to uvicorn's; LOG_LEVEL=DEBUG for more
    # Blocking endpoints (files, Excel, Gmail) run in a thread pool: room for many users at once
    anyio.to_thread.current_default_thread_limiter().total_tokens = settings.api_threads
    Base.metadata.create_all(engine)   # use Alembic migrations once the schema settles
    background = Background()
    background.start()                 # worker + Gmail listener, in this process
    yield
    background.shutdown()


log = logging.getLogger("api")
app = FastAPI(title="Invoice Processing", lifespan=lifespan)
api = APIRouter(prefix="/api/v1")


def current_user(x_user_id: int = Header(..., description="Acting user's id (no login yet)"),
                 session: Session = Depends(get_session)) -> User:
    user = session.get(User, x_user_id)
    if user is None:
        log.warning("Request with unknown X-User-Id %s", x_user_id)
        raise HTTPException(401, f"Unknown user {x_user_id}")
    session.commit()   # ends the read: the connection goes back to the pool while the request works
    return user


def _404(what="Invoice"):
    raise HTTPException(404, f"{what} not found")


def _locked_invoice(session: Session, invoice_id: int) -> Invoice:
    """The invoice, locked until the request commits: a second approve/reject/correct of the
    same invoice waits, then sees the new status (and gets a 409)."""
    inv = session.scalar(select(Invoice).where(Invoice.id == invoice_id).with_for_update()
                         .execution_options(populate_existing=True))
    return inv or _404()


# ---------- users & Gmail

class NewUser(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: EmailStr


def serialize_user(u: User) -> dict:
    from app import gmail_health
    session = object_session(u)
    health = gmail_health.health_of(session, u) if session is not None and u.gmail_account_id else None
    return {"id": u.id, "name": u.name, "email": u.email,
            "gmail": {"connected": bool(u.gmail_account_id), "trigger_id": u.gmail_trigger_id,
                      "connected_at": u.gmail_connected_at, "last_sync_at": u.gmail_last_sync_at,
                      # "reconnect" = Gmail access expired or was removed: connect again
                      "health": gmail_health.serialize(health)}}


@api.post("/users", status_code=201)
def create_user(body: NewUser, session: Session = Depends(get_session)):
    if not body.name.strip():
        raise HTTPException(422, "Name can't be blank")
    user = User(name=body.name.strip(), email=body.email.lower())
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        raise HTTPException(409, "A user with this email already exists")
    log.info("User %s created: %s <%s>", user.id, user.name, user.email)
    return serialize_user(user)


@api.get("/users")
def list_users(session: Session = Depends(get_session)):
    return [serialize_user(u) for u in session.scalars(select(User).order_by(User.id))]


@api.get("/users/{user_id}")
def get_user(user_id: int, session: Session = Depends(get_session)):
    return serialize_user(session.get(User, user_id) or _404("User"))


def _composio_call(fn, *args):
    try:
        return fn(*args)
    except gmail.GmailNotConnected as e:
        raise HTTPException(409, str(e))
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Composio call %s failed", fn.__name__)
        from app import gmail_health
        if len(args) >= 2 and isinstance(args[1], User) and gmail_health.looks_like_auth_error(e):
            args[0].rollback()
            h = gmail_health.check(args[0], args[1])   # expired access: say so plainly
            if h is not None and h.status == gmail_health.RECONNECT:
                raise HTTPException(409, f"{h.detail}. Use Connect Gmail to fix it.") from e
        raise HTTPException(502, f"Composio error: {e}") from e


@api.post("/users/{user_id}/gmail/connect")
def gmail_connect(user_id: int, session: Session = Depends(get_session)):
    """Open the returned URL and allow Gmail access. Gmail is activated automatically
    when you finish (no further call needed)."""
    user = session.get(User, user_id) or _404("User")
    return {"connect_url": _composio_call(gmail.connect_link, user),
            "next": "Open connect_url and allow access; Gmail is activated automatically."}


@api.get("/users/{user_id}/gmail/callback", response_class=HTMLResponse, include_in_schema=False)
def gmail_callback(user_id: int, status: str | None = None, session: Session = Depends(get_session)):
    """Where Composio sends the browser after the Gmail consent screen."""
    user = session.get(User, user_id) or _404("User")
    log.info("Gmail callback for user %s (status=%s)", user_id, status)
    try:
        gmail.activate(session, user)
    except gmail.GmailNotConnected:
        return _page("Gmail not connected", "Access was not granted. Close this tab and try connecting again.")
    except Exception as e:
        session.rollback()
        log.exception("Activation from callback failed for user %s", user_id)
        return _page("Gmail connected, activation failed",
                     f"{e}. Retry with POST /api/v1/users/{user_id}/gmail/activate.")
    return _page("Gmail connected", f"Invoices emailed to {user.name}'s inbox will now be processed "
                                    "automatically. You can close this tab.")


def _page(title: str, body: str) -> str:
    title, body = html.escape(title), html.escape(body)   # user names and errors are not trusted HTML
    return (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
            f"<body style='font-family:system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem'>"
            f"<h2>{title}</h2><p>{body}</p></body>")


@api.post("/users/{user_id}/gmail/activate")
def gmail_activate(user_id: int, session: Session = Depends(get_session)):
    """Normally automatic after connecting; call it to retry or re-activate."""
    user = session.get(User, user_id) or _404("User")
    return serialize_user(_composio_call(gmail.activate, session, user))


@api.post("/users/{user_id}/gmail/check")
def gmail_check(user_id: int, session: Session = Depends(get_session)):
    """Ask Composio now whether this user's Gmail access still works."""
    from app import gmail_health
    user = session.get(User, user_id) or _404("User")
    if not user.gmail_account_id:
        raise HTTPException(409, "Gmail is not connected for this user")
    if gmail_health.check(session, user) is None:
        raise HTTPException(503, "Couldn't reach Composio to check; try again shortly")
    session.refresh(user)
    return serialize_user(user)


@api.post("/users/{user_id}/gmail/sync")
def gmail_sync(user_id: int, days: int | None = Query(None, ge=1, le=90),
               session: Session = Depends(get_session)):
    """Fetch emails now: since the last sync, or the last `days` days."""
    user = session.get(User, user_id) or _404("User")
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    ids = _composio_call(gmail.sync_user, session, user, since)
    return {"new_invoice_ids": ids}


@api.post("/users/{user_id}/gmail/reconcile")
def gmail_reconcile(user_id: int, days: int = Query(7, ge=1, le=90), session: Session = Depends(get_session)):
    """Check the last `days` days of Gmail against the email log (runs daily by itself):
    emails never seen and failed ones are processed now; returns what was found."""
    user = session.get(User, user_id) or _404("User")
    return _composio_call(gmail.reconcile_user, session, user, days)


@api.get("/gmail/status")
def gmail_status(session: Session = Depends(get_session)):
    """Is the Gmail automation alive? Listener connection, last event, last sync, per inbox."""
    from app import gmail_listener   # the listener's live status (this process)
    inboxes = []
    for u in session.scalars(select(User).where(User.gmail_account_id.is_not(None)).order_by(User.id)):
        last = session.scalar(select(EmailMessage).where(EmailMessage.user_id == u.id)
                              .order_by(EmailMessage.created_at.desc()).limit(1))
        from app import gmail_health
        inboxes.append({"user_id": u.id, "name": u.name, "trigger_id": u.gmail_trigger_id,
                        "health": gmail_health.serialize(gmail_health.health_of(session, u)),
                        "last_sync_at": u.gmail_last_sync_at,
                        "last_email_logged_at": last.created_at if last else None,
                        "last_email_subject": last.subject if last else None})
    return {"listener": {**gmail_listener.status,
                         "enabled": settings.run_gmail_listener and bool(settings.composio_api_key)},
            "sync_every_minutes": gmail_listener.SYNC_EVERY_MINUTES, "inboxes": inboxes}


# ---------- email log

def serialize_email(e: EmailMessage, statuses: dict[int, str]) -> dict:
    attachments = []
    for a in e.attachments or []:
        ids = a.get("invoice_ids") or ([a["invoice_id"]] if a.get("invoice_id") else [])
        attachments.append({**a, "invoices": [{"invoice_id": i, "status": statuses.get(i)} for i in ids]})
    return {"id": e.id, "user_id": e.user_id, "message_id": e.message_id, "sender": e.sender,
            "subject": e.subject, "received_at": e.received_at, "folders": e.folders, "outcome": e.outcome,
            "error": e.error, "gmail_label": e.ap_label, "attachments": attachments}


def _statuses(session: Session, emails: list[EmailMessage]) -> dict[int, str]:
    ids = {i for e in emails for a in (e.attachments or [])
           for i in (a.get("invoice_ids") or ([a["invoice_id"]] if a.get("invoice_id") else []))}
    return dict(session.execute(select(Invoice.id, Invoice.status).where(Invoice.id.in_(ids))).all()) if ids else {}


@api.get("/emails")
def list_emails(user_id: int | None = None, outcome: str | None = None,
                limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                session: Session = Depends(get_session)):
    """Every Gmail message seen and what happened to each attachment (newest first).
    outcome: done | no_documents | skipped | failed."""
    q = select(EmailMessage).order_by(EmailMessage.received_at.desc().nulls_last(), EmailMessage.id.desc())
    if user_id:
        q = q.where(EmailMessage.user_id == user_id)
    if outcome:
        q = q.where(EmailMessage.outcome == outcome)
    emails = session.scalars(q.limit(limit).offset(offset)).all()
    statuses = _statuses(session, emails)
    return [serialize_email(e, statuses) for e in emails]


@api.get("/emails/{email_id}")
def get_email(email_id: int, session: Session = Depends(get_session)):
    e = session.get(EmailMessage, email_id) or _404("Email")
    return {**serialize_email(e, _statuses(session, [e])), "body_snippet": e.body_snippet}


# ---------- suppliers (built from approved invoices)

@api.get("/suppliers")
def suppliers(session: Session = Depends(get_session)):
    """Every supplier with approved invoices: what they usually bill, for the reviewer's context."""
    return list_suppliers(session)


# ---------- invoices

def serialize(inv: Invoice) -> dict:
    return {
        "invoice_id": inv.id, "status": inv.status, "status_reason": inv.status_reason,
        "user_id": inv.user_id, "source": inv.source, "sender": inv.sender,
        "email_subject": inv.email_subject, "file_name": inv.original_filename,
        "supplier_name": inv.supplier_name, "supplier_gstin": inv.supplier_gstin,
        "invoice_no": inv.invoice_no, "invoice_date": inv.invoice_date,
        "grand_total": float(inv.grand_total) if inv.grand_total is not None else None,
        "duplicate_of": inv.duplicate_of_id, "approved_by": inv.approved_by,
        "approved_at": inv.approved_at, "exported_at": inv.exported_at,
        "checks": inv.checks, "error": inv.error, "created_at": inv.created_at,
    }


@api.post("/invoices/upload", status_code=201)
def upload(file: UploadFile = File(...), user: User = Depends(current_user),
           session: Session = Depends(get_session)):
    # A plain def: FastAPI runs it in a worker thread, so the database and disk work
    # here doesn't block other requests
    if not is_accepted(file.filename or ""):
        raise HTTPException(400, "Supported: PDF, PNG, JPG, WEBP, Excel (.xlsx), CSV, Word (.docx), "
                                 "and .zip / .eml files containing them")
    limit = settings.max_upload_mb * 1024 * 1024
    content = file.file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB")
    if not content:
        raise HTTPException(400, "The file is empty")
    log.info("Upload by user %s: '%s' (%d bytes)", user.id, file.filename, len(content))
    documents, skipped = expand(file.filename, content, limit)   # a .zip/.eml gives several
    if not documents:
        raise HTTPException(400, f"No supported documents in {file.filename}: {', '.join(skipped)}")
    results = []
    for name, data in documents:
        inv, created = ingest_document(session, data, Path(name).name, "upload", user.id, sender=user.email,
                                       source_ref=f"upload:{name}"[:255] if name != file.filename else None)
        results.append({"invoice_id": inv.id, "status": inv.status, "duplicate_file": not created,
                        "file_name": Path(name).name})
    # First document in the old shape (the frontend reads these), all of them in "documents"
    return {**results[0], "documents": results, "skipped": skipped}


@api.get("/invoices")
def list_invoices(status: str | None = None, user_id: int | None = None, source: str | None = None,
                  limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                  session: Session = Depends(get_session)):
    q = select(Invoice).order_by(Invoice.created_at.desc(), Invoice.id.desc()).limit(limit).offset(offset)
    if status:
        q = q.where(Invoice.status == status.upper())
    if user_id:
        q = q.where(Invoice.user_id == user_id)
    if source:
        q = q.where(Invoice.source == source)
    return [serialize(i) for i in session.scalars(q)]


@api.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, session: Session = Depends(get_session)):
    inv = session.get(Invoice, invoice_id) or _404()
    logs = session.scalars(select(AuditLog).where(AuditLog.invoice_id == invoice_id)
                           .order_by(AuditLog.created_at, AuditLog.id)).all()
    return {**serialize(inv), "extracted": inv.extracted,
            "audit": [{"stage": l.stage, "actor": l.actor, "message": l.message,
                       "data": l.data, "at": l.created_at} for l in logs]}


@api.get("/invoices/{invoice_id}/file")
def get_invoice_file(invoice_id: int, session: Session = Depends(get_session)):
    """The original document, for the reviewer."""
    inv = session.get(Invoice, invoice_id) or _404()
    if not Path(inv.file_path).exists():
        _404("File")
    return FileResponse(inv.file_path, filename=inv.original_filename)


@api.patch("/invoices/{invoice_id}")
def correct(invoice_id: int, body: InvoiceCorrection, user: User = Depends(current_user),
            session: Session = Depends(get_session)):
    """Fix wrongly extracted fields. The checks run again; approve afterwards."""
    inv = _locked_invoice(session, invoice_id)
    if inv.status not in (Status.NEEDS_REVIEW, Status.DUPLICATE) or not inv.extracted:
        raise HTTPException(409, f"Cannot correct an invoice in {inv.status}")
    changes = body.model_dump(exclude_unset=True, exclude={"note"})
    if not changes:
        raise HTTPException(400, "No fields to change")
    before = inv.extracted
    log.info("Invoice %s corrected by %s: %s", inv.id, user.name, {k: (before.get(k), v) for k, v in changes.items()
                                                                  if k != "lines"} or "lines")
    # The reviewer has checked the values, so the AI's own uncertainty no longer applies
    try:
        data = from_stored({**before, **changes, "confidence": 1.0, "low_confidence_fields": []})
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        raise HTTPException(422, f"Invalid correction: {problems}")
    audit(session, inv.id, "correct", f"Corrected {', '.join(changes)}. {body.note}".strip(),
          {"before": {k: before.get(k) for k in changes}, "after": changes}, actor=user.name)
    evaluate(session, inv, data, actor=user.name, after_correction=True)
    session.commit()
    return serialize(inv)


class Review(BaseModel):
    note: str = ""
    # Act only if the invoice hasn't changed since then (an assistant's suggestion, an old screen)
    if_unchanged_since: datetime | None = None


def _unchanged(inv: Invoice, body: Review) -> None:
    since = body.if_unchanged_since
    if since is not None and inv.updated_at and inv.updated_at > since + timedelta(milliseconds=1):
        raise HTTPException(409, f"Invoice #{inv.id} changed after this was suggested (now {inv.status}); "
                                 f"open it and check before acting")


def _transition(session, inv: Invoice, allowed: set[str], new_status: str, user: User, note: str):
    if inv.status not in allowed:
        log.warning("Invoice %s: %s tried %s -> %s (not allowed)", inv.id, user.name, inv.status, new_status)
        raise HTTPException(409, f"Cannot move from {inv.status} to {new_status}")
    old = inv.status
    inv.status = new_status
    log.info("Invoice %s: %s -> %s by %s%s", inv.id, old, new_status, user.name, f" ({note})" if note else "")
    audit(session, inv.id, "review", f"{old} -> {new_status}. {note}".strip(), actor=user.name)


@api.post("/invoices/{invoice_id}/approve")
def approve(invoice_id: int, body: Review = Review(), user: User = Depends(current_user),
            session: Session = Depends(get_session)):
    """Approve and write to Excel right away (or as soon as the workbook is closed)."""
    inv = _locked_invoice(session, invoice_id)
    _unchanged(inv, body)
    _transition(session, inv, {Status.NEEDS_REVIEW}, Status.APPROVED, user, body.note)
    inv.approved_by = user.name
    inv.approved_at = datetime.now(timezone.utc)
    inv.status_reason = f"Approved by {user.name}"
    session.commit()
    error = try_export(session, wait=False)   # an export already running writes it within seconds
    session.refresh(inv)
    return {**serialize(inv), "excel_error": error}


@api.post("/invoices/{invoice_id}/reject")
def reject(invoice_id: int, body: Review = Review(), user: User = Depends(current_user),
           session: Session = Depends(get_session)):
    inv = _locked_invoice(session, invoice_id)
    _unchanged(inv, body)
    _transition(session, inv, {Status.NEEDS_REVIEW, Status.DUPLICATE, Status.NOT_INVOICE, Status.NEEDS_PASSWORD},
                Status.REJECTED, user, body.note)
    inv.status_reason = f"Rejected by {user.name}" + (f": {body.note}" if body.note else "")
    # Copies and journals marked as duplicates of this one would otherwise never be
    # recorded: send them back to review
    for dup in session.scalars(select(Invoice).where(Invoice.duplicate_of_id == inv.id,
                                                     Invoice.status == Status.DUPLICATE)):
        dup.status = Status.NEEDS_REVIEW
        dup.duplicate_of_id = None
        dup.status_reason = f"Was a duplicate of #{inv.id}, which {user.name} rejected; please check this one"
        audit(session, dup.id, "review", dup.status_reason, actor=user.name)
        log.info("Invoice %s back to review: its original #%s was rejected", dup.id, inv.id)
    session.commit()
    return serialize(inv)


@api.post("/invoices/{invoice_id}/reprocess")
def reprocess(invoice_id: int, body: Review = Review(), user: User = Depends(current_user),
              session: Session = Depends(get_session)):
    """Run the AI extraction again (e.g. after a misread or a technical failure)."""
    inv = _locked_invoice(session, invoice_id)
    _unchanged(inv, body)
    _transition(session, inv, {Status.NEEDS_REVIEW, Status.FAILED, Status.NOT_INVOICE, Status.DUPLICATE,
                               Status.REJECTED, Status.NEEDS_PASSWORD}, Status.RECEIVED, user, body.note)
    inv.attempts = 0
    inv.error = None
    inv.status_reason = None
    session.commit()
    return serialize(inv)


@api.post("/invoices/{invoice_id}/process-as-invoice")
def process_as_invoice(invoice_id: int, body: Review = Review(), user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """Override the AI: check a NOT_INVOICE document as an invoice. It goes to review,
    where missing fields can be corrected (PATCH) before approving. No new AI call."""
    inv = _locked_invoice(session, invoice_id)
    _unchanged(inv, body)
    if inv.status != Status.NOT_INVOICE or not inv.extracted:
        raise HTTPException(409, f"Only NOT_INVOICE documents can be processed as invoices (this is {inv.status})")
    data = from_stored({**inv.extracted, "document_type": "tax_invoice"})
    log.info("Invoice %s: %s overrides AI (%s) -> process as invoice%s", inv.id, user.name,
             inv.extracted.get("document_type"), f" ({body.note})" if body.note else "")
    audit(session, inv.id, "review", f"Processed as invoice (AI said {inv.extracted.get('document_type')}). "
                                     f"{body.note}".strip(), actor=user.name)
    evaluate(session, inv, data, actor=user.name, after_correction=True)
    session.commit()
    return serialize(inv)


class PdfPasswordBody(BaseModel):
    password: str = Field(min_length=1, max_length=200)


@api.post("/invoices/{invoice_id}/password")
def unlock_pdf(invoice_id: int, body: PdfPasswordBody, user: User = Depends(current_user),
               session: Session = Depends(get_session)):
    """Password for a protected PDF. It is checked on the file, remembered for future PDFs,
    and the invoice goes back to the queue to be read."""
    inv = _locked_invoice(session, invoice_id)
    if inv.status != Status.NEEDS_PASSWORD:
        raise HTTPException(409, f"Invoice {invoice_id} is {inv.status}, not waiting for a password")
    with PDF_LOCK, pymupdf.open(inv.file_path) as doc:   # MuPDF: one thread at a time
        if not doc.authenticate(body.password):
            raise HTTPException(400, "Wrong password for this PDF")
    session.execute(pg_insert(PdfPassword).values(password=body.password, sender=inv.sender)
                    .on_conflict_do_update(index_elements=[PdfPassword.password],
                                           set_={"last_used_at": datetime.now(timezone.utc)}))
    _transition(session, inv, {Status.NEEDS_PASSWORD}, Status.RECEIVED, user, "PDF password entered")
    inv.attempts, inv.status_reason = 0, None
    session.commit()
    return serialize(inv)


# ---------- AI assistant

class ChatTurn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=20000)


class ChatRequest(BaseModel):
    messages: list[ChatTurn] = Field(min_length=1, max_length=100)


@api.post("/assistant/chat")
async def assistant_chat(body: ChatRequest, user: User = Depends(current_user)):
    """Ask the assistant about invoices, suppliers, GST, the Excel workbook and the inboxes.
    Send the whole conversation; the reply comes with the tools it used, files to download and
    proposed actions (for the person to confirm)."""
    from app import assistant
    try:
        return await assistant.chat([m.model_dump() for m in body.messages], user.name)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    except assistant.AssistantUnavailable as e:
        raise HTTPException(503, f"The AI service is unavailable right now: {e}") from e
    except Exception as e:
        log.exception("Assistant failed")
        raise HTTPException(502, f"The assistant couldn't answer: {e}") from e


# Saved conversations (each user sees only their own)

class NewChat(BaseModel):
    title: str = Field("New chat", max_length=120)


class ChatTitle(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ChatQuestion(BaseModel):
    content: str = Field(min_length=1, max_length=20000, pattern=r"\S")   # not only spaces


class ActionDone(BaseModel):
    label: str = Field(min_length=1, max_length=120)


def _owned_chat(session: Session, user: User, chat_id: int):
    chat = chat_store.get_owned(session, user.id, chat_id)
    if chat is None:
        _404("Chat")
    return chat


@api.get("/assistant/sessions")
def chat_sessions(user: User = Depends(current_user), session: Session = Depends(get_session)):
    """The current user's saved conversations, most recent first."""
    return chat_store.list_sessions(session, user.id)


@api.post("/assistant/sessions", status_code=201)
def new_chat_session(body: NewChat = NewChat(), user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    chat = ChatSession(user_id=user.id, title=body.title.strip() or "New chat")
    session.add(chat)
    session.commit()
    return chat_store.serialize_session(chat, 0)


@api.get("/assistant/sessions/{chat_id}")
def get_chat_session(chat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    chat = _owned_chat(session, user, chat_id)
    msgs = chat_store.messages(session, chat.id)
    return {**chat_store.serialize_session(chat, len(msgs)),
            "messages": [chat_store.serialize_message(m) for m in msgs]}


@api.patch("/assistant/sessions/{chat_id}")
def rename_chat_session(chat_id: int, body: ChatTitle, user: User = Depends(current_user),
                        session: Session = Depends(get_session)):
    chat = _owned_chat(session, user, chat_id)
    chat.title = body.title.strip()
    session.commit()
    return chat_store.serialize_session(chat)


@api.delete("/assistant/sessions/{chat_id}", status_code=204)
def delete_chat_session(chat_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    chat = _owned_chat(session, user, chat_id)
    session.query(ChatMessage).filter(ChatMessage.session_id == chat.id).delete()
    session.delete(chat)
    session.commit()


def _start_turn(user_id: int, chat_id: int, content: str) -> tuple[int, list[dict]]:
    """Save the question; returns (chat id, the conversation for the AI). Short DB work only."""
    with SessionLocal() as session:
        chat = chat_store.get_owned(session, user_id, chat_id)
        if chat is None:
            _404("Chat")
        chat_store.add_message(session, chat, "user", content)
        return chat.id, chat_store.history(session, chat.id)


def _save_turn(chat_id: int, role: str, text: str, meta: dict | None = None) -> tuple[dict, str] | None:
    """Save an answer (or error); None if the chat was deleted meanwhile."""
    with SessionLocal() as session:
        chat = session.get(ChatSession, chat_id)
        if chat is None:
            return None
        m = chat_store.add_message(session, chat, role, text, meta)
        return chat_store.serialize_message(m), chat.title


def _error_text(assistant, e: Exception) -> str:
    if isinstance(e, assistant.AssistantUnavailable):
        return f"The AI service is unavailable right now: {e}"
    return f"The assistant couldn't answer: {e}"


ANSWER_META = ("tools_used", "files", "actions", "model")


@api.post("/assistant/sessions/{chat_id}/messages")
async def ask_in_chat(chat_id: int, body: ChatQuestion, user: User = Depends(current_user)):
    """Ask a question in a saved conversation; the question and the answer are saved."""
    from app import assistant
    chat_pk, convo = await run_in_threadpool(_start_turn, user.id, chat_id, body.content)
    try:
        r = await assistant.chat(convo, user.name)
    except Exception as e:
        if not isinstance(e, assistant.AssistantUnavailable):
            log.exception("Assistant failed")
        text = _error_text(assistant, e)
        await run_in_threadpool(_save_turn, chat_pk, "error", text)
        raise HTTPException(503 if isinstance(e, assistant.AssistantUnavailable) else 502, text) from e
    saved = await run_in_threadpool(_save_turn, chat_pk, "assistant", r["reply"], {k: r[k] for k in ANSWER_META})
    if saved is None:
        _404("Chat")
    return saved[0]


@api.post("/assistant/sessions/{chat_id}/stream")
async def ask_in_chat_streamed(chat_id: int, body: ChatQuestion, user: User = Depends(current_user)):
    """Like /messages, but the answer arrives as it is written (Server-Sent Events):
    data: {"type": "tool"|"delta"|"saved"|"error", ...}
    Async: a waiting answer holds neither a server thread nor a database connection."""
    from app import assistant
    chat_pk, convo = await run_in_threadpool(_start_turn, user.id, chat_id, body.content)
    user_name = user.name

    def sse(data: dict) -> str:
        return f"data: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"

    async def events():
        written, finished = [], False
        try:
            async for ev in assistant.stream(convo, user_name):
                if ev["type"] == "delta":
                    written.append(ev["text"])
                if ev["type"] != "done":
                    yield sse(ev)
                    continue
                finished = True
                saved = await run_in_threadpool(_save_turn, chat_pk, "assistant", ev["reply"],
                                                {k: ev[k] for k in ANSWER_META})
                yield sse({"type": "error", "message": "This chat was deleted"} if saved is None
                          else {"type": "saved", "message": saved[0], "title": saved[1]})
        except (asyncio.CancelledError, GeneratorExit):
            # The browser went away mid-answer: keep what was written, so the chat doesn't end
            # with an unanswered question (saved on a thread: this task is being cancelled)
            if not finished:
                partial = "".join(written).strip()
                threading.Thread(target=_save_turn, daemon=True, args=(
                    chat_pk, "error", (partial + "\n\n" if partial else "") + "(The answer was interrupted.)")).start()
            raise
        except Exception as e:
            finished = True
            if not isinstance(e, assistant.AssistantUnavailable):
                log.exception("Assistant failed (streamed)")
            text = _error_text(assistant, e)
            try:
                saved = await run_in_threadpool(_save_turn, chat_pk, "error", text)
            except Exception:
                log.exception("Could not save the assistant error")
                saved = None
            yield sse({"type": "error", "message": text, **({"saved": saved[0]} if saved else {})})

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@api.post("/assistant/messages/{message_id}/actions/{index}/done")
def chat_action_done(message_id: int, index: int, body: ActionDone, user: User = Depends(current_user),
                     session: Session = Depends(get_session)):
    """Remember that a suggested action was carried out (so the button shows as done later)."""
    m = session.get(ChatMessage, message_id)
    if m is None or chat_store.get_owned(session, user.id, m.session_id) is None:
        _404("Message")
    try:
        return chat_store.serialize_message(chat_store.mark_action_done(session, m, index, body.label))
    except IndexError as e:
        raise HTTPException(404, str(e)) from e


@api.get("/assistant/suggestions")
def assistant_suggestions():
    from app.assistant import SUGGESTIONS
    return SUGGESTIONS


@api.get("/assistant/files/{name}")
def assistant_file(name: str):
    """A file the assistant created (export)."""
    from app.assistant_tools import SAFE_FILE
    path = Path(settings.export_dir) / name
    if not SAFE_FILE.match(name) or not path.exists():
        _404("File")
    media = "text/csv" if name.endswith(".csv") else         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(path, filename=name, media_type=media)


# ---------- Excel

# ---------- summary report by email

Period = Literal["day", "week", "month"]


class SendReport(BaseModel):
    period: Period = "week"
    recipients: list[EmailStr] = Field(default_factory=list, max_length=20)   # empty = the usual recipients


@api.get("/reports")
def report_settings(session: Session = Depends(get_session)):
    """The schedule (set in .env), who sends and receives the report, and the last reports sent."""
    from app import reports
    sender = reports.default_sender(session)
    schedule = settings.report_schedule.strip().lower()
    return {
        "schedule": schedule if schedule in reports.SCHEDULES else "off",
        "time": settings.report_time, "weekday": settings.report_weekday,
        "next_run": reports.next_run(),
        "sender": {"id": sender.id, "name": sender.name, "email": sender.email} if sender else None,
        "recipients": reports.default_recipients(session),
        "runs": reports.recent_runs(session),
    }


@api.get("/reports/preview")
def report_preview(period: Period = "week", session: Session = Depends(get_session)):
    """The report for the last day/week/month, as it would be emailed (subject + HTML)."""
    from app import reports
    return reports.preview(session, period)


@api.post("/reports/send")
def report_send(body: SendReport, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Email the report now: from your own Gmail if it is connected, else from the usual sender."""
    from app import reports
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=reports.PERIOD_DAYS[body.period])
    sender = user if user.gmail_account_id else None
    allowed = {r.lower() for r in reports.allowed_recipients(session)}
    outside = [str(r) for r in body.recipients if str(r).lower() not in allowed]
    if outside:
        raise HTTPException(422, f"Reports can only go to the addresses in REPORT_RECIPIENTS; "
                                 f"not to {', '.join(outside)}")
    run = reports.send(session, start, end, sender=sender, recipients=[str(r) for r in body.recipients] or None,
                       sent_by=user.name)
    if run.status != "sent":
        raise HTTPException(502, run.error or "The report wasn't sent")
    return reports.serialize_run(run)


@api.get("/system/status")
def system_status(session: Session = Depends(get_session)):
    """Is document reading working? The AI account state (this process's workers, and documents in
    the queue waiting because of it, from any process) and the queue sizes."""
    from app import worker
    counts = dict(session.execute(select(Invoice.status, func.count()).group_by(Invoice.status)).all())
    waiting_on_account = session.scalar(select(func.count()).where(
        Invoice.status == Status.RECEIVED, Invoice.error.like("Waiting: the OpenAI account%")))
    problem = worker.ai_status["problem"]
    if not problem and waiting_on_account:
        problem = "Documents are waiting because the OpenAI account can't be used (no credits or invalid key)"
    return {
        "ai": {"ok": not problem, "problem": problem,
               "kind": worker.ai_status["kind"] or ("account" if waiting_on_account else None),
               "since": worker.ai_status["since"], "last_ok_at": worker.ai_status["last_ok_at"],
               "waiting_on_account": waiting_on_account},
        "queue": {k: counts.get(k, 0) for k in (Status.RECEIVED, Status.PROCESSING, Status.APPROVED,
                                                 Status.NEEDS_REVIEW, Status.FAILED)},
        "workers_here": settings.worker_concurrency if settings.run_worker else 0,
    }


@api.get("/excel")
def download_excel(session: Session = Depends(get_session)):
    """The workbook of approved invoices."""
    error = try_export(session)   # include anything approved a moment ago
    path = Path(settings.excel_path)
    if not path.exists():
        raise HTTPException(404, error or "No approved invoices yet")
    # Read in one go (not streamed from disk): the worker may replace the workbook while it is
    # being sent, and a streamed file would then arrive cut off
    for attempt in range(5):
        try:
            content = path.read_bytes()
            break
        except PermissionError:   # Windows: the file is being replaced this instant
            time.sleep(0.2 * (attempt + 1))
    else:
        raise HTTPException(503, "The workbook is being updated; try again in a moment")
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{path.name}"'})


app.include_router(api)


# ---------- Composio webhook (production delivery of new-email events)

@app.post("/webhooks/composio", include_in_schema=False)
async def composio_webhook(request: Request):
    """New-email events from Composio, signed with COMPOSIO_WEBHOOK_SECRET. Only active with
    GMAIL_DELIVERY=webhook. The event is queued and handled like a websocket event."""
    from composio import exceptions as composio_errors
    from app import gmail_listener
    if not (gmail_listener.webhook_mode() and settings.composio_webhook_secret):
        raise HTTPException(404, "Webhook delivery is not enabled (GMAIL_DELIVERY=webhook + COMPOSIO_WEBHOOK_SECRET)")
    body = (await request.body()).decode("utf-8", "replace")
    try:
        result = gmail._composio().triggers.verify_webhook(
            id=request.headers.get("webhook-id", ""), payload=body,
            signature=request.headers.get("webhook-signature", ""),
            timestamp=request.headers.get("webhook-timestamp", ""), secret=settings.composio_webhook_secret)
    except composio_errors.WebhookSignatureVerificationError as e:
        log.warning("Composio webhook rejected: %s", e)
        raise HTTPException(401, "Invalid webhook signature") from e
    except composio_errors.WebhookPayloadError as e:
        log.warning("Composio webhook with an unreadable payload: %s", e)
        raise HTTPException(400, str(e)) from e
    event = dict(result["payload"])
    if "GMAIL" not in str(event.get("trigger_slug") or "").upper():
        log.info("Composio webhook for %s ignored", event.get("trigger_slug"))
        return {"ok": True, "ignored": True}
    if not str(event.get("user_id") or "").startswith("ap-user-"):
        # Older webhook formats name only the connected account: find whose inbox it is
        account = ((event.get("metadata") or {}).get("connected_account") or {}).get("id")
        with SessionLocal() as session:
            user = session.scalar(select(User).where(User.gmail_account_id == account)) if account else None
        if user is None:
            log.warning("Composio webhook for unknown connected account %r ignored", account)
            return {"ok": True, "ignored": True}
        event["user_id"] = user.composio_user_id
    event["trigger_slug"] = gmail.TRIGGER
    if gmail_listener.status.get("running"):
        gmail_listener.receive(event)          # handled on the listener's event thread
    else:                                      # listener runs in another process: handle it here
        threading.Thread(target=gmail_listener.on_event, args=(event,), name="gmail-webhook", daemon=True).start()
    return {"ok": True}
