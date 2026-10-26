"""Single entry point for every channel (upload, Gmail, later WhatsApp).
Channels only call ingest_document(); everything after that is shared."""
import hashlib
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.documents import DOCUMENT_EXT, MIME_EXT, kind  # noqa: F401  (MIME_EXT re-exported)
from app.models import Invoice, Status, audit

log = logging.getLogger("ingest")
ALLOWED_EXT = DOCUMENT_EXT


def is_supported(filename: str) -> bool:
    """A document we can read (PDF, image, Excel/CSV/Word). Containers (.zip, .eml) are
    unpacked by the caller with app.documents.expand()."""
    return Path(filename).suffix.lower() in ALLOWED_EXT


def is_accepted(filename: str) -> bool:
    """Accepted for upload: a document or a container."""
    return kind(filename) is not None


def ingest_document(session: Session, content: bytes, filename: str, source: str, user_id: int, *,
                    source_ref: str | None = None, sender: str | None = None,
                    email_subject: str | None = None) -> tuple[Invoice, bool]:
    """Store the file and queue it for processing. Returns (invoice, created).
    The same file arriving again (any channel, any user) returns the existing record."""
    file_hash = hashlib.sha256(content).hexdigest()
    existing = session.scalar(select(Invoice).where(Invoice.file_hash == file_hash))
    if existing:
        log.info("%s '%s' (user %s) is the same file as invoice %s; not queued again",
                 source, filename, user_id, existing.id)
        return existing, False

    storage = Path(settings.storage_dir)
    storage.mkdir(parents=True, exist_ok=True)
    path = storage / f"{file_hash}{Path(filename).suffix.lower()}"
    path.write_bytes(content)

    inv = Invoice(user_id=user_id, source=source, source_ref=source_ref, sender=(sender or "")[:255] or None,
                  email_subject=(email_subject or "")[:500] or None, original_filename=filename[:255],
                  file_path=str(path), file_hash=file_hash, status=Status.RECEIVED)
    session.add(inv)
    try:
        session.flush()  # get inv.id
    except IntegrityError:
        # The same file arrived on another channel/request at the same moment
        session.rollback()
        log.info("%s '%s' arrived twice at the same moment; keeping one", source, filename)
        return session.scalar(select(Invoice).where(Invoice.file_hash == file_hash)), False
    audit(session, inv.id, "ingest", f"Received via {source}",
          {"filename": filename, "sender": sender, "subject": email_subject})
    session.commit()
    log.info("Invoice %s queued: %s '%s' (%d bytes, user %s%s)", inv.id, source, filename, len(content),
             user_id, f", from {sender}" if sender else "")
    return inv, True
