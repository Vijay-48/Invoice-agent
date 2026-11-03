# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""The WhatsApp message log, retries of failed downloads and senders stored on users (app/whatsapp.py,
D-106), against the test database. Twilio is faked (signed requests, httpx.MockTransport for media)."""
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app import whatsapp
from app.config import settings
from app.models import Invoice, User, WhatsAppMedia, WhatsAppMessage
from tests.test_flow import client  # noqa: F401 (fixture)

TOKEN = "test-token-not-real"
BASE_URL = "https://example.test"
PATH = "/api/v1/webhook/whatsapp"
NUMBER = "+919876543210"
ACCOUNT_SID = "AC" + "a" * 32
CDN_URL = "https://media.twiliocdn.com/file?Expires=1&Signature=x"
PDF = b"%PDF-1.7\n" + b"1" * 200


def sid(n: int) -> str:
    return "MM" + f"{n:032x}"


def media_url(message_sid: str) -> str:
    return f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Messages/{message_sid}/Media/ME{'c' * 32}"


@pytest.fixture
def twilio(monkeypatch):
    """Signed webhook requests and a fake media store. `.mode`: "ok", "busy" (503) or "gif"."""
    for name, value in dict(twilio_auth_token=TOKEN, twilio_account_sid=ACCOUNT_SID, public_base_url=BASE_URL,
                            whatsapp_senders="", followup_enabled=False).items():
        monkeypatch.setattr(settings, name, value)
    state = SimpleNamespace(mode="ok", downloads=0)

    def handler(request):
        if request.url.host == "api.twilio.com":
            state.downloads += 1
            if state.mode == "busy":
                return httpx.Response(503)
            return httpx.Response(307, headers={"Location": CDN_URL})
        return httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"})

    monkeypatch.setattr(whatsapp, "media_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False))
    app = FastAPI()
    app.include_router(whatsapp.router)
    with TestClient(app) as c:
        state.client = c
        yield state


def send(twilio, message_sid, sender=f"whatsapp:{NUMBER}", content_type="application/pdf", media=True):
    data = {"From": sender, "MessageSid": message_sid, "AccountSid": ACCOUNT_SID, "Body": "invoice", "NumMedia": "0"}
    if media:
        data |= {"NumMedia": "1", "MediaUrl0": media_url(message_sid), "MediaContentType0": content_type}
    signature = RequestValidator(TOKEN).compute_signature(BASE_URL + PATH, data)
    r = twilio.client.post(PATH, data=data, headers={"X-Twilio-Signature": signature})
    assert r.status_code == 200
    return r


def logged(session):
    session.expire_all()
    return session.query(WhatsAppMessage).order_by(WhatsAppMessage.id).all(), \
        session.query(WhatsAppMedia).order_by(WhatsAppMedia.id).all()


@pytest.fixture
def priya(session, users):
    users["priya"].whatsapp_number = NUMBER
    session.commit()
    return users["priya"]


def test_a_number_stored_on_the_user_is_enough(twilio, priya, session):
    """No WHATSAPP_SENDERS: the number on the user maps the sender; the file becomes an invoice and the log says so."""
    send(twilio, sid(1))
    (message,), (media,) = logged(session)
    assert (message.user_id, message.kind, message.media_count, message.sender) == (priya.id, "files", 1,
                                                                                    "whatsapp:+91******3210")
    inv = session.get(Invoice, media.invoice_id)
    assert (media.status, media.attempts, inv.source, inv.user_id) == ("ingested", 1, "whatsapp", priya.id)


def test_twilio_sending_a_message_twice_is_handled_once(twilio, priya, session):
    send(twilio, sid(1))
    send(twilio, sid(1))
    messages, media = logged(session)
    assert len(messages) == 1 and len(media) == 1 and twilio.downloads == 1


def test_unknown_senders_are_logged_but_nothing_is_downloaded(twilio, users, session):
    send(twilio, sid(2), sender="whatsapp:+14155550100")
    (message,), media = logged(session)
    assert (message.user_id, message.kind, message.media_count, media) == (None, "ignored", 1, [])
    assert twilio.downloads == 0 and session.query(Invoice).count() == 0


def test_a_failed_download_is_tried_again_until_it_works(twilio, priya, session):
    twilio.mode = "busy"
    send(twilio, sid(3))
    _, (media,) = logged(session)
    assert (media.status, media.attempts, media.reason) == ("failed", 1, "HTTP 503")
    first_try = media.next_try_at

    assert whatsapp.retry_due(first_try - timedelta(seconds=1)) == 0        # not due yet
    twilio.mode = "ok"
    assert whatsapp.retry_due(first_try + timedelta(seconds=1)) == 1
    _, (media,) = logged(session)
    assert (media.status, media.attempts, media.next_try_at) == ("ingested", 2, None) and media.invoice_id


def test_after_max_attempts_it_gives_up(twilio, priya, session):
    twilio.mode = "busy"
    send(twilio, sid(4))
    for _ in range(whatsapp.MAX_ATTEMPTS - 1):
        _, (media,) = logged(session)
        whatsapp.retry_due(media.next_try_at + timedelta(seconds=1))
    _, (media,) = logged(session)
    assert (media.status, media.attempts, media.next_try_at) == ("gave_up", whatsapp.MAX_ATTEMPTS, None)
    assert whatsapp.retry_due(whatsapp._now() + timedelta(days=1)) == 0


def test_an_unusable_file_is_rejected_and_not_tried_again(twilio, priya, session):
    send(twilio, sid(5), content_type="image/gif")
    _, (media,) = logged(session)
    assert (media.status, media.reason) == ("rejected", "file type image/gif is not supported")
    assert whatsapp.retry_due(whatsapp._now() + timedelta(days=1)) == 0 and twilio.downloads == 0


def test_a_file_left_pending_by_a_stopped_process_is_picked_up(twilio, priya, session):
    whatsapp.record_message(sid(6), f"whatsapp:{NUMBER}", priya.id, "files", 0,
                            [(media_url(sid(6)), "application/pdf")], 1)
    assert whatsapp.retry_due() == 0                                        # just arrived: still being handled
    later = whatsapp._now() + timedelta(minutes=whatsapp.STALE_PENDING_MINUTES + 1)
    assert whatsapp.retry_due(later) == 1
    _, (media,) = logged(session)
    assert media.status == "ingested"


def test_the_stored_number_wins_over_whatsapp_senders(twilio, priya, users, session, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_senders", f"{NUMBER}:{users['ravi'].id}")
    send(twilio, sid(7), media=False)
    (message,), _ = logged(session)
    assert (message.user_id, message.kind) == (priya.id, "text")


# ---------- the API

def test_setting_a_whatsapp_number(client, users, session):
    uid = users["ravi"].id
    r = client.put(f"/api/v1/users/{uid}/whatsapp", json={"number": "whatsapp:+91 98765-43210"})
    assert r.status_code == 200 and r.json()["whatsapp"] == "+91******3210"
    session.expire_all()
    assert session.get(User, uid).whatsapp_number == NUMBER
    assert client.put(f"/api/v1/users/{users['priya'].id}/whatsapp", json={"number": NUMBER}).status_code == 409
    assert client.put(f"/api/v1/users/{uid}/whatsapp", json={"number": "call me maybe"}).status_code == 422
    assert client.delete(f"/api/v1/users/{uid}/whatsapp").json()["whatsapp"] is None


def test_the_message_list_and_a_retry_by_hand(client, twilio, priya, session):
    send(twilio, sid(8), content_type="image/gif")
    messages = client.get("/api/v1/whatsapp/messages").json()
    assert messages[0]["sender"] == "whatsapp:+91******3210" and NUMBER not in str(messages)
    media = messages[0]["media"][0]
    assert (media["status"], media["reason"]) == ("rejected", "file type image/gif is not supported")
    r = client.post(f"/api/v1/whatsapp/media/{media['id']}/retry")
    assert r.status_code == 200 and r.json()["status"] == "failed"         # queued for the next retry check
    assert client.get(f"/api/v1/whatsapp/messages?user_id={priya.id + 100}").json() == []
    assert client.post("/api/v1/whatsapp/media/999999/retry").status_code == 404
