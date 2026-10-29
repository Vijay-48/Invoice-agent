"""WhatsApp through Twilio: an incoming channel. Replies and reminders are sent only when
FOLLOWUP_ENABLED=true (app/followup.py, app/outbound.py); otherwise nothing is ever sent back.

Twilio POSTs each incoming message as form data to /api/v1/webhook/whatsapp.
The webhook logs each message and maps the sender to a user (WHATSAPP_SENDERS); messages
from unknown senders, or senders mapped to a missing user, are ignored. For known senders,
each attachment is downloaded and checked after the reply is sent (download_media below)
and handed to ingest_document(), like an upload: stored, de-duplicated by file hash and
queued for the worker (extraction, checks, decision, review).

Every request must carry a valid X-Twilio-Signature (HMAC of the public URL and the
form fields, keyed with TWILIO_AUTH_TOKEN); anything else gets 403. Twilio signs the URL
it called, so PUBLIC_BASE_URL must match the webhook URL in the Twilio Console exactly.
There is deliberately no switch to turn this off: without a token every request is rejected.
"""
import io
import logging
import re
import time
import zipfile

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, Response
from twilio.request_validator import RequestValidator

from app.config import settings
from app.db import SessionLocal
from app.documents import DOCUMENT_EXT, MIME_EXT
from app.ingest import ingest_document
from app.models import User

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


def user_exists(user_id: int) -> bool:
    with SessionLocal() as session:
        return session.get(User, user_id) is not None


def sender_user_id(sender: str, message_sid: str) -> int | None:
    """The user this sender's invoices belong to, or None (with the reason logged) to ignore it."""
    masked = mask_sender(sender)
    senders = parse_senders(settings.whatsapp_senders)
    number = normalize_number(sender)
    if number not in senders:
        log.warning("WhatsApp message %s from %s ignored: sender not in WHATSAPP_SENDERS", message_sid, masked)
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
    """An attachment that is not downloaded or not accepted; the message is safe to log."""


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
                raise MediaRejected(f"download took longer than {MEDIA_DEADLINE_SECONDS}s")
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
                    raise MediaRejected(f"HTTP {r.status_code}")
                if int(r.headers.get("content-length") or 0) > limit:
                    raise MediaRejected(f"larger than {settings.max_upload_mb} MB")
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise MediaRejected(f"larger than {settings.max_upload_mb} MB")
                    if time.monotonic() > deadline:
                        raise MediaRejected(f"download took longer than {MEDIA_DEADLINE_SECONDS}s")
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
        raise MediaRejected(f"file type {declared_type or 'unknown'} is not supported")
    checked_url = check_media_url(url, account_sid, message_sid)
    content, served_type = fetch(checked_url, settings.max_upload_mb * 1024 * 1024)
    if served_type not in GENERIC_TYPES and served_type != declared_type:
        raise MediaRejected(f"served as {served_type}, announced as {declared_type}")
    if not content:
        raise MediaRejected("the file is empty")
    if not content_matches(ext, content):
        raise MediaRejected(f"content is not a {ext} file")
    media_sid = MEDIA_PATH_RE.fullmatch(checked_url.path)[3]
    return f"whatsapp-{message_sid}-{index}{ext}", content, f"{message_sid}/{media_sid}"


def ingest_media(user_id: int, sender: str, name: str, content: bytes, ref: str) -> tuple[int, bool]:
    """Hand one checked attachment to ingest_document() -> (invoice id, created). Its own session,
    closed (and rolled back if unfinished) even when ingest fails. The sender is stored masked."""
    with SessionLocal() as session:
        inv, created = ingest_document(session, content, name, "whatsapp", user_id,
                                       source_ref=ref, sender=mask_sender(sender))
        return inv.id, created


def download_all(message_sid: str, account_sid: str, media: list[tuple[str, str]],
                 user_id: int, sender: str) -> None:
    """Runs after the reply is sent. Each attachment on its own: downloaded, checked, then
    ingested; one that fails doesn't stop the others."""
    from app import followup      # here, not at the top: followup imports this module
    for index, (url, content_type) in enumerate(media):
        try:
            name, content, ref = download_media(url, content_type, account_sid, message_sid, index)
        except MediaRejected as e:
            log.warning("WhatsApp message %s media %d rejected: %s", message_sid, index, e)
            followup.notify_unreadable(user_id, sender)     # does nothing unless FOLLOWUP_ENABLED
            continue
        except httpx.HTTPError as e:      # the class only: httpx messages can contain the URL
            log.warning("WhatsApp message %s media %d download failed: %s", message_sid, index,
                        type(e).__name__)
            followup.notify_unreadable(user_id, sender)
            continue
        except Exception as e:
            log.error("WhatsApp message %s media %d: unexpected %s", message_sid, index, type(e).__name__)
            continue
        log.info("WhatsApp message %s media %d downloaded and checked: %s, %d bytes",
                 message_sid, index, name.rsplit(".", 1)[-1], len(content))
        try:
            invoice_id, created = ingest_media(user_id, sender, name, content, ref)
        except Exception as e:            # e.g. database down; the class only
            log.error("WhatsApp message %s media %d not ingested: %s", message_sid, index, type(e).__name__)
            continue
        if created:
            log.info("WhatsApp message %s media %d -> invoice %s, queued for processing",
                     message_sid, index, invoice_id)
        else:
            log.info("WhatsApp message %s media %d is the same file as invoice %s; not queued again",
                     message_sid, index, invoice_id)
        followup.acknowledge_file(user_id, sender, created)


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
    if user_id is not None and settings.followup_enabled:
        # Remembers when they last wrote (WhatsApp's 24-hour window) and answers 1 / 2 / STOP
        from app import followup      # here, not at the top: followup imports this module
        background.add_task(followup.on_incoming, user_id, From, Body, bool(media))
    if user_id is not None and media:
        if NumMedia > len(media):
            log.warning("WhatsApp message %s: only %d of %d media are downloaded", MessageSid, len(media), NumMedia)
        background.add_task(download_all, MessageSid, AccountSid, media, user_id, From)
    return Response(content=EMPTY_TWIML, media_type="application/xml")
