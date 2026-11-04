"""Run: uvicorn app.api:app --reload   (docs at http://localhost:8000/docs)

This one command also starts the worker and the Gmail listener (app/background.py).
No login yet: the acting user is passed in the X-User-Id header.
"""
import asyncio
import html
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import anyio
import pymupdf
from fastapi import APIRouter, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, EmailStr, Field, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from app import chat_store, followup, gmail, gstr2b, migrate, payables, procurement, security, voice, whatsapp
from app.background import Background
from app.config import settings
from app.db import SessionLocal, get_session
from app.documents import PDF_LOCK, expand
from app.excel import find_workbook, list_workbooks, try_export
from app.ingest import ingest_document, is_accepted
from app.logs import setup_logging
from app.models import (
    Approval,
    AuditLog,
    Branch,
    ChatMessage,
    ChatSession,
    EmailMessage,
    FollowUp,
    FollowUpEvent,
    GoodsReceipt,
    Gstr2bReturn,
    Invoice,
    PdfPassword,
    PurchaseOrder,
    Status,
    Supplier,
    SupplierEvent,
    User,
    VoiceCall,
    WhatsAppMedia,
    WhatsAppMessage,
    audit,
)
from app.pipeline import supplier as supplier_master
from app.pipeline.evaluate import evaluate
from app.pipeline.validate import gstin_valid
from app.schemas import InvoiceCorrection, from_stored


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()   # worker/Gmail logs next to uvicorn's; LOG_LEVEL=DEBUG for more
    security.warn_if_open()
    # Blocking endpoints (files, Excel, Gmail) run in a thread pool: room for many users at once
    anyio.to_thread.current_default_thread_limiter().total_tokens = settings.api_threads
    migrate.upgrade_on_start()          # database migrations (Alembic)
    background = Background()
    background.start()                 # worker + Gmail listener, in this process
    yield
    background.shutdown()


log = logging.getLogger("api")
app = FastAPI(title="Invoice Processing", lifespan=lifespan)
app.add_middleware(security.ApiKeyMiddleware)
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
            "whatsapp": whatsapp.mask_sender(u.whatsapp_number) if u.whatsapp_number else None,
            "approval_limit": float(u.approval_limit) if u.approval_limit is not None else None,
            "branch_id": u.branch_id,
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
        raise HTTPException(409, "A user with this email already exists") from None
    log.info("User %s created: %s <%s>", user.id, user.name, user.email)
    return serialize_user(user)


@api.get("/users")
def list_users(session: Session = Depends(get_session)):
    return [serialize_user(u) for u in session.scalars(select(User).order_by(User.id))]


@api.get("/users/{user_id}")
def get_user(user_id: int, session: Session = Depends(get_session)):
    return serialize_user(session.get(User, user_id) or _404("User"))


class WhatsAppNumber(BaseModel):
    number: str = Field(min_length=8, max_length=30, description="e.g. +919876543210")


@api.put("/users/{user_id}/whatsapp")
def set_whatsapp_number(user_id: int, body: WhatsAppNumber, session: Session = Depends(get_session)):
    """The WhatsApp number this user sends invoices from and is reminded at. Takes effect at once (no
    restart); WHATSAPP_SENDERS in .env keeps working for numbers not stored here."""
    user = session.get(User, user_id) or _404("User")
    number = whatsapp.normalize_number(body.number)
    if number is None:
        raise HTTPException(422, "Not a phone number: use the international form, e.g. +919876543210")
    user.whatsapp_number = number
    try:
        session.commit()
    except IntegrityError:
        raise HTTPException(409, "Another user already has this WhatsApp number") from None
    log.info("User %s WhatsApp number set to %s", user.id, whatsapp.mask_sender(number))
    return serialize_user(user)


class ApprovalLimit(BaseModel):
    limit: float | None = Field(None, ge=0, description="Highest invoice total this user may approve; "
                                                         "null = any amount, 0 = may not approve")


@api.put("/users/{user_id}/approval-limit")
def set_approval_limit(user_id: int, body: ApprovalLimit, user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """The approval matrix (D-108): how much this user may approve."""
    target = session.get(User, user_id) or _404("User")
    before = target.approval_limit
    target.approval_limit = body.limit
    session.commit()
    log.info("User %s approval limit %s -> %s, set by %s", target.id, before, body.limit, user.name)
    return serialize_user(target)


@api.delete("/users/{user_id}/whatsapp")
def remove_whatsapp_number(user_id: int, session: Session = Depends(get_session)):
    user = session.get(User, user_id) or _404("User")
    user.whatsapp_number = None
    session.commit()
    log.info("User %s WhatsApp number removed", user.id)
    return serialize_user(user)


def _composio_call(fn, *args):
    try:
        return fn(*args)
    except gmail.GmailNotConnected as e:
        raise HTTPException(409, str(e)) from e
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
    from app import gmail_listener  # the listener's live status (this process)
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


# ---------- suppliers (history from approved invoices + the supplier master)

@api.get("/suppliers")
def suppliers(session: Session = Depends(get_session)):
    """Every supplier with approved invoices: what they usually bill, for the reviewer's context, and its
    record in the supplier master (PAN, the bank account payments go to)."""
    masters = {s.gstin: s for s in session.scalars(select(Supplier))}
    return [{**row, **supplier_master.serialize_master(masters.get(row["gstin"]))}
            for row in supplier_master.list_suppliers(session)]


@api.get("/suppliers/{gstin}")
def get_supplier(gstin: str, session: Session = Depends(get_session)):
    """One supplier: its record, its history and what happened to its record (bank details set or confirmed)."""
    gstin = gstin.upper()
    sup = supplier_master.get(session, gstin)
    history = supplier_master.profile(session, gstin)
    if sup is None and history is None:
        _404("Supplier")
    events = [] if sup is None else session.scalars(
        select(SupplierEvent).where(SupplierEvent.supplier_id == sup.id).order_by(SupplierEvent.id.desc()).limit(100))
    return {"gstin": gstin, "name": sup.name if sup else history.names[0], **supplier_master.serialize_master(sup),
            "invoices": history.invoices if history else 0, "highest_total": history.max_total if history else 0,
            "average_total": history.avg_total if history else 0,
            "last_invoice_date": history.last_invoice_date if history else None,
            "events": [supplier_master.serialize_event(e) for e in events]}


class BankConfirmation(BaseModel):
    bank_account: str
    bank_ifsc: str
    note: str = Field(description="How the change was confirmed: who you called, on which number from your records")
    invoice_id: int | None = Field(None, description="The invoice that showed the new details, if any")


@api.post("/suppliers/{gstin}/bank")
def confirm_supplier_bank(gstin: str, body: BankConfirmation, user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    """Record a supplier's bank account after confirming it with the supplier on a known phone number. An
    invoice held because its bank details differ can be approved once they match the recorded ones."""
    try:
        sup = supplier_master.confirm_bank(session, gstin.upper(), body.bank_account, body.bank_ifsc, body.note,
                                           user.name, body.invoice_id)
    except LookupError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    session.commit()
    log.info("Supplier %s: bank details confirmed by %s", sup.gstin, user.name)
    return {"gstin": sup.gstin, "name": sup.name, **supplier_master.serialize_master(sup)}


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
        "tds": {"section": inv.tds_section, "base": _num(inv.tds_base), "rate": _num(inv.tds_rate),
                "amount": _num(inv.tds_amount)} if inv.tds_section else None,
        "due_date": inv.due_date, "paid_on": inv.paid_on, "paid_reference": inv.paid_reference,
        "paid_by": inv.paid_by, "po_id": inv.po_id, "branch_id": inv.branch_id, "match_status": inv.match_status,
    }


def _num(x) -> float | None:
    return float(x) if x is not None else None


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
    approvals = session.scalars(select(Approval).where(Approval.invoice_id == invoice_id).order_by(Approval.id))
    return {**serialize(inv), "extracted": inv.extracted, "approvals": [serialize_approval(a) for a in approvals],
            "audit": [{"stage": ln.stage, "actor": ln.actor, "message": ln.message,
                       "data": ln.data, "at": ln.created_at} for ln in logs]}


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
        raise HTTPException(422, f"Invalid correction: {problems}") from e
    audit(session, inv.id, "correct", f"Corrected {', '.join(changes)}. {body.note}".strip(),
          {"before": {k: before.get(k) for k in changes}, "after": changes, "user_id": user.id}, actor=user.name)
    _clear_approvals(session, inv, "corrected")
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


def serialize_approval(a: Approval) -> dict:
    return {"user_id": a.user_id, "user_name": a.user_name, "amount": _num(a.amount), "note": a.note,
            "at": a.created_at}


def _clear_approvals(session: Session, inv: Invoice, why: str) -> None:
    """Approvals given so far were for other values: they no longer count."""
    gone = session.execute(delete(Approval).where(Approval.invoice_id == inv.id)).rowcount
    if gone:
        audit(session, inv.id, "review", f"{gone} approval(s) cleared: invoice {why}")


def approvals_needed(inv: Invoice) -> int:
    limit = settings.second_approval_above
    return 2 if limit and abs(float(inv.grand_total or 0)) > limit else 1


def _may_approve(session: Session, inv: Invoice, user: User) -> None:
    """The approval matrix (D-108): the user's own limit, and with MAKER_CHECKER, not their own invoice."""
    amount = abs(float(inv.grand_total or 0))
    if user.approval_limit is not None:
        limit = float(user.approval_limit)
        if limit <= 0:
            raise HTTPException(403, f"{user.name} may not approve invoices")
        if amount > limit:
            raise HTTPException(403, f"Total {amount:,.2f} is above {user.name}'s approval limit {limit:,.2f}: "
                                     f"someone with a higher limit must approve it")
    if not settings.maker_checker:
        return
    if inv.source in ("upload", "whatsapp") and inv.user_id == user.id:
        raise HTTPException(403, f"{user.name} sent this invoice in, so someone else must approve it")
    corrected = session.scalar(select(AuditLog.id).where(
        AuditLog.invoice_id == inv.id, AuditLog.stage == "correct",
        AuditLog.data["user_id"].as_integer() == user.id).limit(1))
    if corrected:
        raise HTTPException(403, f"{user.name} corrected this invoice, so someone else must approve it")


@api.post("/invoices/{invoice_id}/approve")
def approve(invoice_id: int, body: Review = Review(), user: User = Depends(current_user),
            session: Session = Depends(get_session)):
    """Approve and write to Excel right away (or as soon as the workbook is closed). Above
    SECOND_APPROVAL_ABOVE the first approval is recorded and the invoice waits for a second person."""
    inv = _locked_invoice(session, invoice_id)
    _unchanged(inv, body)
    if inv.status == Status.NEEDS_REVIEW and (hold := supplier_master.bank_hold(session, inv)):
        raise HTTPException(409, hold)
    if inv.status != Status.NEEDS_REVIEW:
        _transition(session, inv, {Status.NEEDS_REVIEW}, Status.APPROVED, user, body.note)   # raises 409
    _may_approve(session, inv, user)
    given = session.scalars(select(Approval).where(Approval.invoice_id == inv.id).order_by(Approval.id)).all()
    if any(a.user_id == user.id for a in given):
        raise HTTPException(409, f"{user.name} has already approved this invoice: another person must approve it")
    session.add(Approval(invoice_id=inv.id, user_id=user.id, user_name=user.name, amount=inv.grand_total,
                         note=body.note or None))
    names = [a.user_name for a in given] + [user.name]
    needed = approvals_needed(inv)
    if len(names) < needed:
        inv.status_reason = (f"Approved by {user.name} ({len(names)} of {needed}): totals above "
                             f"{settings.second_approval_above:,.2f} need another approver")
        audit(session, inv.id, "review", inv.status_reason + (f". {body.note}" if body.note else ""), actor=user.name)
        log.info("Invoice %s: approval %s of %s by %s", inv.id, len(names), needed, user.name)
        session.commit()
        return {**serialize(inv), "approvals_needed": needed, "approvals": len(names), "excel_error": None}
    _transition(session, inv, {Status.NEEDS_REVIEW}, Status.APPROVED, user, body.note)
    inv.approved_by = ", ".join(names)[:100]
    inv.approved_at = datetime.now(timezone.utc)
    inv.status_reason = f"Approved by {' and '.join(names)}"
    if inv.extracted:
        supplier_master.record_approved(session, inv, from_stored(inv.extracted), user.name)
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
    _clear_approvals(session, inv, "sent to be read again")
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


@app.get("/healthz", include_in_schema=False)
def healthz():
    """The process is up (for a load balancer or monitor; no key needed)."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def readyz(session: Session = Depends(get_session)):
    """The process can serve requests: the database answers. 503 otherwise."""
    try:
        session.execute(select(1))
    except Exception as e:
        log.warning("Readiness check: database unavailable (%s)", type(e).__name__)
        return JSONResponse({"status": "unavailable", "database": False}, status_code=503)
    return {"status": "ok", "database": True}


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


# ---------- GSTR-2B reconciliation (app/gstr2b.py)

@api.post("/gstr2b", status_code=201)
def upload_gstr2b(file: UploadFile = File(...), user: User = Depends(current_user),
                  session: Session = Depends(get_session)):
    """Upload a month's GSTR-2B (JSON from the GST portal). Replaces an earlier upload for the same GSTIN and
    month. Returns the reconciliation."""
    limit = settings.max_upload_mb * 1024 * 1024
    content = file.file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB")
    try:
        ret = gstr2b.store(session, content, file.filename or "gstr2b.json", user.name)
    except gstr2b.Gstr2bError as e:
        raise HTTPException(422, str(e)) from e
    session.commit()
    return gstr2b.reconcile(session, ret)


@api.get("/gstr2b")
def list_gstr2b(session: Session = Depends(get_session)):
    """Uploaded GSTR-2B returns, latest month first, each with its reconciliation summary."""
    returns = session.scalars(select(Gstr2bReturn).order_by(Gstr2bReturn.period.desc(), Gstr2bReturn.gstin))
    return [{**gstr2b.serialize(r), "summary": gstr2b.reconcile(session, r)["summary"]} for r in returns]


@api.get("/gstr2b/{return_id}")
def get_gstr2b(return_id: int, status: str | None = Query(None, description="matched | mismatch | not_in_books"),
               session: Session = Depends(get_session)):
    """The reconciliation of one GSTR-2B: every document it lists, matched with our invoices, and our invoices
    of that month it doesn't list (ITC at risk)."""
    ret = session.get(Gstr2bReturn, return_id) or _404("GSTR-2B")
    result = gstr2b.reconcile(session, ret)
    if status:
        result["documents"] = [d for d in result["documents"] if d["status"] == status]
    return result


@api.get("/gstr2b/{return_id}/excel")
def gstr2b_workbook(return_id: int, session: Session = Depends(get_session)):
    """The reconciliation as an Excel workbook (summary + every document)."""
    ret = session.get(Gstr2bReturn, return_id) or _404("GSTR-2B")
    content = gstr2b.workbook(gstr2b.reconcile(session, ret))
    name = f"GSTR-2B reconciliation {ret.gstin} {ret.period}.xlsx"
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@api.delete("/gstr2b/{return_id}", status_code=204)
def delete_gstr2b(return_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    ret = session.get(Gstr2bReturn, return_id) or _404("GSTR-2B")
    log.info("GSTR-2B %s %s deleted by %s", ret.gstin, ret.period, user.name)
    session.delete(ret)
    session.commit()


@api.get("/excel/workbooks")
def excel_workbooks():
    """The workbooks there are: one per financial year (newest first), plus the single workbook from
    before the split, if any (year null)."""
    return [{"year": w["year"], "file": w["file"], "size_bytes": w["path"].stat().st_size,
             "download_url": f"/api/v1/excel?year={w['year']}" if w["year"] else "/api/v1/excel?year=all"}
            for w in list_workbooks()]


@api.get("/excel")
def download_excel(year: str | None = Query(None, description="Financial year like 2026-27; "
                                                                  "'all' = the single workbook from before the split"),
                   session: Session = Depends(get_session)):
    """The workbook of approved invoices: this financial year's (or the newest) unless `year` says which."""
    error = try_export(session)   # include anything approved a moment ago
    try:
        path = Path(settings.excel_path) if year == "all" else find_workbook(year)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if path is None or not path.exists():
        raise HTTPException(404, error or (f"No workbook for {year}" if year else "No approved invoices yet"))
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


# ---------- payables: supplier terms, payments, ageing (D-110)

UDYAM_RE = r"^UDYAM-[A-Z]{2}-\d{2}-\d{7}$"


class SupplierTerms(BaseModel):
    payment_terms_days: int | None = Field(None, ge=0, le=365, description="Days after the invoice date; "
                                                                           "null = PAYMENT_DAYS")
    msme_category: Literal["micro", "small", "medium"] | None = None
    udyam_number: str | None = Field(None, pattern=UDYAM_RE, description="e.g. UDYAM-TS-02-0012345")


@api.put("/suppliers/{gstin}/terms")
def set_supplier_terms(gstin: str, body: SupplierTerms, user: User = Depends(current_user),
                       session: Session = Depends(get_session)):
    """Payment terms and MSME status. The due dates of the supplier's unpaid invoices follow."""
    gstin = gstin.upper()
    sup = session.scalar(select(Supplier).where(Supplier.gstin == gstin).with_for_update())
    if sup is None:
        history = supplier_master.profile(session, gstin)
        if history is None:
            _404("Supplier")
        sup = Supplier(gstin=gstin, name=history.names[0] or gstin, pan=gstin[2:12])
        session.add(sup)
        session.flush()
    before = {"payment_terms_days": sup.payment_terms_days, "msme_category": sup.msme_category,
              "udyam_number": sup.udyam_number}
    changed = payables.set_terms(session, sup, days=body.payment_terms_days, msme_category=body.msme_category,
                                 udyam_number=body.udyam_number, actor=user.name)
    days, why = payables.terms_days(sup)
    session.add(SupplierEvent(supplier_id=sup.id, kind="terms", actor=user.name,
                              message=f"Payment terms set by {user.name}: {days} days ({why})",
                              data={"before": before, "after": body.model_dump()}))
    session.commit()
    log.info("Supplier %s terms set by %s: %s days (%s); %s invoice due date(s) changed", gstin, user.name,
             days, why, changed)
    return {"gstin": sup.gstin, "name": sup.name, **supplier_master.serialize_master(sup),
            "due_dates_changed": changed}


class Payment(BaseModel):
    paid_on: date
    reference: str = Field("", max_length=100, description="UTR, cheque or transaction number")


@api.post("/invoices/{invoice_id}/payment")
def mark_paid(invoice_id: int, body: Payment, user: User = Depends(current_user),
              session: Session = Depends(get_session)):
    """Record that an approved invoice was paid (the payment itself is made in the bank)."""
    inv = _locked_invoice(session, invoice_id)
    if inv.status not in payables.OWED:
        raise HTTPException(409, f"Only approved invoices can be paid (this one is {inv.status})")
    if inv.paid_on is not None:
        raise HTTPException(409, f"Already marked paid on {inv.paid_on}")
    if body.paid_on > date.today():
        raise HTTPException(422, "The payment date is in the future")
    payables.mark_paid(session, inv, body.paid_on, body.reference, user.name)
    session.commit()
    log.info("Invoice %s marked paid on %s by %s", inv.id, body.paid_on, user.name)
    return serialize(inv)


@api.delete("/invoices/{invoice_id}/payment")
def unmark_paid(invoice_id: int, body: Review = Review(), user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """Undo a payment marked by mistake."""
    inv = _locked_invoice(session, invoice_id)
    if inv.paid_on is None:
        raise HTTPException(409, "This invoice is not marked paid")
    payables.unmark_paid(session, inv, user.name, body.note)
    session.commit()
    return serialize(inv)


@api.get("/payables/ageing")
def payables_ageing(as_of: date | None = None, session: Session = Depends(get_session)):
    """AP ageing: unpaid approved invoices per supplier, by days past the due date, net of TDS. MSME
    invoices past their due date are listed separately (interest, and the 43B(h) deduction)."""
    return payables.ageing(session, as_of)


@api.get("/payables/due")
def payables_due(days: int = Query(7, ge=0, le=365), session: Session = Depends(get_session)):
    """Unpaid invoices due within `days`, and every overdue one: the payment run's list."""
    return payables.due_within(session, days)


# ---------- branches, purchase orders, goods receipts (D-111, D-112)

class BranchIn(BaseModel):
    code: str = Field(min_length=1, max_length=20, description="Short code, e.g. HYD")
    name: str = Field(min_length=1, max_length=100)
    city: str | None = Field(None, max_length=60)
    state_code: str | None = Field(None, pattern=r"^\d{2}$", description="GST state code, e.g. 36")
    gstin: str | None = Field(None, description="The GST registration used at this branch")
    address: str | None = None


class BranchPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    city: str | None = Field(None, max_length=60)
    state_code: str | None = Field(None, pattern=r"^\d{2}$")
    gstin: str | None = None
    address: str | None = None
    active: bool | None = None


def serialize_branch(b: Branch) -> dict:
    return {"id": b.id, "code": b.code, "name": b.name, "city": b.city, "state_code": b.state_code,
            "gstin": b.gstin, "address": b.address, "active": b.active}


def _check_gstin(gstin: str | None) -> str | None:
    gstin = (gstin or "").strip().upper() or None
    if gstin and not gstin_valid(gstin):
        raise HTTPException(422, f"{gstin} is not a valid GSTIN")
    return gstin


def _branch(session: Session, branch_id: int | None) -> int | None:
    if branch_id is not None and session.get(Branch, branch_id) is None:
        raise HTTPException(422, f"No branch {branch_id}")
    return branch_id


@api.post("/branches", status_code=201)
def create_branch(body: BranchIn, session: Session = Depends(get_session)):
    gstin = _check_gstin(body.gstin)
    if gstin and body.state_code and gstin[:2] != body.state_code:
        raise HTTPException(422, f"GSTIN {gstin} is for state {gstin[:2]}, not {body.state_code}")
    branch = Branch(code=body.code.strip().upper(), name=body.name.strip(), city=body.city,
                    state_code=body.state_code or (gstin[:2] if gstin else None), gstin=gstin, address=body.address)
    session.add(branch)
    try:
        session.commit()
    except IntegrityError:
        raise HTTPException(409, f"A branch with code {branch.code} already exists") from None
    return serialize_branch(branch)


@api.get("/branches")
def list_branches(session: Session = Depends(get_session)):
    return [serialize_branch(b) for b in session.scalars(select(Branch).order_by(Branch.code))]


@api.patch("/branches/{branch_id}")
def update_branch(branch_id: int, body: BranchPatch, session: Session = Depends(get_session)):
    branch = session.get(Branch, branch_id) or _404("Branch")
    changes = body.model_dump(exclude_unset=True)
    if "gstin" in changes:
        changes["gstin"] = _check_gstin(changes["gstin"])
    for k, v in changes.items():
        setattr(branch, k, v)
    session.commit()
    return serialize_branch(branch)


class UserBranch(BaseModel):
    branch_id: int | None


@api.put("/users/{user_id}/branch")
def set_user_branch(user_id: int, body: UserBranch, session: Session = Depends(get_session)):
    """Where this person works: their GRNs are for this branch unless they say otherwise."""
    target = session.get(User, user_id) or _404("User")
    target.branch_id = _branch(session, body.branch_id)
    session.commit()
    return serialize_user(target)


class PoLineIn(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    hsn_sac: str | None = Field(None, pattern=r"^\d{4,8}$")
    quantity: float = Field(gt=0)
    unit: str | None = Field(None, max_length=20)
    unit_price: float = Field(ge=0, description="Before tax")
    gst_rate: float | None = Field(None, ge=0, le=28)
    branch_id: int | None = Field(None, description="Delivered to; empty = the PO's branch")


class PoIn(BaseModel):
    po_number: str = Field(min_length=1, max_length=50)
    supplier_name: str = Field(min_length=1, max_length=255)
    supplier_gstin: str | None = None
    po_date: date
    kind: Literal["goods", "services"] = "goods"
    branch_id: int | None = None
    notes: str | None = None
    lines: list[PoLineIn] = Field(min_length=1)


def _procurement_error(e: procurement.ProcurementError):
    return HTTPException(e.status, str(e))


@api.post("/purchase-orders", status_code=201)
def create_purchase_order(body: PoIn, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Enter a PO. Invoices already in review that quote its number are checked again."""
    _branch(session, body.branch_id)
    for ln in body.lines:
        _branch(session, ln.branch_id)
    try:
        po = procurement.create_po(session, po_number=body.po_number, supplier_name=body.supplier_name,
                                   supplier_gstin=_check_gstin(body.supplier_gstin), po_date=body.po_date,
                                   kind=body.kind, branch_id=body.branch_id, notes=body.notes,
                                   lines=[ln.model_dump() for ln in body.lines], actor=user.name)
    except procurement.ProcurementError as e:
        raise _procurement_error(e) from e
    log.info("PO %s entered by %s: %s, %d line(s)", po.po_number, user.name, po.supplier_name, len(body.lines))
    rechecked = procurement.after_po_change(session, po, user.name)
    return {**procurement.serialize_po(session, po, detail=True), "rechecked": rechecked}


@api.get("/purchase-orders")
def list_purchase_orders(status: str | None = None, supplier_gstin: str | None = None,
                         limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                         session: Session = Depends(get_session)):
    q = select(PurchaseOrder).order_by(PurchaseOrder.po_date.desc(), PurchaseOrder.id.desc()).limit(limit).offset(offset)
    if status:
        q = q.where(PurchaseOrder.status == status.upper())
    if supplier_gstin:
        q = q.where(PurchaseOrder.supplier_gstin == supplier_gstin.upper())
    return [procurement.serialize_po(session, po) for po in session.scalars(q)]


@api.get("/purchase-orders/{po_id}")
def get_purchase_order(po_id: int, session: Session = Depends(get_session)):
    """The PO with, per line, how much was ordered, accepted, rejected, billed, and is still to come."""
    po = session.get(PurchaseOrder, po_id) or _404("Purchase order")
    return procurement.serialize_po(session, po, detail=True)


@api.post("/purchase-orders/{po_id}/close")
def close_purchase_order(po_id: int, body: Review = Review(), user: User = Depends(current_user),
                         session: Session = Depends(get_session)):
    """Nothing more will be delivered (short close)."""
    return _end_po(session, po_id, "close", body, user)


@api.post("/purchase-orders/{po_id}/cancel")
def cancel_purchase_order(po_id: int, body: Review = Review(), user: User = Depends(current_user),
                          session: Session = Depends(get_session)):
    """Only if nothing was received."""
    return _end_po(session, po_id, "cancel", body, user)


def _end_po(session: Session, po_id: int, action: str, body: Review, user: User) -> dict:
    po = session.scalar(select(PurchaseOrder).where(PurchaseOrder.id == po_id).with_for_update()) or _404("Purchase order")
    try:
        procurement.end_po(session, po, "CLOSED" if action == "close" else "CANCELLED", user.name, body.note)
    except procurement.ProcurementError as e:
        raise _procurement_error(e) from e
    session.commit()
    log.info("PO %s %s by %s", po.po_number, po.status, user.name)
    return procurement.serialize_po(session, po, detail=True)


class GrnLineIn(BaseModel):
    line_no: int = Field(ge=1, description="The PO line")
    received_qty: float = Field(gt=0)
    accepted_qty: float | None = Field(None, ge=0, description="Empty = received less rejected")
    rejected_qty: float = Field(0, ge=0)
    reason: str | None = Field(None, description="Why rejected: damaged, wrong item, short shelf life...")


class GrnIn(BaseModel):
    branch_id: int | None = Field(None, description="Empty = the user's branch, else the PO's")
    received_on: date
    challan_number: str | None = Field(None, max_length=50)
    notes: str | None = None
    lines: list[GrnLineIn] = Field(min_length=1)
    confirm: bool = Field(False, description="Confirm at once instead of saving a draft")


@api.post("/purchase-orders/{po_id}/grns", status_code=201)
def create_grn(po_id: int, body: GrnIn, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """Record what a branch received against a PO (for services: what was done). It counts for matching
    once confirmed."""
    po = session.get(PurchaseOrder, po_id) or _404("Purchase order")
    try:
        grn = procurement.create_grn(session, po, branch_id=_branch(session, body.branch_id) or user.branch_id,
                                     received_on=body.received_on, challan_number=body.challan_number,
                                     notes=body.notes, lines=[ln.model_dump() for ln in body.lines], actor=user.name,
                                     actor_id=user.id)
        if body.confirm:
            procurement.confirm_grn(session, grn, user.name)
    except procurement.ProcurementError as e:
        raise _procurement_error(e) from e
    log.info("GRN %s for PO %s by %s (%s)", grn.id, po.po_number, user.name, grn.status)
    rechecked = procurement.after_po_change(session, po, user.name) if body.confirm else (session.commit() or [])
    return {**procurement.serialize_grn(session, grn), "po_status": po.status, "rechecked": rechecked}


@api.get("/grns/{grn_id}")
def get_grn(grn_id: int, session: Session = Depends(get_session)):
    return procurement.serialize_grn(session, session.get(GoodsReceipt, grn_id) or _404("GRN"))


@api.post("/grns/{grn_id}/confirm")
def confirm_grn(grn_id: int, body: Review = Review(), user: User = Depends(current_user),
                session: Session = Depends(get_session)):
    """The receipt counts from now on, and invoices waiting for it are checked again."""
    return _grn_action(session, grn_id, "confirm", body, user)


@api.post("/grns/{grn_id}/cancel")
def cancel_grn(grn_id: int, body: Review = Review(), user: User = Depends(current_user),
               session: Session = Depends(get_session)):
    """A draft only."""
    return _grn_action(session, grn_id, "cancel", body, user)


def _grn_action(session: Session, grn_id: int, action: str, body: Review, user: User) -> dict:
    grn = session.scalar(select(GoodsReceipt).where(GoodsReceipt.id == grn_id).with_for_update()) or _404("GRN")
    try:
        if action == "confirm":
            po = procurement.confirm_grn(session, grn, user.name)
        else:
            procurement.cancel_grn(session, grn, user.name, body.note)
            po = None
    except procurement.ProcurementError as e:
        raise _procurement_error(e) from e
    log.info("GRN %s %s by %s", grn.id, grn.status, user.name)
    rechecked = procurement.after_po_change(session, po, user.name) if po else (session.commit() or [])
    return {**procurement.serialize_grn(session, grn), "po_status": po.status if po else None, "rechecked": rechecked}


# ---------- follow-ups: WhatsApp reminders and calls (what was sent, to whom, and what came of it)

@api.get("/followups")
def list_followups(state: Literal["active", "done"] | None = None, user_id: int | None = None,
                   limit: int = Query(50, ge=1, le=200), session: Session = Depends(get_session)):
    """Reminder ladders, newest first. Shows `enabled` / `dry_run` so you can tell nothing is being sent."""
    query = select(FollowUp).order_by(FollowUp.id.desc()).limit(limit)
    if state:
        query = query.where(FollowUp.state == state)
    if user_id:
        query = query.where(FollowUp.target_user_id == user_id)
    rows = list(session.scalars(query))
    invoices = {i.id: i for i in session.scalars(select(Invoice).where(Invoice.id.in_({f.invoice_id for f in rows})))}
    return {"enabled": settings.followup_enabled, "dry_run": settings.followup_dry_run,
            "items": [followup.serialize(f, invoices.get(f.invoice_id)) for f in rows]}


@api.get("/followups/events")
def followup_events(user_id: int | None = None, limit: int = Query(100, ge=1, le=500),
                    session: Session = Depends(get_session)):
    """What was sent (or would have been, in a dry run), skipped, failed or answered, newest first."""
    query = select(FollowUpEvent).order_by(FollowUpEvent.id.desc()).limit(limit)
    if user_id:
        query = query.where(FollowUpEvent.user_id == user_id)
    return [followup.serialize_event(e) for e in session.scalars(query)]


@api.post("/followups/{follow_up_id}/stop")
def stop_followup(follow_up_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    """End a reminder ladder by hand."""
    fu = session.scalar(select(FollowUp).where(FollowUp.id == follow_up_id).with_for_update()) or _404("Follow-up")
    if fu.state != "active":
        raise HTTPException(409, "This follow-up is already finished")
    fu.state, fu.done_reason, fu.awaiting_reply_since = "done", "stopped", None
    session.add(FollowUpEvent(follow_up_id=fu.id, user_id=fu.target_user_id, channel="whatsapp", direction="in",
                              status="reply", detail=f"stopped by {user.name}"[:500]))
    session.commit()
    return followup.serialize(fu, session.get(Invoice, fu.invoice_id))


@api.get("/followups/action", response_class=HTMLResponse, include_in_schema=False)
def followup_action_page(t: str = Query("")):
    """Opened from a reminder email's button: says what the choice does, with a Confirm button (a POST).
    Opening the link changes nothing, so mail scanners that open links can't make the choice."""
    try:
        text = followup.describe_action(t)
    except followup.BadLink as e:
        return HTMLResponse(followup.action_page("This link can't be used", str(e)), status_code=e.status)
    return HTMLResponse(followup.action_page(followup.EMAIL_ACTIONS[followup.read_action_token(t)[2]], text, t))


@api.post("/followups/action", response_class=HTMLResponse, include_in_schema=False)
def followup_action(t: str = Form(""), session: Session = Depends(get_session)):
    try:
        text = followup.apply_action(session, t)
    except followup.BadLink as e:
        return HTMLResponse(followup.action_page("This link can't be used", str(e)), status_code=e.status)
    return HTMLResponse(followup.action_page("Done", text))


# ---------- WhatsApp messages received (app/whatsapp.py): what came in and what became of each file

@api.get("/whatsapp/messages")
def list_whatsapp_messages(user_id: int | None = None, limit: int = Query(50, ge=1, le=200),
                           session: Session = Depends(get_session)):
    """Incoming WhatsApp messages, newest first, each attachment with its status: ingested (with the
    invoice), duplicate, rejected (and why), failed (tried again at next_try_at), gave_up or pending.
    Senders are masked; message text is never stored."""
    query = select(WhatsAppMessage).order_by(WhatsAppMessage.id.desc()).limit(limit)
    if user_id:
        query = query.where(WhatsAppMessage.user_id == user_id)
    messages = list(session.scalars(query))
    media: dict[int, list] = {}
    for m in session.scalars(select(WhatsAppMedia).where(WhatsAppMedia.message_id.in_([x.id for x in messages]))
                             .order_by(WhatsAppMedia.position)):
        media.setdefault(m.message_id, []).append(m)
    return [whatsapp.serialize_message(m, media.get(m.id, [])) for m in messages]


@api.post("/whatsapp/media/{media_id}/retry")
def retry_whatsapp_media(media_id: int, session: Session = Depends(get_session)):
    """Try one attachment again at the next retry check (within a minute), e.g. after raising MAX_UPLOAD_MB."""
    media = whatsapp.retry_now(session, media_id) or _404("WhatsApp attachment")
    if media.status in ("ingested", "duplicate"):
        raise HTTPException(409, f"This attachment was already received (invoice {media.invoice_id})")
    return {"id": media.id, "status": media.status, "next_try_at": media.next_try_at, "attempts": media.attempts}


@api.get("/voice/calls")
def list_voice_calls(limit: int = Query(20, ge=1, le=100), session: Session = Depends(get_session)):
    """Follow-up phone calls with their outcome and transcript."""
    return [followup.serialize_call(c) for c in session.scalars(select(VoiceCall).order_by(VoiceCall.id.desc()).limit(limit))]


CALL_ENDED = {"completed", "no_answer", "busy", "failed", "canceled", "dry_run"}
LIVE_MAX_SECONDS = 15 * 60


def _call_snapshot(call_id: int) -> tuple[str, list, str | None] | None:
    with SessionLocal() as session:
        c = session.get(VoiceCall, call_id)
        return None if c is None else (c.status, list(c.transcript or []), c.outcome)


def _newest_open_call() -> int | None:
    with SessionLocal() as session:
        return session.scalar(select(VoiceCall.id).where(VoiceCall.status.notin_(CALL_ENDED))
                              .order_by(VoiceCall.id.desc()).limit(1))


@api.get("/voice/calls/{call_ref}/live")
async def live_transcript(call_ref: str):
    """The conversation as it happens, one line per turn ("AI: ...", "Caller: ..."), until the call ends.
    `latest` follows the call in progress, or waits (up to 15 minutes) for the next one to start.
    Watch it with: curl -N http://localhost:8000/api/v1/voice/calls/latest/live"""
    if call_ref != "latest" and not call_ref.isdigit():
        raise HTTPException(422, "Use a call id or 'latest'")

    async def lines():
        deadline = time.monotonic() + LIVE_MAX_SECONDS
        call_id = int(call_ref) if call_ref.isdigit() else None
        if call_id is None:
            yield "Waiting for the next call...\n"
            while (call_id := await run_in_threadpool(_newest_open_call)) is None:
                if time.monotonic() > deadline:
                    yield "No call started.\n"
                    return
                await asyncio.sleep(1)
            yield f"Call {call_id}:\n"
        shown = 0
        while time.monotonic() < deadline:
            snapshot = await run_in_threadpool(_call_snapshot, call_id)
            if snapshot is None:
                yield f"No call {call_id}.\n"
                return
            status, transcript, outcome = snapshot
            for line in transcript[shown:]:
                yield f"{'AI' if line.get('role') == 'assistant' else 'Caller'}: {line.get('text', '')}\n"
            shown = len(transcript)
            if status in CALL_ENDED:
                yield f"-- call {status}; outcome: {outcome or 'none'}\n"
                return
            await asyncio.sleep(1)

    return StreamingResponse(lines(), media_type="text/plain; charset=utf-8")


@api.get("/voice/calls/{call_id}")
def get_voice_call(call_id: int, session: Session = Depends(get_session)):
    return followup.serialize_call(session.get(VoiceCall, call_id) or _404("Call"))


app.include_router(api)
app.include_router(whatsapp.router)
app.include_router(voice.router)


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
