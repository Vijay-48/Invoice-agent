"""Run: uvicorn app.api:app --reload"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import Base, engine, get_session
from app.ingest.common import ingest_file, is_supported
from app.ledger import safe_name
from app.models import Invoice, AuditLog, Status, audit
from app.pipeline.post import POSTABLE, post_invoice

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)   # use Alembic migrations once the schema settles
    yield


app = FastAPI(title="Invoice AP Automation", lifespan=lifespan)


def serialize(inv: Invoice) -> dict:
    return {
        "id": inv.id, "status": inv.status, "source": inv.source,
        "filename": inv.original_filename, "supplier_gstin": inv.supplier_gstin,
        "invoice_no": inv.invoice_no, "invoice_date": inv.invoice_date,
        "grand_total": float(inv.grand_total) if inv.grand_total is not None else None,
        "checks": inv.checks, "error": inv.error, "created_at": inv.created_at,
    }


@app.post("/invoices/upload", status_code=201)
async def upload(file: UploadFile = File(...), session: Session = Depends(get_session)):
    if not is_supported(file.filename or ""):
        raise HTTPException(400, "Only PDF, PNG, JPG and WEBP files are supported")
    content = await file.read()
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB")
    inv, created = ingest_file(session, content, file.filename, "upload")
    return {"id": inv.id, "status": inv.status, "duplicate_file": not created}


@app.get("/invoices")
def list_invoices(status: str | None = None, session: Session = Depends(get_session)):
    q = select(Invoice).order_by(Invoice.created_at.desc()).limit(100)
    if status:
        q = q.where(Invoice.status == status)
    return [serialize(i) for i in session.scalars(q)]


@app.get("/invoices/{invoice_id}")
def get_invoice(invoice_id: int, session: Session = Depends(get_session)):
    inv = session.get(Invoice, invoice_id) or _404()
    logs = session.scalars(select(AuditLog).where(AuditLog.invoice_id == invoice_id)
                           .order_by(AuditLog.created_at)).all()
    return {**serialize(inv), "extracted": inv.extracted,
            "audit": [{"stage": l.stage, "actor": l.actor, "message": l.message,
                       "at": l.created_at} for l in logs]}


class Review(BaseModel):
    reviewer: str
    note: str = ""


@app.post("/invoices/{invoice_id}/approve")
def approve(invoice_id: int, body: Review, session: Session = Depends(get_session)):
    """Approve and post to the ledger files under the reviewer's name."""
    _transition(session, invoice_id, {Status.PENDING_APPROVAL}, Status.APPROVED, body)
    return _post(session, session.get(Invoice, invoice_id), body.reviewer, f"approved by {body.reviewer}")


@app.post("/invoices/{invoice_id}/post")
def post(invoice_id: int, body: Review, session: Session = Depends(get_session)):
    """Retry posting an AUTO_POST / APPROVED invoice whose ledger write failed."""
    inv = session.get(Invoice, invoice_id) or _404()
    if inv.status not in POSTABLE:
        raise HTTPException(409, f"Cannot post an invoice in {inv.status}")
    return _post(session, inv, body.reviewer, f"posted by {body.reviewer}")


@app.post("/invoices/{invoice_id}/reject")
def reject(invoice_id: int, body: Review, session: Session = Depends(get_session)):
    return _transition(session, invoice_id, {Status.PENDING_APPROVAL, Status.EXCEPTION},
                       Status.REJECTED, body)


@app.post("/invoices/{invoice_id}/reprocess")
def reprocess(invoice_id: int, body: Review, session: Session = Depends(get_session)):
    """After fixing master data (e.g. adding a supplier or PO), run the pipeline again."""
    inv = session.get(Invoice, invoice_id) or _404()
    inv.attempts = 0
    return _transition(session, invoice_id, {Status.EXCEPTION, Status.FAILED}, Status.RECEIVED, body)


def _transition(session, invoice_id, allowed, new_status, body: Review):
    inv = session.get(Invoice, invoice_id) or _404()
    if inv.status not in allowed:
        raise HTTPException(409, f"Cannot move from {inv.status} to {new_status}")
    old = inv.status
    inv.status = new_status
    audit(session, inv.id, "review", f"{old} -> {new_status}. {body.note}", actor=body.reviewer)
    session.commit()
    return serialize(inv)


def _post(session, inv: Invoice, actor: str, how: str):
    try:
        post_invoice(session, inv, actor, how)
    except Exception as e:
        # Invoice keeps its AUTO_POST/APPROVED status; retry with POST /invoices/{id}/post
        session.rollback()
        inv = session.get(Invoice, inv.id)
        inv.error = f"Posting failed: {e}"[:2000]
        audit(session, inv.id, "post", inv.error, actor=actor)
        session.commit()
    return serialize(inv)


LEDGER_BOOKS = {"suppliers", "users"}


@app.get("/ledgers")
def list_ledgers():
    """Available ledger files: the day book, plus one per supplier and per user."""
    root = Path(settings.ledger_dir)
    return {"daybook": (root / "daybook.md").exists(),
            **{book: sorted(p.stem for p in (root / book).glob("*.md")) for book in sorted(LEDGER_BOOKS)}}


@app.get("/ledgers/daybook", response_class=PlainTextResponse)
def daybook():
    return _read_ledger(Path(settings.ledger_dir) / "daybook.md")


@app.get("/ledgers/{book}/{name}", response_class=PlainTextResponse)
def ledger(book: str, name: str):
    """e.g. /ledgers/users/system, /ledgers/users/priya, /ledgers/suppliers/36aaacs1234a1z3"""
    if book not in LEDGER_BOOKS:
        raise HTTPException(404, "Ledger book must be 'suppliers' or 'users'")
    return _read_ledger(Path(settings.ledger_dir) / book / f"{safe_name(name)}.md")


def _read_ledger(path: Path) -> str:
    if not path.exists():
        raise HTTPException(404, "No postings in this ledger yet")
    return path.read_text(encoding="utf-8")


def _404():
    raise HTTPException(404, "Invoice not found")
