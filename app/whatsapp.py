"""WhatsApp through Twilio: an incoming channel. Replies and reminders are sent only when
FOLLOWUP_ENABLED=true (app/followup.py, app/outbound.py); otherwise nothing is ever sent back.

Twilio POSTs each incoming message as form data to /api/v1/webhook/whatsapp.
The webhook maps the sender to a user (users.whatsapp_number, else WHATSAPP_SENDERS); messages
from unknown senders, or senders mapped to a missing user, are ignored. For known senders,
each attachment is downloaded and checked after the reply is sent (download_media below)
and handed to ingest_document(), like an upload: stored, de-duplicated by file hash and
queued for the worker (extraction, checks, decision, review).

Every message is logged in whatsapp_messages (no text, masked sender) and each attachment in
whatsapp_media with what became of it. Twilio may send the same message twice; the second is
ignored (unique MessageSid). A download that failed for a passing reason (network, Twilio,
database) is tried again by retry_due() (a background loop), up to MAX_ATTEMPTS times (D-106).

Every request must carry a valid X-Twilio-Signature (HMAC of the public URL and the
form fields, keyed with TWILIO_AUTH_TOKEN); anything else gets 403. Twilio signs the URL
it called, so PUBLIC_BASE_URL must match the webhook URL in the Twilio Console exactly.
There is deliberately no switch to turn this off: without a token every request is rejected.
"""
import io
import logging
import re
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from twilio.request_validator import RequestValidator

from app.config import settings
from app.db import SessionLocal
from app.documents import DOCUMENT_EXT, MIME_EXT
from app.ingest import ingest_document
from app.models import User, WhatsAppMedia, WhatsAppMessage

log = logging.getLogger("whatsapp")


async def verify_twilio_signature(request: Request) -> None:
    """Reject (403) anything Twilio did not sign. Logs the reason only: never the token,
    the signature, the sender or the text."""
    token = settings.twilio_auth_token
    signature = request.headers.get("X-Twilio-Signature")
    if not token:
        log.error("WhatsApp request rejected: TWILIO_AUTH_TOKEN is not set")
    elif not signature:
        log.warning("WhatsApp request rejected: no X-Twilio-Signature header")
    else:
        url = settings.public_base_url.rstrip("/") + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        form = await request.form()   # cached by Starlette; the endpoint reuses it
        if RequestValidator(token).validate(url, dict(form), signature):
            return
        log.warning("WhatsApp request rejected: signature does not match "
                    "(is PUBLIC_BASE_URL the URL set in the Twilio Console?)")
    raise HTTPException(403, "Invalid Twilio signature")


# On the router, so every WhatsApp route is protected (and checked before its fields)
router = APIRouter(prefix="/api/v1/webhook", tags=["whatsapp"],
                   dependencies=[Depends(verify_twilio_signature)])

# Empty TwiML: "received, send nothing back"
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def mask_sender(sender: str) -> str:
    """'whatsapp:+919876543210' -> 'whatsapp:+91******3210', so full numbers stay out of the logs."""
    channel, sep, number = sender.rpartition(":")
    keep_start = 3 if number.startswith("+") else 2
    if len(number) <= keep_start + 4:
        number = "*" * len(number)
    else:
        number = number[:keep_start] + "*" * (len(number) - keep_start - 4) + number[-4:]
    return f"{channel}{sep}{number}"


def normalize_number(value: str) -> str | None:
    """'whatsapp:+91 98765-43210' -> '+919876543210'; None if it is not a phone number.
    The leading + is optional, so '919876543210' in WHATSAPP_SENDERS matches too."""
    value = value.strip()
    if value.lower().startswith("whatsapp:"):
        value = value[len("whatsapp:"):]
    digits = re.sub(r"[\s\-().]", "", value).removeprefix("+")
    return "+" + digits if digits.isdigit() and 8 <= len(digits) <= 15 else None


def parse_senders(raw: str) -> dict[str, int | None]:
    """WHATSAPP_SENDERS -> {number: user id}. A number whose user id is not a positive whole
    number, or that is listed twice with different ids, maps to None (its messages are ignored).
    Bad entries are logged by position only, never with the number."""
    senders: dict[str, int | None] = {}
    for position, entry in enumerate(raw.split(","), start=1):
        if not entry.strip():
            continue
        number, sep, user_id = entry.rpartition(":")
        number = normalize_number(number) if sep else None
        if number is None:
            log.warning("WHATSAPP_SENDERS entry %d is not 'number:user_id'; skipped", position)
            continue
        user_id = user_id.strip()
        uid = int(user_id) if user_id.isdigit() and int(user_id) > 0 else None
        if uid is None:
            log.warning("WHATSAPP_SENDERS entry %d has an invalid user id", position)
        elif number in senders and senders[number] != uid:
            log.warning("WHATSAPP_SENDERS entry %d repeats a number with a different user id", position)
            uid = None
        senders[number] = uid
    return senders


# ---------- the database: who sends, and the message log. Small functions, so the webhook tests can
# replace them and keep their "no database" guarantee.

def user_exists(user_id: int) -> bool:
    with SessionLocal() as session:
        return session.get(User, user_id) is not None


def stored_sender(number: str) -> int | None:
    """The user whose users.whatsapp_number is `number` (set through the API, no restart needed)."""
    with SessionLocal() as session:
        return session.scalar(select(User.id).where(User.whatsapp_number == number))


def record_message(message_sid: str, sender: str, user_id: int | None, kind: str, text_length: int,
                   media: list[tuple[str, str]], media_count: int) -> int | None:
    """Log one incoming message, its attachments as "pending". None if this MessageSid was logged
    before: Twilio sent the same message again, and it must not be handled twice."""
    with SessionLocal() as session:
        message_id = session.scalar(
            pg_insert(WhatsAppMessage).values(message_sid=message_sid, sender=mask_sender(sender)[:40],
                                              user_id=user_id, kind=kind, text_length=text_length,
                                              media_count=media_count)
            .on_conflict_do_nothing(index_elements=["message_sid"]).returning(WhatsAppMessage.id))
        if message_id is None:
            return None
        for position, (url, content_type) in enumerate(media):
            session.add(WhatsAppMedia(message_id=message_id, position=position, url=url,
                                      content_type=content_type[:100], status="pending"))
        session.commit()
        return message_id


def sender_user_id(sender: str, message_sid: str) -> int | None:
    """The user this sender's invoices belong to, or None (with the reason logged) to ignore it.
    users.whatsapp_number first, then WHATSAPP_SENDERS."""
    masked = mask_sender(sender)
    number = normalize_number(sender)
    if number is not None and (user_id := stored_sender(number)) is not None:
        log.info("WhatsApp message %s from %s mapped to user %s", message_sid, masked, user_id)
        return user_id
    senders = parse_senders(settings.whatsapp_senders)
    if number not in senders:
        log.warning("WhatsApp message %s from %s ignored: sender not in WHATSAPP_SENDERS or users.whatsapp_number",
                    message_sid, masked)
        return None
    user_id = senders[number]
    if user_id is None:
        log.warning("WhatsApp message %s from %s ignored: its WHATSAPP_SENDERS entry is invalid",
                    message_sid, masked)
        return None
    if not user_exists(user_id):
        log.warning("WhatsApp message %s from %s ignored: mapped user %s does not exist",
                    message_sid, masked, user_id)
        return None
    log.info("WhatsApp message %s from %s mapped to user %s", message_sid, masked, user_id)
    return user_id


# ---------- media download
#
# Twilio's media URL (api.twilio.com, our credentials) answers with a redirect to its file
# storage. Redirects are followed by hand: only to the hosts below, over HTTPS, and without
# credentials. Media URLs are signed links, so they are never logged (app/logs.py keeps
# httpx's own request lines at WARNING for the same reason).

TWILIO_API_HOST = "api.twilio.com"
MEDIA_REDIRECT_HOSTS = ("twiliocdn.com", "twilio.com", "amazonaws.com")   # the host or a subdomain
MAX_REDIRECTS = 2
MAX_MEDIA = 10                       # Twilio sends at most 10 attachments per message
MEDIA_TIMEOUT = httpx.Timeout(10.0, connect=5.0)   # per connect / read / write
MEDIA_DEADLINE_SECONDS = 60          # whole download, so a slow trickle can't hold a thread
ACCOUNT_SID_RE = re.compile(r"AC[0-9a-fA-F]{32}")
MESSAGE_SID_RE = re.compile(r"(SM|MM)[0-9a-fA-F]{32}")
MEDIA_PATH_RE = re.compile(r"/2010-04-01/Accounts/(AC[0-9a-fA-F]{32})/Messages/((?:SM|MM)[0-9a-fA-F]{32})"
                           r"/Media/(ME[0-9a-fA-F]{32})")
# Single documents only (the upload's types without .zip/.eml containers)
MEDIA_TYPES = {mime: ext for mime, ext in MIME_EXT.items() if ext in DOCUMENT_EXT}
GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream"}   # what file storage may answer


class MediaRejected(Exception):
    """An attachment that is not downloaded or not accepted; the message is safe to log.
    `retry`: a passing problem (Twilio busy, too slow), worth trying again later.
    `user_text`: what the sender may be told (None = only that it couldn't be used)."""

    def __init__(self, message: str, *, retry: bool = False, user_text: str | None = None):
        super().__init__(message)
        self.retry, self.user_text = retry, user_text


TOO_LARGE = "it is larger than {mb} MB"
NOT_SUPPORTED = "that file type isn't supported: send a PDF, a photo (JPG, PNG, WEBP), or an Excel, Word or CSV file"
DAMAGED = "the file seems damaged, or isn't the type its name says"


async def media_fields(request: Request) -> list[tuple[str, str]]:
    """[(MediaUrl<i>, MediaContentType<i>)] from the (already signature-checked) form."""
    form = await request.form()
    return [(str(form.get(f"MediaUrl{i}", "")), str(form.get(f"MediaContentType{i}", "")))
            for i in range(MAX_MEDIA) if f"MediaUrl{i}" in form]


def check_media_url(url: str, account_sid: str, message_sid: str) -> httpx.URL:
    """The URL if it is this account's media for this message on api.twilio.com over HTTPS."""
    try:
        u = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        raise MediaRejected("not a valid URL") from None
    if (u.scheme != "https" or u.host != TWILIO_API_HOST or u.port not in (None, 443)
            or u.userinfo or u.query or u.fragment):
        raise MediaRejected("not a Twilio media URL")
    m = MEDIA_PATH_RE.fullmatch(u.path)
    if not m:
        raise MediaRejected("not a Twilio media URL")
    if m[1] != account_sid or m[2] != message_sid:
        raise MediaRejected("media URL is for another account or message")
    return u


def allowed_redirect(u: httpx.URL) -> bool:
    return (u.scheme == "https" and u.port in (None, 443) and not u.userinfo
            and any(u.host == h or u.host.endswith("." + h) for h in MEDIA_REDIRECT_HOSTS))


def media_client() -> httpx.Client:
    """No automatic redirects (followed by hand), no proxy or .netrc settings from the environment."""
    return httpx.Client(timeout=MEDIA_TIMEOUT, follow_redirects=False, trust_env=False)


def fetch(url: httpx.URL, limit: int) -> tuple[bytes, str]:
    """GET the media -> (content, response content type). Credentials go only to the first
    request (api.twilio.com); redirects only to MEDIA_REDIRECT_HOSTS, without credentials."""
    auth = (settings.twilio_account_sid, settings.twilio_auth_token)
    deadline = time.monotonic() + MEDIA_DEADLINE_SECONDS
    with media_client() as client:
        for hop in range(MAX_REDIRECTS + 1):
            if time.monotonic() > deadline:
                raise MediaRejected(f"download took longer than {MEDIA_DEADLINE_SECONDS}s", retry=True)
            with client.stream("GET", url, auth=auth if hop == 0 else None) as r:
                if r.is_redirect:
                    location = r.headers["location"].strip()
                    if not location:
                        raise MediaRejected("redirect without a location")
                    try:
                        url = r.url.join(location)
                    except httpx.InvalidURL:
                        raise MediaRejected("redirect to an invalid URL") from None
                    if not allowed_redirect(url):
                        raise MediaRejected(f"redirect to a host that is not allowed ({url.host or 'none'})")
                    continue
                if r.status_code != 200:
                    raise MediaRejected(f"HTTP {r.status_code}", retry=r.status_code == 429 or r.status_code >= 500)
                too_large = TOO_LARGE.format(mb=settings.max_upload_mb)
                if int(r.headers.get("content-length") or 0) > limit:
                    raise MediaRejected(f"larger than {settings.max_upload_mb} MB", user_text=too_large)
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise MediaRejected(f"larger than {settings.max_upload_mb} MB", user_text=too_large)
                    if time.monotonic() > deadline:
                        raise MediaRejected(f"download took longer than {MEDIA_DEADLINE_SECONDS}s", retry=True)
                    chunks.append(chunk)
                content_type = r.headers.get("content-type", "").split(";")[0].strip().lower()
                return b"".join(chunks), content_type
    raise MediaRejected("too many redirects")


def content_matches(ext: str, content: bytes) -> bool:
    """The file's first bytes (or structure) fit its type, so a renamed file is not accepted."""
    if ext == ".pdf":
        return content.startswith(b"%PDF-")
    if ext == ".png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return content.startswith(b"\xff\xd8\xff")
    if ext == ".webp":
        return content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    if ext in (".xlsx", ".xlsm", ".docx"):   # Office files are zips with a known part inside
        part = "word/document.xml" if ext == ".docx" else "xl/workbook.xml"
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                return part in z.namelist()
        except zipfile.BadZipFile:
            return False
    if ext == ".csv":
        return b"\x00" not in content[:4096]
    return False


def download_media(url: str, declared_type: str, account_sid: str, message_sid: str,
                   index: int) -> tuple[str, bytes, str]:
    """One attachment -> (file name, content, source_ref "<MessageSid>/<MediaSid>"),
    or MediaRejected with the reason."""
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        raise MediaRejected("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is not set")
    if account_sid != settings.twilio_account_sid or not ACCOUNT_SID_RE.fullmatch(account_sid):
        raise MediaRejected("AccountSid is not this Twilio account")
    if not MESSAGE_SID_RE.fullmatch(message_sid):
        raise MediaRejected("MessageSid is not a Twilio message SID")
    declared_type = declared_type.split(";")[0].strip().lower()
    ext = MEDIA_TYPES.get(declared_type)
    if ext is None:
        raise MediaRejected(f"file type {declared_type or 'unknown'} is not supported", user_text=NOT_SUPPORTED)
    checked_url = check_media_url(url, account_sid, message_sid)
    content, served_type = fetch(checked_url, settings.max_upload_mb * 1024 * 1024)
    if served_type not in GENERIC_TYPES and served_type != declared_type:
        raise MediaRejected(f"served as {served_type}, announced as {declared_type}", user_text=DAMAGED)
    if not content:
        raise MediaRejected("the file is empty", user_text=DAMAGED)
    if not content_matches(ext, content):
        raise MediaRejected(f"content is not a {ext} file", user_text=DAMAGED)
    media_sid = MEDIA_PATH_RE.fullmatch(checked_url.path)[3]
    return f"whatsapp-{message_sid}-{index}{ext}", content, f"{message_sid}/{media_sid}"


def ingest_media(user_id: int, sender: str, name: str, content: bytes, ref: str) -> tuple[int, bool]:
    """Hand one checked attachment to ingest_document() -> (invoice id, created). Its own session,
    closed (and rolled back if unfinished) even when ingest fails. The sender is stored masked."""
    with SessionLocal() as session:
        inv, created = ingest_document(session, content, name, "whatsapp", user_id,
                                       source_ref=ref, sender=mask_sender(sender))
        return inv.id, created


# ---------- one attachment: download, ingest, record, tell the sender

MAX_ATTEMPTS = 4
RETRY_MINUTES = (2, 10, 30)          # after the 1st, 2nd and 3rd failed attempt
RETRY_LEASE_MINUTES = 10             # a claimed retry is not claimed again for this long
STALE_PENDING_MINUTES = 15           # "pending" this long: the process stopped mid-way; try again
RETRY_EVERY_SECONDS = 60
RETRY_BATCH = 20


@dataclass
class Outcome:
    status: str                      # ingested | duplicate | rejected | failed
    reason: str | None = None        # safe to log and store: never a URL, number or text
    invoice_id: int | None = None
    user_text: str | None = None     # for a rejected file: what the sender may be told


def _now() -> datetime:
    return datetime.now().astimezone()


def handle_media(message_sid: str, account_sid: str, position: int, url: str, content_type: str,
                 user_id: int, sender: str) -> Outcome:
    """Download, check and ingest one attachment. Never raises: every problem becomes an Outcome."""
    try:
        name, content, ref = download_media(url, content_type, account_sid, message_sid, position)
    except MediaRejected as e:
        log.warning("WhatsApp message %s media %d rejected: %s", message_sid, position, e)
        return Outcome("failed" if e.retry else "rejected", str(e)[:300], user_text=e.user_text)
    except httpx.HTTPError as e:      # the class only: httpx messages can contain the URL
        log.warning("WhatsApp message %s media %d download failed: %s", message_sid, position, type(e).__name__)
        return Outcome("failed", f"download failed: {type(e).__name__}")
    except Exception as e:
        log.error("WhatsApp message %s media %d: unexpected %s", message_sid, position, type(e).__name__)
        return Outcome("failed", f"unexpected {type(e).__name__}")
    log.info("WhatsApp message %s media %d downloaded and checked: %s, %d bytes",
             message_sid, position, name.rsplit(".", 1)[-1], len(content))
    try:
        invoice_id, created = ingest_media(user_id, sender, name, content, ref)
    except Exception as e:            # e.g. database down; the class only
        log.error("WhatsApp message %s media %d not ingested: %s", message_sid, position, type(e).__name__)
        return Outcome("failed", f"not ingested: {type(e).__name__}")
    if created:
        log.info("WhatsApp message %s media %d -> invoice %s, queued for processing", message_sid, position, invoice_id)
    else:
        log.info("WhatsApp message %s media %d is the same file as invoice %s; not queued again",
                 message_sid, position, invoice_id)
    return Outcome("ingested" if created else "duplicate", invoice_id=invoice_id)


def save_media_result(message_sid: str, position: int, outcome: Outcome, attempt: int) -> str:
    """Write what became of an attachment. Returns its status: a failure becomes "failed" (tried again
    later) or, after MAX_ATTEMPTS, "gave_up"."""
    status, next_try_at = outcome.status, None
    if status == "failed":
        if attempt >= MAX_ATTEMPTS:
            status = "gave_up"
        else:
            next_try_at = _now() + timedelta(minutes=RETRY_MINUTES[min(attempt, len(RETRY_MINUTES)) - 1])
    with SessionLocal() as session:
        media = session.scalar(select(WhatsAppMedia).join(WhatsAppMessage)
                               .where(WhatsAppMessage.message_sid == message_sid, WhatsAppMedia.position == position))
        if media is not None:
            media.status, media.reason, media.attempts, media.next_try_at = status, outcome.reason, attempt, next_try_at
            media.invoice_id = outcome.invoice_id or media.invoice_id
            session.commit()
    return status


def process_media(message_sid: str, account_sid: str, position: int, url: str, content_type: str,
                  user_id: int, sender: str, attempt: int) -> str:
    """One attempt at one attachment, recorded, and the sender told (only when FOLLOWUP_ENABLED)."""
    from app import followup  # here, not at the top: followup imports this module
    outcome = handle_media(message_sid, account_sid, position, url, content_type, user_id, sender)
    try:
        status = save_media_result(message_sid, position, outcome, attempt)
    except Exception as e:            # the row stays "pending" and is picked up again (STALE_PENDING_MINUTES)
        log.error("WhatsApp message %s media %d: result not saved: %s", message_sid, position, type(e).__name__)
        status = outcome.status
    if status in ("ingested", "duplicate"):
        followup.acknowledge_file(user_id, sender, status == "ingested")
    elif status == "rejected":
        followup.notify_unreadable(user_id, sender, outcome.user_text)
    elif status == "failed" and attempt == 1:
        followup.notify_retrying(user_id, sender)
    elif status == "gave_up":
        followup.notify_unreadable(user_id, sender, "I couldn't download it after several tries")
    return status


def download_all(message_sid: str, account_sid: str, media: list[tuple[str, str]],
                 user_id: int, sender: str) -> None:
    """Runs after the reply is sent. Each attachment on its own: downloaded, checked, then
    ingested; one that fails doesn't stop the others."""
    for position, (url, content_type) in enumerate(media):
        process_media(message_sid, account_sid, position, url, content_type, user_id, sender, attempt=1)


def retry_due(now: datetime | None = None) -> int:
    """Try again the attachments whose download or ingest failed for a passing reason, and those left
    "pending" by a process that stopped. Claimed with SKIP LOCKED and a lease, so several processes can
    run this. Returns how many were tried."""
    from app import followup  # here, not at the top: followup imports this module
    now = now or _now()
    with SessionLocal() as session:
        rows = session.execute(
            select(WhatsAppMedia, WhatsAppMessage).join(WhatsAppMessage)
            .where(WhatsAppMessage.user_id.is_not(None),
                   or_(and_(WhatsAppMedia.status == "failed", WhatsAppMedia.next_try_at <= now),
                       and_(WhatsAppMedia.status == "pending",
                            WhatsAppMedia.updated_at < now - timedelta(minutes=STALE_PENDING_MINUTES))))
            .order_by(WhatsAppMedia.id).limit(RETRY_BATCH)
            .with_for_update(of=WhatsAppMedia, skip_locked=True)).all()
        jobs = []
        for media, message in rows:
            media.status, media.next_try_at = "failed", now + timedelta(minutes=RETRY_LEASE_MINUTES)
            jobs.append((message.message_sid, media.position, media.url, media.content_type, message.user_id,
                         message.sender, media.attempts + 1))
        session.commit()
    for message_sid, position, url, content_type, user_id, masked, attempt in jobs:
        number = followup.number_for(user_id)        # the stored sender is masked; replies need the number
        sender = f"whatsapp:{number}" if number else masked
        status = process_media(message_sid, settings.twilio_account_sid, position, url, content_type,
                               user_id, sender, attempt)
        log.info("WhatsApp message %s media %d, attempt %d: %s", message_sid, position, attempt, status)
    return len(jobs)


def retry_now(session, media_id: int) -> WhatsAppMedia | None:
    """Queue one attachment to be tried again at the next retry check (from the API), whatever its status
    except already received. It gets at least one more attempt."""
    media = session.get(WhatsAppMedia, media_id)
    if media is None or media.status in ("ingested", "duplicate"):
        return media
    media.status, media.next_try_at = "failed", _now()
    media.attempts = min(media.attempts, MAX_ATTEMPTS - 1)
    session.commit()
    return media


def run_retries(stop: threading.Event) -> None:
    """Background loop (started with the API when TWILIO_ACCOUNT_SID is set)."""
    while not stop.is_set():
        try:
            retry_due()
        except Exception:
            log.exception("WhatsApp retry check failed (will try again)")
        stop.wait(RETRY_EVERY_SECONDS)


def serialize_message(message: WhatsAppMessage, media: list[WhatsAppMedia]) -> dict:
    return {"id": message.id, "message_sid": message.message_sid, "sender": message.sender,
            "user_id": message.user_id, "kind": message.kind, "text_length": message.text_length,
            "media_count": message.media_count, "created_at": message.created_at,
            "media": [{"id": m.id, "position": m.position, "content_type": m.content_type, "status": m.status,
                       "reason": m.reason, "invoice_id": m.invoice_id, "attempts": m.attempts,
                       "next_try_at": m.next_try_at, "updated_at": m.updated_at} for m in media]}


@router.post("/whatsapp", response_class=Response)
def incoming_message(background: BackgroundTasks, From: str = Form(...), MessageSid: str = Form(...),
                     Body: str = Form(""), NumMedia: int = Form(0), AccountSid: str = Form(""),
                     media: list[tuple[str, str]] = Depends(media_fields)):
    """One incoming WhatsApp message from Twilio. Logs metadata only (never the text).
    A plain def: FastAPI runs it in a worker thread, so the user lookup doesn't block.
    Attachments are downloaded after the reply is sent, so Twilio gets its answer quickly."""
    log.info("WhatsApp message %s from %s: %d characters of text, %d media",
             MessageSid, mask_sender(From), len(Body), NumMedia)
    user_id = sender_user_id(From, MessageSid)
    kind = "ignored" if user_id is None else "files" if media else "text"
    try:
        logged = record_message(MessageSid, From, user_id, kind, len(Body), media if user_id is not None else [],
                                max(NumMedia, len(media)))
    except Exception as e:            # still handled: the invoice matters more than the log
        log.error("WhatsApp message %s not logged: %s", MessageSid, type(e).__name__)
        logged = 0
    if logged is None:
        log.info("WhatsApp message %s was already received (Twilio sent it again); not handled twice", MessageSid)
        return Response(content=EMPTY_TWIML, media_type="application/xml")
    if user_id is not None and not media and settings.whatsapp_grn:
        from app import grn_chat  # here, not at the top: it imports modules that import this one
        if grn_chat.is_grn_message(Body):
            background.add_task(grn_chat.on_message, user_id, From, Body)
            return Response(content=EMPTY_TWIML, media_type="application/xml")
    if user_id is not None and settings.followup_enabled:
        # Remembers when they last wrote (WhatsApp's 24-hour window) and answers 1 / 2 / STOP
        from app import followup  # here, not at the top: followup imports this module
        background.add_task(followup.on_incoming, user_id, From, Body, bool(media))
    if user_id is not None and media:
        if NumMedia > len(media):
            log.warning("WhatsApp message %s: only %d of %d media are downloaded", MessageSid, len(media), NumMedia)
        background.add_task(download_all, MessageSid, AccountSid, media, user_id, From)
    return Response(content=EMPTY_TWIML, media_type="application/xml")
