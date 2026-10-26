"""Single entry point used by every channel (upload, Gmail, later WhatsApp)."""
import hashlib
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Invoice, Status, audit

ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}


def is_supported(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXT


def ingest_file(session: Session, content: bytes, filename: str, source: str,
                source_ref: str | None = None) -> tuple[Invoice, bool]:
    """Store the file and create an invoice record. Returns (invoice, created).
    The same file arriving twice (any channel) returns the existing record."""
    file_hash = hashlib.sha256(content).hexdigest()
    existing = session.scalar(select(Invoice).where(Invoice.file_hash == file_hash))
    if existing:
        return existing, False

    storage = Path(settings.storage_dir)
    storage.mkdir(parents=True, exist_ok=True)
    path = storage / f"{file_hash}{Path(filename).suffix.lower()}"
    path.write_bytes(content)

    inv = Invoice(source=source, source_ref=source_ref, original_filename=filename,
                  file_path=str(path), file_hash=file_hash, status=Status.RECEIVED)
    session.add(inv)
    try:
        session.flush()  # get inv.id
    except IntegrityError:
        # The same file arrived on another channel/request at the same moment
        session.rollback()
        return session.scalar(select(Invoice).where(Invoice.file_hash == file_hash)), False
    audit(session, inv.id, "ingest", f"Received via {source}", {"filename": filename})
    session.commit()
    return inv, True
