"""Gmail through Composio: each user connects their own inbox.

    connect_link(user)      Composio Connect Link the user opens to authorise Gmail
    activate(session, user) after authorising: store the connected account and turn on
                            the GMAIL_NEW_GMAIL_MESSAGE trigger for that inbox
    handle_message(...)     one email -> download supported attachments -> ingest_document()
    sync_user(...)          catch up on emails since the last sync (missed while the listener was off)

The listener process (app/gmail_listener.py) feeds trigger events into handle_message().
All Composio calls go through _composio(), execute() and download(), which tests replace.
"""
import logging
import re
import threading
import weakref
from contextlib import contextmanager
from collections import Counter
from email.utils import parseaddr
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.logs import timed
from app.documents import (IMAGE_EXT, LOGO_MAX_BYTES, body_to_pdf, expand, looks_like_invoice_text,
                           with_extension)
from app.ingest import ingest_document
from app.models import AuditLog, EmailMessage, GmailLabel, Invoice, Status, User, audit

log = logging.getLogger("gmail")
TOOLKIT = "gmail"
TRIGGER = "GMAIL_NEW_GMAIL_MESSAGE"
CONNECT_POLL_SECONDS = 3
RECEIVED_AGAIN = "received_again"   # audit stage for a file that arrived in another email


class GmailNotConnected(Exception):
    pass


_client_lock = threading.Lock()


def _composio():
    with _client_lock:   # the listener and sync threads start at the same moment
        return _make_composio()


@lru_cache
def _make_composio():
    from composio import Composio   # imported lazily so the API starts without a key
    if not settings.composio_api_key:
        raise RuntimeError("COMPOSIO_API_KEY is not set in .env")
    log.info("Composio client ready (gmail toolkit %s)", settings.composio_gmail_version)
    return Composio(api_key=settings.composio_api_key, allow_tracking=False,
                    toolkit_versions={TOOLKIT: settings.composio_gmail_version})


class NetworkError(Exception):
    """The internet or Composio/Gmail could not be reached (after retries)."""


def is_network_error(e: BaseException) -> bool:
    import composio_client   # lazy, like the Composio client itself
    return isinstance(e, (NetworkError, composio_client.APIConnectionError, composio_client.RateLimitError,
                          composio_client.InternalServerError, httpx.TransportError))


def _with_retries(what: str, fn):
    """Retry a read-only call on network trouble (connection reset, DNS failure, timeouts,
    Composio 5xx/429): waits 2, 5 and 15 seconds, then raises NetworkError."""
    for attempt, wait in enumerate([2, 5, 15, None], start=1):
        try:
            return fn()
        except Exception as e:
            if not is_network_error(e):
                raise
            if wait is None:
                raise NetworkError(f"{what}: network error after {attempt} attempts ({type(e).__name__}: {e})") from e
            log.warning("%s: network error (%s), retrying in %ss", what, type(e).__name__, wait)
            time.sleep(wait)


def execute(user: User, slug: str, arguments: dict) -> dict:
    """Run a Composio Gmail tool for this user and return its `data`. Only read-only tools
    are used, so network errors are retried."""
    log.debug("Composio %s for user %s args=%s", slug, user.id, arguments)
    with timed(log, f"Composio {slug} for user {user.id}"):
        res = _with_retries(f"Composio {slug}", lambda: _composio().tools.execute(
            slug, arguments, user_id=user.composio_user_id, connected_account_id=user.gmail_account_id))
    if not res.get("successful"):
        log.error("Composio %s failed for user %s: %s", slug, user.id, res.get("error"))
        raise RuntimeError(f"{slug} failed: {res.get('error')}")
    return res.get("data") or {}


def send_email(user: User, recipients: list[str], subject: str, body_html: str) -> None:
    """Send an email from this user's Gmail. Not retried: a send that timed out may have
    gone out, and a second try would send it twice."""
    args = {"recipient_email": recipients[0], "extra_recipients": recipients[1:], "subject": subject,
            "body": body_html, "is_html": True}
    log.info("Sending '%s' from user %s to %s", subject, user.id, ", ".join(recipients))
    with timed(log, f"Composio GMAIL_SEND_EMAIL for user {user.id}"):
        res = _composio().tools.execute("GMAIL_SEND_EMAIL", args, user_id=user.composio_user_id,
                                        connected_account_id=user.gmail_account_id)
    if not res.get("successful"):
        raise RuntimeError(str(res.get("error") or "GMAIL_SEND_EMAIL failed")[:500])


class TooLarge(ValueError):
    """An attachment over MAX_UPLOAD_MB (Gmail allows 25 MB)."""


def download(url: str) -> bytes:
    return _with_retries("Attachment download", lambda: _download(url))


def _download(url: str) -> bytes:
    limit = settings.max_upload_mb * 1024 * 1024
    with httpx.stream("GET", url, timeout=60, follow_redirects=True) as r:
        r.raise_for_status()
        chunks, size = [], 0
        for chunk in r.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise TooLarge(f"larger than {settings.max_upload_mb} MB (MAX_UPLOAD_MB)")
            chunks.append(chunk)
    log.debug("Downloaded attachment: %d bytes", size)   # never log the signed URL
    return b"".join(chunks)


# ---------- connecting an inbox

def callback_url(user_id: int) -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/v1/users/{user_id}/gmail/callback"


def connect_link(user: User) -> str:
    """URL the user opens to authorise Gmail (Composio-hosted OAuth).

    Activation then happens by itself: Composio sends the browser back to
    /gmail/callback, and a watcher thread also activates it if that page is never reached."""
    log.info("Creating Gmail connect link for user %s (%s)", user.id, user.composio_user_id)
    session = _composio().create(user_id=user.composio_user_id)
    request = session.authorize(TOOLKIT, callback_url=callback_url(user.id))
    threading.Thread(target=_activate_when_connected, args=(user.id, request.id),
                     name=f"gmail-connect-{user.id}", daemon=True).start()
    return request.redirect_url


def _activate_when_connected(user_id: int, account_id: str, timeout_minutes: int = 15) -> None:
    """Wait for the user to finish the Gmail consent screen, then activate."""
    from app.db import SessionLocal   # avoid an import cycle at module load
    deadline = time.monotonic() + timeout_minutes * 60
    log.info("Waiting up to %d min for user %s to allow Gmail access (%s)", timeout_minutes, user_id, account_id)
    while time.monotonic() < deadline:
        time.sleep(CONNECT_POLL_SECONDS)
        try:
            status = _composio().connected_accounts.get(account_id).status
        except Exception as e:
            if is_network_error(e):
                log.warning("Checking Gmail connection %s: network error, will retry", account_id)
            else:
                log.exception("Checking Gmail connection %s failed; will retry", account_id)
            continue
        if status == "ACTIVE":
            with SessionLocal() as session:
                user = session.get(User, user_id)
                if user is None:
                    return
                if user.gmail_account_id == account_id and user.gmail_trigger_id:
                    log.info("Gmail for user %s was already activated (callback)", user_id)
                    return
                try:
                    activate(session, user, if_needed=True)
                except Exception:
                    session.rollback()
                    log.exception("Automatic Gmail activation failed for user %s; "
                                  "POST /api/v1/users/%s/gmail/activate to retry", user_id, user_id)
            return
        if status in ("FAILED", "EXPIRED", "REVOKED", "INACTIVE"):
            log.warning("Gmail connection %s for user %s ended as %s", account_id, user_id, status)
            return
    log.info("User %s did not finish connecting Gmail within %d min", user_id, timeout_minutes)


def find_connected_account(user: User) -> str | None:
    accounts = _composio().connected_accounts.list(
        user_ids=[user.composio_user_id], toolkit_slugs=[TOOLKIT], statuses=["ACTIVE"],
        order_by="created_at", order_direction="desc")
    items = getattr(accounts, "items", None) or []
    log.info("User %s has %d active Gmail connection(s)%s", user.id, len(items),
             f"; using {items[0].id}" if items else "")
    return items[0].id if items else None


_activate_locks: dict[int, threading.Lock] = {}
_activate_locks_guard = threading.Lock()


def activate(session: Session, user: User, if_needed: bool = False) -> User:
    """Turn on new-email events for the user's connected Gmail. Safe to call again.
    One activation per user at a time; `if_needed` skips it when already active."""
    with _activate_locks_guard:
        lock = _activate_locks.setdefault(user.id, threading.Lock())
    with lock:
        session.refresh(user)
        if if_needed and user.gmail_trigger_id and user.gmail_account_id == find_connected_account(user):
            log.info("Gmail for user %s was already activated", user.id)
            return user
        return _activate(session, user)


def _activate(session: Session, user: User) -> User:
    account_id = find_connected_account(user)
    if not account_id:
        log.warning("Activate for user %s: Gmail not connected yet", user.id)
        raise GmailNotConnected("Gmail is not connected yet: open the connect link first")
    # Save the connection first, so sync works even if the trigger can't be created
    now = datetime.now(timezone.utc)
    if user.gmail_account_id != account_id or not user.gmail_connected_at:
        user.gmail_connected_at = now
        # First connection: only emails from now on (/gmail/sync?days=N for older ones).
        # Reconnecting (e.g. expired access): keep the sync point, so the outage is caught up
        user.gmail_last_sync_at = user.gmail_last_sync_at or now
    user.gmail_account_id = account_id
    session.commit()

    config = {"interval": settings.gmail_poll_minutes, "query": settings.gmail_query}
    log.info("Creating %s trigger for user %s on %s with %s", TRIGGER, user.id, account_id, config)
    # user_id is required when 2FA is enabled on the Composio project
    trigger = _composio().triggers.create(TRIGGER, user_id=user.composio_user_id,
                                          connected_account_id=account_id, trigger_config=config)
    user.gmail_trigger_id = trigger.trigger_id
    session.commit()
    from app import gmail_health
    gmail_health.record(session, user, gmail_health.OK, "Connected")
    _remove_other_triggers(account_id, keep=trigger.trigger_id)
    log.info("Gmail active for user %s (trigger %s)", user.id, trigger.trigger_id)
    return user


def _remove_other_triggers(account_id: str, keep: str) -> None:
    """One trigger per inbox: a trigger created with an older GMAIL_QUERY would
    deliver every email a second time."""
    try:
        active = _composio().triggers.list_active(connected_account_ids=[account_id], trigger_names=[TRIGGER])
        for t in getattr(active, "items", None) or []:
            if t.id != keep:
                _composio().triggers.delete(t.id)
                log.info("Removed old trigger %s (config %s) from %s", t.id, t.trigger_config, account_id)
    except Exception:
        log.exception("Could not clean up old triggers on %s; emails may arrive twice (duplicates are skipped)",
                      account_id)


# ---------- reading emails

def _first(d: dict, *keys, default=None):
    for k in keys:
        if d.get(k):
            return d[k]
    return default


def _walk_parts(part: dict):
    """Attachments inside a raw Gmail payload (nested multipart)."""
    body = part.get("body") or {}
    if part.get("filename") and body.get("attachmentId"):
        yield {"filename": part["filename"], "attachmentId": body["attachmentId"],
               "mimeType": part.get("mimeType")}
    for sub in part.get("parts") or []:
        yield from _walk_parts(sub)


def attachments_of(message: dict) -> list[dict]:
    """[{filename, attachment_id, mime_type}] from a trigger event or a fetched message."""
    raw = _first(message, "attachment_list", "attachmentList", default=[]) or []
    if not raw:
        raw = list(_walk_parts(message.get("payload") or {}))
    out = []
    for a in raw:
        att_id = _first(a, "attachmentId", "attachment_id", "id")
        name = _first(a, "filename", "fileName", "file_name", "name", default="")
        if att_id and name:
            out.append({"filename": name, "attachment_id": att_id,
                        "mime_type": _first(a, "mimeType", "mime_type", "mimetype")})
    return out


def _subject_matches(subject: str, body: str) -> bool:
    keywords = [k.strip().lower() for k in settings.gmail_subject_keywords.split(",") if k.strip()]
    text = f"{subject}\n{body}".lower()
    return not keywords or any(k in text for k in keywords)


def _email_row(session: Session, user: User, message_id: str) -> EmailMessage:
    """The email's log row, created if needed. Insert-or-nothing, so two handlers of the
    same email (event + sync, or a duplicate event) can't both insert it."""
    session.execute(pg_insert(EmailMessage).values(
        user_id=user.id, message_id=message_id, outcome="pending", attachments=[],
    ).on_conflict_do_nothing(index_elements=["user_id", "message_id"]))
    return session.scalar(select(EmailMessage).where(EmailMessage.user_id == user.id,
                                                     EmailMessage.message_id == message_id))


_message_locks: "weakref.WeakValueDictionary[tuple, threading.Lock]" = weakref.WeakValueDictionary()
_message_locks_guard = threading.Lock()


@contextmanager
def _one_at_a_time(user_id: int, message_id: str):
    """Handle each email in one thread at a time; a second handler waits, then finds it done."""
    with _message_locks_guard:
        lock = _message_locks.get((user_id, message_id))
        if lock is None:
            lock = threading.Lock()
            _message_locks[(user_id, message_id)] = lock
    with lock:
        yield


def handle_message(session: Session, user: User, message: dict) -> list[int]:
    """Process one email: each supported attachment (and the documents inside .zip/.eml
    attachments) becomes its own job; an invoice written in the body becomes a PDF.
    Everything is recorded in the email log (EmailMessage). Returns new invoice ids."""
    message_id = _first(message, "id", "message_id", "messageId")
    if not message_id:
        log.warning("Gmail event without a message id: %s", list(message))
        return []
    with _one_at_a_time(user.id, message_id):
        return _handle_message(session, user, message, message_id)


def _handle_message(session: Session, user: User, message: dict, message_id: str) -> list[int]:
    subject = _first(message, "subject", default="") or ""
    sender = _first(message, "sender", "from", default="") or ""
    body = _first(message, "message_text", "messageText", default="") or ""

    row = _email_row(session, user, message_id)
    row.thread_id = _first(message, "thread_id", "threadId") or row.thread_id
    row.sender, row.subject = sender[:255] or None, subject[:500] or None
    row.received_at = _message_time(message) or row.received_at
    row.folders = _first(message, "label_ids", "labelIds") or row.folders
    row.body_snippet = body[:2000] or row.body_snippet
    if not _subject_matches(subject, body):
        log.info("Skipping email %s: no invoice keywords in '%s'", message_id, subject)
        row.outcome, row.error = "skipped", None
        session.commit()
        return []
    session.commit()

    try:
        return _ingest_email(session, user, message, row, message_id, sender, subject, body)
    except Exception as e:
        session.rollback()
        row = _email_row(session, user, message_id)
        row.outcome, row.error = "failed", f"{type(e).__name__}: {e}"[:1000]
        session.commit()
        raise


def _ingest_email(session: Session, user: User, message: dict, row: EmailMessage, message_id: str,
                  sender: str, subject: str, body: str) -> list[int]:
    log.info("Email %s for user %s from %s: '%s'", message_id, user.id, sender, subject)
    atts = attachments_of(message)
    if not atts and not message.get("payload"):
        # Trigger events can be trimmed: fetch the full message once
        log.debug("Email %s: no attachment list in event, fetching full message", message_id)
        message = execute(user, "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", {"message_id": message_id, "format": "full"})
        atts = attachments_of(message)
        body = body or _first(message, "message_text", "messageText", default="") or ""
    log.debug("Email %s attachments: %s", message_id, [(a["filename"], a.get("mime_type")) for a in atts])

    entries = {e["ref"]: e for e in (row.attachments or [])}
    created, seen_names = [], {}
    limit = settings.max_upload_mb * 1024 * 1024
    for att in atts:
        # Gmail attachment ids change between fetches; message id + file name doesn't.
        # A second attachment with the same name gets "#2", "#3"... so it isn't skipped.
        seen_names[att["filename"]] = n = seen_names.get(att["filename"], 0) + 1
        ref = f"{message_id}/{att['filename']}{'' if n == 1 else f'#{n}'}"[:255]
        name = with_extension(att["filename"], att.get("mime_type"))
        if not name:
            log.info("Email %s: skipping '%s' (%s), not a supported file", message_id, att["filename"],
                     att.get("mime_type"))
            entries[ref] = {"ref": ref, "name": att["filename"], "result": "unsupported",
                            "note": att.get("mime_type")}
            continue
        if ref in entries and entries[ref]["result"] != "failed":
            log.debug("Email %s: '%s' already handled, skipping", message_id, name)
            continue
        earlier = _earlier_result(session, ref, name)
        if earlier:   # handled before the email log existed, or by another path: record, don't redo
            entries[ref] = earlier
            continue
        data = execute(user, "GMAIL_GET_ATTACHMENT", {
            "message_id": message_id, "attachment_id": att["attachment_id"], "file_name": name})
        url = (data.get("file") or {}).get("s3url")
        if not url:
            raise RuntimeError(f"GMAIL_GET_ATTACHMENT returned no file for {name}")
        try:
            content = download(url)
        except TooLarge as e:
            log.warning("Email %s: '%s' not processed: %s", message_id, name, e)
            entries[ref] = {"ref": ref, "name": name, "result": "too_large", "note": str(e)}
            continue
        if Path(name).suffix.lower() in IMAGE_EXT and len(content) < LOGO_MAX_BYTES:
            # A signature or letterhead image: not an invoice, and it mustn't stop the email body
            # (which may be the invoice) from being read
            log.info("Email %s: '%s' (%d bytes) looks like a logo; skipped", message_id, name, len(content))
            entries[ref] = {"ref": ref, "name": name, "result": "unsupported", "note": "small image (logo/signature)"}
            continue
        documents, skipped = expand(name, content, limit)   # .zip/.eml -> the documents inside
        results = []
        for inner_name, content in documents:
            inner_ref = ref if inner_name == name else f"{ref}/{inner_name.split('/', 1)[1]}"[:255]
            done_before = session.scalar(select(Invoice.id).where(Invoice.source_ref == inner_ref))
            if done_before:   # a container half-processed before an error: don't redo (or re-note) these
                results.append({"ref": inner_ref, "name": Path(inner_name).name, "result": "invoice",
                                "invoice_id": done_before})
                continue
            entry = _ingest_one(session, user, content, Path(inner_name).name, inner_ref, message_id, sender, subject)
            results.append(entry)
            if entry["result"] == "invoice":
                created.append(entry["invoice_id"])
        if len(documents) == 1 and documents[0][0] == name:
            entries[ref] = results[0]
        else:   # a container: one entry for it, listing what was inside
            entries[ref] = {"ref": ref, "name": name, "result": "unpacked" if results else "unsupported",
                            "invoice_ids": [r["invoice_id"] for r in results if r.get("invoice_id")],
                            "note": f"{len(results)} document(s)" + (f"; skipped {', '.join(skipped)}" if skipped else "")}

    # Invoices behind download links in the body (allow-listed supplier portals only)
    if not any(e.get("invoice_id") or e.get("invoice_ids") for e in entries.values()):
        for n, url in enumerate(links_in(body), start=1):
            ref = f"{message_id}/link{n}"
            if ref in entries and entries[ref]["result"] != "failed":
                continue
            entries[ref] = _ingest_link(session, user, url, ref, message_id, sender, subject, limit)
            if entries[ref].get("invoice_id") and entries[ref]["result"] == "invoice":
                created.append(entries[ref]["invoice_id"])
            created += [i for i in entries[ref].get("new_ids", [])]
            entries[ref].pop("new_ids", None)

    # An invoice written in the email body (no usable attachment)
    has_document = any(e.get("invoice_id") or e.get("invoice_ids") for e in entries.values())
    body_ref = f"{message_id}/body"
    if (not has_document and settings.gmail_read_body_invoices and body_ref not in entries
            and looks_like_invoice_text(body) and not _already_seen(session, body_ref)):
        log.info("Email %s: the body looks like an invoice; processing it as a PDF", message_id)
        pdf = body_to_pdf(subject, sender, body)
        entry = _ingest_one(session, user, pdf, f"Email body - {subject[:60] or message_id}.pdf",
                            body_ref, message_id, sender, subject)
        entries[body_ref] = entry
        if entry["result"] == "invoice":
            created.append(entry["invoice_id"])

    row.attachments = list(entries.values())   # a new list, so the JSON column is saved
    row.outcome = "done" if any(e.get("invoice_id") or e.get("invoice_ids") for e in entries.values()) \
        else "no_documents"
    row.error = None
    session.commit()
    if not atts:
        log.info("Email %s has no attachments", message_id)
    return created


LINK_RE = re.compile(r"https://[^\s<>\"'()\[\]]+", re.IGNORECASE)
MAX_LINKS = 5
MAX_REDIRECTS = 3


def _allowed_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    domains = [d.strip().lower().lstrip(".") for d in settings.gmail_link_domains.split(",") if d.strip()]
    return any(host == d or host.endswith("." + d) for d in domains)


def links_in(text: str) -> list[str]:
    """https links in the email text that point to an allowed domain (GMAIL_LINK_DOMAINS)."""
    if not settings.gmail_link_domains.strip():
        return []
    found = []
    for url in LINK_RE.findall(text or ""):
        url = url.rstrip(".,;:!?")
        if _allowed_host(httpx.URL(url).host) and url not in found:
            found.append(url)
    return found[:MAX_LINKS]


class LinkProblem(Exception):
    """The link didn't give a document (a login page, an unsupported file, too large...)."""


def fetch_link(url: str) -> tuple[str, bytes]:
    """Download a document from an allowed https link -> (file name, content). Redirects are
    followed only to other allowed https addresses, so a link can't lead anywhere else."""
    limit = settings.max_upload_mb * 1024 * 1024
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            target = httpx.URL(url)
            if target.scheme != "https" or not _allowed_host(target.host):
                raise LinkProblem(f"link leads outside the allowed domains ({target.host})")
            with client.stream("GET", url) as r:
                if r.is_redirect:
                    url = str(target.join(r.headers.get("location", "")))
                    continue
                r.raise_for_status()
                ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                disposition = r.headers.get("content-disposition", "")
                m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition, re.IGNORECASE)
                raw_name = unquote(m.group(1) if m else Path(target.path).name) or "document"
                name = with_extension(Path(raw_name).name, ctype)
                if not name:
                    raise LinkProblem("the link gives a web page, not a document (a portal that needs a login?)"
                                      if ctype == "text/html" else f"not a supported file ({ctype or 'unknown type'})")
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise LinkProblem(f"larger than {settings.max_upload_mb} MB (MAX_UPLOAD_MB)")
                    chunks.append(chunk)
                return name, b"".join(chunks)
    raise LinkProblem("too many redirects")


def _ingest_link(session: Session, user: User, url: str, ref: str, message_id: str, sender: str, subject: str,
                 limit: int) -> dict:
    shown = url if len(url) <= 120 else url[:117] + "..."
    try:
        name, content = _with_retries("Invoice link download", lambda: fetch_link(url))
    except LinkProblem as e:
        log.info("Email %s: link %s not used: %s", message_id, shown, e)
        return {"ref": ref, "name": shown, "result": "unsupported", "note": str(e)}
    except Exception as e:   # network / HTTP error: tried again by the next sync or the daily check
        log.warning("Email %s: link %s failed: %s", message_id, shown, e)
        return {"ref": ref, "name": shown, "result": "failed", "note": f"{type(e).__name__}: {e}"[:300]}
    documents, skipped = expand(name, content, limit)
    results = [_ingest_one(session, user, data, Path(inner).name,
                           ref if inner == name else f"{ref}/{inner.split('/', 1)[1]}"[:255], message_id, sender, subject)
               for inner, data in documents]
    log.info("Email %s: link %s -> %s", message_id, shown, [r.get("invoice_id") for r in results])
    if len(results) == 1 and documents[0][0] == name:
        return {**results[0], "note": f"from link {shown}"}
    return {"ref": ref, "name": name, "result": "unpacked" if results else "unsupported",
            "invoice_ids": [r["invoice_id"] for r in results if r.get("invoice_id")],
            "new_ids": [r["invoice_id"] for r in results if r["result"] == "invoice"],
            "note": f"from link {shown}; {len(results)} document(s)" + (f"; skipped {', '.join(skipped)}" if skipped else "")}


def _ingest_one(session: Session, user: User, content: bytes, name: str, ref: str, message_id: str,
                sender: str, subject: str) -> dict:
    inv, new = ingest_document(session, content, name, "gmail", user.id,
                               source_ref=ref, sender=sender, email_subject=subject)
    if new:
        log.info("Email %s: '%s' -> invoice %s", message_id, name, inv.id)
        return {"ref": ref, "name": name, "result": "invoice", "invoice_id": inv.id}
    # Same file sent again (another email, maybe another name): keep one record, but note
    # the delivery on it so it doesn't look lost, and so it isn't downloaded again
    log.info("Email %s: '%s' is the same file as invoice %s (recorded on #%s)", message_id, name, inv.id, inv.id)
    audit(session, inv.id, RECEIVED_AGAIN,
          f"Same file received again: '{name}' from {sender or 'unknown sender'}" + (f" ('{subject}')" if subject else ""),
          {"source_ref": ref, "message_id": message_id, "filename": name, "sender": sender})
    session.commit()
    return {"ref": ref, "name": name, "result": "same_file", "invoice_id": inv.id}


def _earlier_result(session: Session, ref: str, name: str) -> dict | None:
    """The log entry for an attachment handled before: its own invoice(s), or a repeat delivery."""
    ids = list(session.scalars(select(Invoice.id).where(
        (Invoice.source_ref == ref) | Invoice.source_ref.like(f"{ref}/%")).order_by(Invoice.id)))
    if len(ids) == 1 and session.scalar(select(Invoice.source_ref).where(Invoice.id == ids[0])) == ref:
        return {"ref": ref, "name": name, "result": "invoice", "invoice_id": ids[0]}
    # A container (.zip/.eml) with some documents in the database may have been cut off
    # half-way: it is unpacked again, and the documents already there are reused
    note = session.scalar(select(AuditLog).where(AuditLog.stage == RECEIVED_AGAIN,
                                                 AuditLog.data["source_ref"].astext == ref))
    if note:
        return {"ref": ref, "name": name, "result": "same_file", "invoice_id": note.invoice_id}
    return None


def _already_seen(session: Session, ref: str) -> bool:
    """An attachment is handled once: as an invoice of its own, or noted as a repeat delivery."""
    if session.scalar(select(Invoice.id).where(Invoice.source_ref == ref)):
        return True
    return session.scalar(select(AuditLog.id).where(
        AuditLog.stage == RECEIVED_AGAIN, AuditLog.data["source_ref"].astext == ref)) is not None


def _message_time(message: dict) -> datetime | None:
    ts = _first(message, "message_timestamp", "messageTimestamp")
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else None
        return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t
    except ValueError:
        return None


def handle_event(session: Session, event: dict) -> list[int]:
    """A Composio trigger event (from the listener)."""
    composio_user = event.get("user_id") or ""
    log.info("Event %s for %s", event.get("trigger_slug"), composio_user)
    log.debug("Event payload keys: %s", sorted((event.get("payload") or {}).keys()))
    if not composio_user.startswith("ap-user-"):
        log.warning("Event for unknown Composio user %r ignored", composio_user)
        return []
    user = session.get(User, int(composio_user.removeprefix("ap-user-")))
    if user is None:
        log.warning("Event for deleted user %r ignored", composio_user)
        return []
    # The sync point is only moved by sync_user(), so a failed event is fetched again there
    return handle_message(session, user, event.get("payload") or {})


SYNC_OVERLAP = timedelta(minutes=10)    # Gmail search can lag behind delivery
STOP_REWINDING_AFTER = timedelta(days=1)  # an email failing this long is left to the daily check
MAX_MESSAGES_PER_SYNC = 500


_sync_locks: dict[int, threading.Lock] = {}
_sync_locks_guard = threading.Lock()


def sync_user(session: Session, user: User, since: datetime | None = None,
              max_messages: int = MAX_MESSAGES_PER_SYNC) -> list[int]:
    """One sync per inbox at a time: "Sync now" while the background sync runs waits for it, then
    finds the emails already done (instead of both moving the sync point at once)."""
    with _sync_locks_guard:
        lock = _sync_locks.setdefault(user.id, threading.Lock())
    with lock:
        session.refresh(user)   # the sync point another sync may just have moved
        return _sync_user(session, user, since, max_messages)


def _sync_user(session: Session, user: User, since: datetime | None, max_messages: int) -> list[int]:
    """Fetch emails since `since` (default: last sync) and ingest them.
    Used when the listener starts and by the manual sync endpoint."""
    if not user.gmail_account_id:
        log.warning("Sync for user %s: Gmail not connected", user.id)
        raise GmailNotConnected("Gmail is not connected for this user: connect and activate it first")
    since = since or user.gmail_last_sync_at or datetime.now(timezone.utc) - timedelta(days=1)
    started_from = user.gmail_last_sync_at
    next_since = datetime.now(timezone.utc)   # moved back below any email that fails
    query = f"{settings.gmail_query} after:{int((since - SYNC_OVERLAP).timestamp())}".strip()
    log.info("Gmail sync for user %s since %s (query: %s)", user.id, since.isoformat(timespec="seconds"), query)
    created, page_token, fetched, oldest = [], None, 0, None
    while fetched < max_messages:
        args = {"query": query, "max_results": min(25, max_messages - fetched), "include_payload": True}
        if page_token:
            args["page_token"] = page_token
        data = execute(user, "GMAIL_FETCH_EMAILS", args)
        messages = data.get("messages") or []
        for m in messages:
            seen_at = _message_time(m)
            if seen_at and (oldest is None or seen_at < oldest):
                oldest = seen_at
            try:
                created += handle_message(session, user, m)
            except Exception as e:
                session.rollback()
                if is_network_error(e):
                    log.warning("Gmail sync: email %s skipped (%s); will retry next sync", m.get("messageId"), e)
                else:
                    log.exception("Gmail sync: email %s failed; will retry next sync", m.get("messageId"))
                if _failing_for_long(session, user, m):
                    log.warning("Gmail sync: email %s keeps failing; left to the daily check (and the email log)",
                                m.get("messageId"))
                    continue
                failed_at = seen_at or since
                next_since = min(next_since, failed_at - timedelta(minutes=1))
        fetched += len(messages)
        page_token = data.get("nextPageToken") or data.get("next_page_token")
        if not page_token or not messages:
            page_token = None
            break
    if page_token and oldest:
        # More emails than one sync takes: the next sync continues from the oldest one handled
        # (Gmail lists newest first), so none are skipped
        log.warning("Gmail sync for user %s stopped at %d emails; continuing next sync", user.id, fetched)
        next_since = min(next_since, oldest)
    # A live event that failed during this sync moved the sync point back: keep that
    session.refresh(user)
    rewound = user.gmail_last_sync_at
    if rewound and started_from and rewound < started_from:
        next_since = min(next_since, rewound)
    user.gmail_last_sync_at = next_since
    session.commit()
    log.info("Gmail sync for user %s: %d message(s), %d new invoice(s)", user.id, fetched, len(created))
    return created


def _failing_for_long(session: Session, user: User, message: dict) -> bool:
    """The email has been failing for over STOP_REWINDING_AFTER (e.g. a broken file): keeping the
    sync point before it would make every sync re-read an ever longer window."""
    message_id = _first(message, "id", "message_id", "messageId")
    first_seen = session.scalar(select(EmailMessage.created_at).where(
        EmailMessage.user_id == user.id, EmailMessage.message_id == message_id)) if message_id else None
    return first_seen is not None and datetime.now(timezone.utc) - first_seen > STOP_REWINDING_AFTER


# ---------- Gmail labels (AP/Processed, AP/Needs review, AP/Not invoice)

LABELS = {"processed": "Processed", "review": "Needs review", "not_invoice": "Not invoice"}
REVIEW_STATUSES = {Status.NEEDS_REVIEW, Status.FAILED, Status.NEEDS_PASSWORD}
PENDING_STATUSES = {Status.RECEIVED, Status.PROCESSING, Status.APPROVED}
DONE_STATUSES = {Status.EXPORTED, Status.DUPLICATE, Status.REJECTED}
LABEL_WINDOW_DAYS = 30


def label_name(key: str) -> str:
    return f"{settings.gmail_label_prefix}/{LABELS[key]}"


def desired_label(session: Session, row: EmailMessage) -> str | None:
    """Which AP label the email should carry now; None = still being processed (no label yet)."""
    if row.outcome == "failed":
        return "review"
    ids = set()
    for e in row.attachments or []:
        ids.update(e.get("invoice_ids") or [])
        if e.get("invoice_id"):
            ids.add(e["invoice_id"])
    statuses = set(session.scalars(select(Invoice.status).where(Invoice.id.in_(ids)))) if ids else set()
    if statuses & REVIEW_STATUSES:
        return "review"
    if statuses & PENDING_STATUSES:
        return None
    if statuses & DONE_STATUSES:
        return "processed"
    return "not_invoice"


def _label_id(session: Session, user: User, name: str) -> str:
    row = session.scalar(select(GmailLabel).where(GmailLabel.user_id == user.id, GmailLabel.name == name))
    if row:
        return row.label_id
    labels = execute(user, "GMAIL_LIST_LABELS", {}).get("labels") or []
    found = next((lb for lb in labels if lb.get("name") == name), None)
    if found:
        label_id = found["id"]
    else:
        created = execute(user, "GMAIL_CREATE_LABEL", {"label_name": name})
        label_id = created.get("id") or (created.get("label") or {}).get("id")
        if not label_id:
            raise RuntimeError(f"GMAIL_CREATE_LABEL returned no id for {name}: {created}")
        log.info("Created Gmail label '%s' for user %s", name, user.id)
    session.add(GmailLabel(user_id=user.id, name=name, label_id=label_id))
    session.commit()
    return label_id


def update_labels(session: Session, user: User, max_emails: int = 50) -> int:
    """Put the right AP label on recent emails whose status changed. Returns how many changed."""
    since = datetime.now(timezone.utc) - timedelta(days=LABEL_WINDOW_DAYS)
    rows = session.scalars(select(EmailMessage).where(
        # Emails without documents get no label: the wider search also returns ordinary emails
        EmailMessage.user_id == user.id, EmailMessage.outcome.in_(["done", "failed"]),
        EmailMessage.received_at >= since).order_by(EmailMessage.received_at.desc())).all()
    changed = 0
    for row in rows:
        key = desired_label(session, row)
        name = label_name(key) if key else None
        if name is None or name == row.ap_label:
            continue
        try:
            args = {"message_id": row.message_id, "add_label_ids": [_label_id(session, user, name)]}
            if row.ap_label:
                args["remove_label_ids"] = [_label_id(session, user, row.ap_label)]
            execute(user, "GMAIL_ADD_LABEL_TO_EMAIL", args)
        except Exception as e:
            session.rollback()
            if is_network_error(e):
                raise   # offline: try all of them again later
            # e.g. the email was deleted, or the label was removed in Gmail: look the labels up
            # again next time, and carry on with the other emails
            log.warning("Email %s: couldn't set label '%s': %s", row.message_id, name, e)
            session.query(GmailLabel).filter(GmailLabel.user_id == user.id).delete()
            session.commit()
            continue
        log.info("Email %s labelled '%s'%s", row.message_id, name,
                 f" (was '{row.ap_label}')" if row.ap_label else "")
        row.ap_label = name
        session.commit()
        changed += 1
        if changed >= max_emails:
            break
    return changed


# ---------- daily reconciliation

def reconcile_user(session: Session, user: User, days: int | None = None) -> dict:
    """Check the last `days` days of Gmail against the email log: emails never seen, and
    emails or attachments that failed, are processed now. Returns a summary."""
    days = days or settings.gmail_reconcile_days
    since = datetime.now(timezone.utc) - timedelta(days=days)
    before = set(session.scalars(select(EmailMessage.message_id).where(EmailMessage.user_id == user.id)))
    created = sync_user(session, user, since)
    rows = session.scalars(select(EmailMessage).where(EmailMessage.user_id == user.id,
                                                      EmailMessage.received_at >= since)).all()
    summary = {
        "days": days, "emails": len(rows), "new_invoices": created,
        "emails_found_missing": sorted(r.message_id for r in rows if r.message_id not in before),
        "by_outcome": dict(Counter(r.outcome for r in rows)),
        "failed": [{"message_id": r.message_id, "subject": r.subject, "error": r.error}
                   for r in rows if r.outcome == "failed"],
    }
    log.info("Gmail check for user %s, last %d days: %d email(s), %d were missing from the log, "
             "%d new invoice(s), %d failed", user.id, days, len(rows), len(summary["emails_found_missing"]),
             len(created), len(summary["failed"]))
    return summary


# ---------- hints for the AI

PUBLIC_MAIL_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "outlook.com", "hotmail.com",
                       "live.com", "rediffmail.com", "icloud.com", "proton.me", "protonmail.com"}


def known_supplier(session: Session, sender: str | None) -> tuple[str, str] | None:
    """(name, GSTIN) when every approved invoice from this sender came from one supplier.
    Company domains match by domain; public mail (gmail.com, ...) by the exact address."""
    address = parseaddr(sender or "")[1].lower()
    if "@" not in address:
        return None
    domain = address.split("@", 1)[1]
    pattern = f"%<{address}>%" if domain in PUBLIC_MAIL_DOMAINS else f"%@{domain}%"
    rows = session.execute(select(Invoice.supplier_name, Invoice.supplier_gstin).where(
        Invoice.status == Status.EXPORTED, Invoice.supplier_gstin.is_not(None),
        (Invoice.sender.ilike(pattern)) | (Invoice.sender.ilike(address)))).all()
    gstins = {g for _, g in rows}
    if len(gstins) != 1:
        return None
    return rows[0][0], rows[0][1]


def email_hints(session: Session, inv: Invoice) -> str | None:
    """Email context for the AI: subject, sender, start of the body, usual supplier."""
    lines = []
    if inv.source == "gmail" and inv.source_ref:
        row = session.scalar(select(EmailMessage).where(EmailMessage.user_id == inv.user_id,
                                                        EmailMessage.message_id == inv.source_ref.split("/")[0]))
        if row:
            lines += [f"From: {row.sender or ''}", f"Subject: {row.subject or ''}"]
            if row.body_snippet:
                lines.append(f"Body: {row.body_snippet[:600]}")
    if inv.source == "gmail" and inv.original_filename:
        lines.append(f"Attachment name: {inv.original_filename}")
    known = known_supplier(session, inv.sender) if inv.source == "gmail" else None
    if known:
        lines.append(f"Earlier approved invoices from this sender were from {known[0]} (GSTIN {known[1]})")
        from app.pipeline.learning import correction_notes
        notes = correction_notes(session, known[1])
        if notes:
            lines.append(notes)
    return "\n".join(lines) or None
