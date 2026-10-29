"""Messages and calls we start: WhatsApp messages and phone calls through Twilio's REST API, and
reminder emails through a connected Gmail (Composio).

Used by app/followup.py (reminders) and for replies to what people send us. Nothing here decides
who may be contacted: callers pass numbers taken from WHATSAPP_SENDERS and addresses from
FOLLOWUP_EMAIL_TO (the allow-lists).

FOLLOWUP_DRY_RUN=true (the default) sends nothing: the result says "dry_run" and the caller records
what would have been sent. Errors come back as a result with status "failed" and a reason that never
contains a number, the text or a token.
"""
import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass

from app.config import settings

log = logging.getLogger("outbound")
HTTP_TIMEOUT_SECONDS = 15
CALL_RING_SECONDS = 25
MAX_BODY = 1000          # WhatsApp allows 1,600; shorter reads better and leaves room


@dataclass
class Result:
    status: str                 # sent | dry_run | failed
    sid: str | None = None
    detail: str | None = None   # why it failed / was not sent


_client = None


def client():
    """The Twilio REST client (made once; its HTTP calls time out instead of hanging a thread)."""
    global _client
    if _client is None:
        from twilio.http.http_client import TwilioHttpClient
        from twilio.rest import Client
        _client = Client(settings.twilio_account_sid, settings.twilio_auth_token,
                         http_client=TwilioHttpClient(timeout=HTTP_TIMEOUT_SECONDS))
    return _client


def one_line(text: str, limit: int = MAX_BODY) -> str:
    """WhatsApp template variables cannot hold line breaks or runs of spaces."""
    flat = " | ".join(part.strip() for part in text.splitlines() if part.strip())
    flat = " ".join(flat.split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def _failure(e: Exception) -> Result:
    """Why Twilio refused: HTTP status, its error code (e.g. 63016 = outside the 24-hour window; 0 = none
    given) and its message, with anything that looks like a phone number removed."""
    status, code, msg = getattr(e, "status", None), getattr(e, "code", None), getattr(e, "msg", "") or ""
    if status is None and code is None:
        return Result("failed", detail=type(e).__name__)       # not a Twilio error: the class only
    msg = re.sub(r"\+?\d{8,}", "<number>", str(msg)).strip()[:200]
    return Result("failed", detail=f"Twilio HTTP {status}, code {code}" + (f": {msg}" if msg else ""))


def configured_for_whatsapp() -> str | None:
    """None when sending is possible, else what is missing."""
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        return "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set"
    if not settings.twilio_whatsapp_from:
        return "TWILIO_WHATSAPP_FROM is not set"
    return None


def send_whatsapp(number: str, body: str, *, template: bool = False) -> Result:
    """One WhatsApp message to `number` (+919876543210). template=True sends it through the approved
    content template (needed when the 24-hour window is closed): the text becomes its single variable."""
    if settings.followup_dry_run:
        return Result("dry_run")
    missing = configured_for_whatsapp()
    if missing:
        return Result("failed", detail=missing)
    if template and not settings.twilio_whatsapp_template_sid:
        return Result("failed", detail="TWILIO_WHATSAPP_TEMPLATE_SID is not set")
    try:
        if template:
            message = client().messages.create(
                from_=settings.twilio_whatsapp_from, to=f"whatsapp:{number}",
                content_sid=settings.twilio_whatsapp_template_sid,
                content_variables=json.dumps({"1": one_line(body)}))
        else:
            message = client().messages.create(
                from_=settings.twilio_whatsapp_from, to=f"whatsapp:{number}", body=body[:MAX_BODY])
        return Result("sent", sid=message.sid)
    except Exception as e:
        log.warning("WhatsApp message not sent: %s", _failure(e).detail)
        return _failure(e)


def email_marker() -> str:
    """Written at the foot of every reminder email. The Gmail listener skips emails that carry it: a
    reminder lists amounts and says "invoice", so it would otherwise be read as an invoice in the body.
    Keyed with COMPOSIO_API_KEY, so a supplier can't put it in a real invoice to have it skipped."""
    key = (settings.composio_api_key or "invoice-ap").encode()
    return "ref ap-" + hmac.new(key, b"follow-up reminder email", hashlib.sha256).hexdigest()[:16]


def is_our_reminder(text: str | None) -> bool:
    return bool(text) and email_marker() in text


def send_email(sender, to: str, subject: str, html: str) -> Result:
    """A reminder email to `to`, from `sender`'s connected Gmail (a User). Not retried here: a send that
    timed out may have gone out (app/gmail.py send_email); the follow-up retries a failure later."""
    if settings.followup_dry_run:
        return Result("dry_run")
    if sender is None:
        return Result("failed", detail="no connected Gmail to send from (FOLLOWUP_EMAIL_SENDER_USER_ID)")
    from app import gmail   # here, not at the top: gmail imports this module
    try:
        gmail.send_email(sender, [to], subject, html)
        return Result("sent")
    except Exception as e:
        detail = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<address>", str(e) or type(e).__name__).strip()[:200]
        log.warning("Reminder email not sent: %s", detail)
        return Result("failed", detail=f"Gmail: {detail}")


def place_call(number: str, url: str, status_callback: str, twiml: str | None = None) -> Result:
    """Start a phone call. With `twiml`, Twilio is given what to do when it is answered; otherwise it
    fetches that from `url` (app/voice.py). A trial account fetches `url` without signing the request,
    which the app refuses, so `twiml` is tried first."""
    if settings.followup_dry_run:
        return Result("dry_run")
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        return Result("failed", detail="TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN are not set")
    if not settings.twilio_voice_from:
        return Result("failed", detail="TWILIO_VOICE_FROM is not set")
    # Most limits first. Trial accounts refuse some parameters (HTTP 400, "trial accounts have limited
    # parameter access"), so on that refusal the call is tried again with fewer: then the call has no
    # time limit of its own (VOICE_MAX_TURNS still ends the conversation).
    attempts = []
    for target in ([{"twiml": twiml}] if twiml else []) + [{"url": url}]:
        base = {"to": number, "from_": settings.twilio_voice_from, **target}
        attempts += [
            {**base, "status_callback": status_callback, "status_callback_event": ["completed"],
             "timeout": CALL_RING_SECONDS, "time_limit": settings.voice_max_seconds},
            {**base, "status_callback": status_callback},   # Twilio's default event is "completed"
            base,                                           # no callback: the ladder moves on after CALL_WAIT_MINUTES
        ]
    for i, params in enumerate(attempts):
        try:
            call = client().calls.create(**params)
            if i:
                log.info("Call placed with fewer settings (trial account): %s",
                         ", ".join(k for k in params if k not in ("from_", "to")))
            return Result("sent", sid=call.sid)
        except Exception as e:
            failure = _failure(e)
            if not (getattr(e, "status", None) == 400 and "parameter access" in str(getattr(e, "msg", "")).lower()):
                log.warning("Call not placed: %s", failure.detail)
                return failure
    log.warning("Call not placed: %s", failure.detail)
    return failure
