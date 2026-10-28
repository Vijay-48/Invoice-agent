"""Incoming WhatsApp webhook (Twilio), with no database and no network.

The conftest.py autouse fixtures (_schema, _clean) drop and empty the test database.
They are overridden here with no-ops, and any attempt to open a database connection
or a network socket fails the test. Requests are signed with a fake token; the real
TWILIO_AUTH_TOKEN and TWILIO_ACCOUNT_SID (if any) are replaced in memory and never used.
Media downloads go to a fake Twilio (httpx.MockTransport), never to the internet.
ingest_document() is replaced by fake_ingest (same contract, in memory); the tests that
run the real ingest_document() give it FakeSession, not a database.
"""
import base64
import hashlib
import io
import logging
import socket
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from twilio.request_validator import RequestValidator

from app import whatsapp
from app.config import settings
from app.ingest import ingest_document
from app.models import AuditLog, Invoice, Status
from app.pipeline import extract
from tests.conftest import make_extracted

# SAMPLE_PDF and FakeAgent: copied from the source project's tests/test_extract_preview.py
# (that module tests app/extract_preview.py, which this project doesn't have)
import pymupdf

SAMPLE_TEXT = """TAX INVOICE
Sri Lakshmi Steels Pvt Ltd   GSTIN 36AAACS1234A1Z3
Invoice No: SLS/26-27/118    Date: 15-09-2026
MS Pipe 25mm   HSN 7306   10 x 450.00 = 4,500.00   GST 18%
GI Elbow 25mm  HSN 7307   20 x 35.00  =   700.00   GST 18%
Taxable 5,200.00  CGST 468.00  SGST 468.00  Total 6,136.00"""


def pdf_bytes(text: str | None = SAMPLE_TEXT, **save) -> bytes:
    """A real one-page PDF: with a text layer, or (text=None) only a drawing, like a scan."""
    doc = pymupdf.open()
    page = doc.new_page()
    if text:
        page.insert_text((40, 60), text, fontsize=9)
    else:
        page.draw_rect(pymupdf.Rect(40, 40, 300, 200), fill=(0.5, 0.5, 0.5))
    data = doc.tobytes(**save)
    doc.close()
    return data


SAMPLE_PDF = pdf_bytes()


class FakeAgent:
    """Stands in for the Agno agent: returns `answer` (an ExtractedInvoice, or text the way Agno
    reports failures) or raises it, and records each prompt and page image."""

    def __init__(self, answer, model="fake-model"):
        self.model, self.answer, self.calls = SimpleNamespace(id=model), answer, []

    def run(self, prompt, images=None):
        files = [Path(i.filepath) for i in images or []]
        self.calls.append({"prompt": prompt, "images": files, "images_existed": all(f.exists() for f in files)})
        if isinstance(self.answer, Exception):
            raise self.answer
        return SimpleNamespace(content=self.answer, metrics=None)

PATH = "/api/v1/webhook/whatsapp"
BASE_URL = "https://example.test"
PUBLIC_URL = BASE_URL + PATH
TOKEN = "test-token-not-real"
SENDER = "whatsapp:+919876543210"
ACCOUNT_SID = "AC" + "a" * 32
MESSAGE_SID = "MM" + "b" * 32
MEDIA_URL = f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Messages/{MESSAGE_SID}/Media/ME{'c' * 32}"
CDN_URL = "https://media.twiliocdn.com/file?Expires=1&Signature=signed-link-secret"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Overrides conftest: no DROP SCHEMA / create_all."""
    yield


@pytest.fixture(autouse=True)
def _clean():
    """Overrides conftest: no TRUNCATE."""
    yield


@pytest.fixture(autouse=True)
def _no_db_no_network(monkeypatch):
    def refuse(*a, **kw):
        raise AssertionError("the WhatsApp webhook must not open database or network connections")

    real_connect = socket.socket.connect

    def local_only(sock, address):
        # asyncio on Windows connects a socket pair over loopback; anything else is a download attempt
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return real_connect(sock, address)
        refuse()

    monkeypatch.setattr(Engine, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", local_only)


@pytest.fixture(autouse=True)
def _fake_twilio(monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", TOKEN)
    monkeypatch.setattr(settings, "public_base_url", BASE_URL + "/")   # trailing slash is tolerated
    monkeypatch.setattr(settings, "whatsapp_senders", "")    # the real WHATSAPP_SENDERS is never used
    monkeypatch.setattr(settings, "twilio_account_sid", ACCOUNT_SID)


@pytest.fixture(autouse=True)
def fake_ai(monkeypatch):
    """No OpenAI. The webhook itself never reads invoices (the worker does), so calls stay empty."""
    agent = FakeAgent(make_extracted())
    monkeypatch.setattr(extract, "extractor", agent)
    monkeypatch.setattr(extract, "scan_extractor", agent)
    return agent


@pytest.fixture(autouse=True)
def fake_ingest(monkeypatch):
    """Stands in for ingest_document() with its contract and no database: the same file (by
    SHA-256) returns the invoice made the first time, with created=False. Every call is recorded;
    a file name in .fail makes that call raise, like a database error."""
    state = SimpleNamespace(calls=[], by_hash={}, fail=set())

    def fake(session, content, filename, source, user_id, *, source_ref=None, sender=None, email_subject=None):
        state.calls.append(dict(content=content, filename=filename, source=source, user_id=user_id,
                                source_ref=source_ref, sender=sender, email_subject=email_subject))
        if filename in state.fail:
            raise RuntimeError("database unavailable")
        file_hash = hashlib.sha256(content).hexdigest()
        if file_hash in state.by_hash:
            return state.by_hash[file_hash], False
        state.by_hash[file_hash] = SimpleNamespace(id=101 + len(state.by_hash))
        return state.by_hash[file_hash], True

    monkeypatch.setattr(whatsapp, "ingest_document", fake)
    return state


@pytest.fixture
def fake_users(monkeypatch):
    """Replaces the database lookup: the ids in the returned set exist; every lookup is recorded."""
    existing, looked_up = {1}, []

    def fake_user_exists(user_id):
        looked_up.append(user_id)
        return user_id in existing

    monkeypatch.setattr(whatsapp, "user_exists", fake_user_exists)
    return existing, looked_up


@pytest.fixture
def client():
    # Only the WhatsApp router: the real app's lifespan would create tables and start the worker
    app = FastAPI()
    app.include_router(whatsapp.router)
    with TestClient(app) as c:
        yield c


def sign(data: dict, token: str = TOKEN, url: str = PUBLIC_URL) -> str:
    return RequestValidator(token).compute_signature(url, data)


def post(client, data: dict, signature: str | None = None):
    """POST like Twilio does: form data plus X-Twilio-Signature (correct unless given)."""
    headers = {"X-Twilio-Signature": sign(data) if signature is None else signature}
    return client.post(PATH, data=data, headers=headers)


def assert_empty_twiml(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(response.text)
    assert root.tag == "Response" and len(root) == 0 and not (root.text or "").strip()


# ---------- signed requests (the Step 1 behaviour)

def test_text_message(client, caplog):
    caplog.set_level(logging.INFO, logger="whatsapp")
    r = post(client, {"From": SENDER, "Body": "Invoice INV-42 for Rs 6,136", "MessageSid": "SM123",
                      "NumMedia": "0", "To": "whatsapp:+14155238886"})
    assert_empty_twiml(r)
    assert "SM123" in caplog.text and "0 media" in caplog.text
    assert "whatsapp:+91******3210" in caplog.text
    assert "9876543210" not in caplog.text          # sender masked
    assert "INV-42" not in caplog.text              # message text never logged


def test_media_is_not_downloaded(client, caplog):
    caplog.set_level(logging.INFO, logger="whatsapp")
    r = post(client, {"From": SENDER, "MessageSid": "SM456", "NumMedia": "1",
                      "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages/SM456/Media/ME1",
                      "MediaContentType0": "application/pdf"})
    assert_empty_twiml(r)     # a download attempt would have raised in _no_db_no_network
    assert "SM456" in caplog.text and "1 media" in caplog.text
    assert "api.twilio.com" not in caplog.text


def test_defaults_when_body_and_num_media_missing(client):
    assert_empty_twiml(post(client, {"From": SENDER, "MessageSid": "SM789"}))


@pytest.mark.parametrize("missing", ["From", "MessageSid"])
def test_missing_required_field(client, missing):
    """Signed correctly, so it gets past the signature check and fails field validation."""
    data = {"From": SENDER, "MessageSid": "SM999", "Body": "hi"}
    del data[missing]
    assert post(client, data).status_code == 422


def test_mask_sender():
    assert whatsapp.mask_sender("whatsapp:+919876543210") == "whatsapp:+91******3210"
    assert whatsapp.mask_sender("+14155238886") == "+14*****8886"
    assert whatsapp.mask_sender("whatsapp:+123") == "whatsapp:****"


# ---------- sender -> user mapping (WHATSAPP_SENDERS)

def test_normalize_number():
    assert whatsapp.normalize_number("whatsapp:+919876543210") == "+919876543210"
    assert whatsapp.normalize_number("  WhatsApp:+91 98765-43210 ") == "+919876543210"
    assert whatsapp.normalize_number("919876543210") == "+919876543210"
    for bad in ("", "whatsapp:", "+91abc43210", "+1234567", "+1234567890123456"):
        assert whatsapp.normalize_number(bad) is None


def test_parse_senders():
    assert whatsapp.parse_senders("") == {}
    assert whatsapp.parse_senders(" +91 98765 43210 : 1 , whatsapp:+14155550100:2,") == {
        "+919876543210": 1, "+14155550100": 2}
    assert whatsapp.parse_senders("+919876543210:1,+919876543210:1") == {"+919876543210": 1}


def message_from(sender: str, sid: str = "SM777", **extra) -> dict:
    return {"From": sender, "MessageSid": sid, "Body": "Invoice INV-42 for Rs 6,136", "NumMedia": "0", **extra}


def assert_nothing_private_logged(caplog):
    assert "9876543210" not in caplog.text          # sender masked
    assert "INV-42" not in caplog.text              # message text never logged


@pytest.mark.parametrize("configured", [
    "+919876543210:1",
    " whatsapp:+91 98765 43210 : 1 ",               # prefix and spaces in the setting
    "919876543210:1",                               # no leading +
    "+14155550100:2, +919876543210:1",              # one of several
    "not-a-number:5,+919876543210:1",               # a bad entry elsewhere doesn't break this one
])
def test_known_sender_mapped(client, caplog, monkeypatch, fake_users, configured):
    caplog.set_level(logging.INFO, logger="whatsapp")
    monkeypatch.setattr(settings, "whatsapp_senders", configured)
    assert_empty_twiml(post(client, message_from(SENDER)))
    assert fake_users[1] == [1]
    assert "SM777 from whatsapp:+91******3210 mapped to user 1" in caplog.text
    assert "ignored" not in caplog.text
    assert_nothing_private_logged(caplog)


@pytest.mark.parametrize("configured", ["", "+14155550100:1"])
def test_unknown_sender_ignored(client, caplog, monkeypatch, fake_users, configured):
    caplog.set_level(logging.INFO, logger="whatsapp")
    monkeypatch.setattr(settings, "whatsapp_senders", configured)
    assert_empty_twiml(post(client, message_from(SENDER, NumMedia="1")))
    assert fake_users[1] == []                      # no user lookup for an unknown number
    assert "SM777 from whatsapp:+91******3210 ignored: sender not in WHATSAPP_SENDERS" in caplog.text
    assert "mapped to user" not in caplog.text and "media ignored" not in caplog.text
    assert_nothing_private_logged(caplog)


@pytest.mark.parametrize("configured", [
    "+919876543210:abc",
    "+919876543210:0",
    "+919876543210:-1",
    "+919876543210:",
    "+919876543210:1.5",
    "+919876543210:1,+919876543210:2",              # same number, two users
], ids=["text", "zero", "negative", "empty", "decimal", "two-users"])
def test_invalid_mapping_ignored(client, caplog, monkeypatch, fake_users, configured):
    caplog.set_level(logging.INFO, logger="whatsapp")
    monkeypatch.setattr(settings, "whatsapp_senders", configured)
    assert_empty_twiml(post(client, message_from(SENDER)))
    assert fake_users[1] == []
    assert "ignored: its WHATSAPP_SENDERS entry is invalid" in caplog.text
    assert "mapped to user" not in caplog.text
    assert_nothing_private_logged(caplog)


def test_missing_user_ignored(client, caplog, monkeypatch, fake_users):
    caplog.set_level(logging.INFO, logger="whatsapp")
    monkeypatch.setattr(settings, "whatsapp_senders", "+919876543210:7")
    assert_empty_twiml(post(client, message_from(SENDER)))
    assert fake_users[1] == [7]
    assert "ignored: mapped user 7 does not exist" in caplog.text
    assert "mapped to user 7" not in caplog.text
    assert_nothing_private_logged(caplog)


def test_bad_entries_logged_without_number(caplog):
    caplog.set_level(logging.INFO, logger="whatsapp")
    whatsapp.parse_senders("+919876543210, +919876543210:x")
    assert "entry 1 is not 'number:user_id'" in caplog.text
    assert "entry 2 has an invalid user id" in caplog.text
    assert "9876543210" not in caplog.text


def test_unsigned_request_from_known_sender_rejected_before_lookup(client, caplog, monkeypatch, fake_users):
    caplog.set_level(logging.INFO, logger="whatsapp")
    monkeypatch.setattr(settings, "whatsapp_senders", "+919876543210:1")
    assert client.post(PATH, data=message_from(SENDER)).status_code == 403
    assert fake_users[1] == []


# ---------- media download (known senders only)

PDF = b"%PDF-1.7\n" + b"0" * 200


def office_file(part: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(part, "<x/>")
    return buf.getvalue()


@pytest.fixture
def twilio_http(monkeypatch):
    """A fake Twilio API and file storage instead of the internet. state["responses"] maps a URL
    to a function making its response (unknown URLs get 404); every request is recorded."""
    state = {"requests": [], "responses": {
        MEDIA_URL: lambda: httpx.Response(307, headers={"Location": CDN_URL}),
        CDN_URL: lambda: httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"}),
    }}

    def handler(request):
        state["requests"].append(request)
        make = state["responses"].get(str(request.url))
        return make() if make else httpx.Response(404)

    monkeypatch.setattr(whatsapp, "media_client", lambda: httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False))
    return state


@pytest.fixture
def known_sender(monkeypatch, fake_users):
    monkeypatch.setattr(settings, "whatsapp_senders", "+919876543210:1")


def media_message(**extra) -> dict:
    return {"From": SENDER, "MessageSid": MESSAGE_SID, "AccountSid": ACCOUNT_SID, "Body": "Invoice INV-42",
            "NumMedia": "1", "MediaUrl0": MEDIA_URL, "MediaContentType0": "application/pdf", **extra}


def assert_no_secrets_logged(caplog):
    assert_nothing_private_logged(caplog)
    for secret in (TOKEN, "api.twilio.com", "twiliocdn.com/file", "signed-link-secret", MEDIA_URL, "Basic "):
        assert secret not in caplog.text


def basic_auth() -> str:
    return "Basic " + base64.b64encode(f"{ACCOUNT_SID}:{TOKEN}".encode()).decode()


def test_media_downloaded_with_credentials_only_for_twilio(client, caplog, known_sender, twilio_http):
    caplog.set_level(logging.INFO, logger="whatsapp")
    assert_empty_twiml(post(client, media_message()))
    first, second = twilio_http["requests"]
    assert str(first.url) == MEDIA_URL and first.headers["Authorization"] == basic_auth()
    assert str(second.url) == CDN_URL and "Authorization" not in second.headers
    assert f"{MESSAGE_SID} media 0 downloaded and checked: pdf, {len(PDF)} bytes" in caplog.text
    assert_no_secrets_logged(caplog)


def test_media_served_without_redirect(client, caplog, known_sender, twilio_http):
    caplog.set_level(logging.INFO, logger="whatsapp")
    twilio_http["responses"][MEDIA_URL] = lambda: httpx.Response(200, content=PDF)
    assert_empty_twiml(post(client, media_message()))
    assert len(twilio_http["requests"]) == 1
    assert "media 0 downloaded and checked: pdf" in caplog.text


@pytest.mark.parametrize("mime, content, served_as", [
    ("application/pdf", PDF, "application/pdf"),
    ("image/jpeg", b"\xff\xd8\xff\xe0" + b"0" * 100, "image/jpeg"),
    ("image/png", b"\x89PNG\r\n\x1a\n" + b"0" * 100, "image/png"),
    ("image/webp", b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 100, "image/webp"),
    ("text/csv", b"Invoice,Amount\nINV-1,100\n", "text/csv; charset=utf-8"),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", office_file("xl/workbook.xml"),
     "application/octet-stream"),
    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", office_file("word/document.xml"),
     "binary/octet-stream"),
], ids=["pdf", "jpeg", "png", "webp", "csv", "xlsx", "docx"])
def test_supported_types(client, caplog, known_sender, twilio_http, mime, content, served_as):
    caplog.set_level(logging.INFO, logger="whatsapp")
    twilio_http["responses"][CDN_URL] = lambda: httpx.Response(200, content=content,
                                                               headers={"Content-Type": served_as})
    assert_empty_twiml(post(client, media_message(MediaContentType0=mime)))
    ext = whatsapp.MEDIA_TYPES[mime].lstrip(".")
    assert f"media 0 downloaded and checked: {ext}, {len(content)} bytes" in caplog.text
    assert "rejected" not in caplog.text


def test_unknown_sender_media_not_downloaded(client, fake_users, twilio_http):
    assert_empty_twiml(post(client, media_message()))
    assert twilio_http["requests"] == []


def test_unsigned_request_media_not_downloaded(client, known_sender, twilio_http):
    assert client.post(PATH, data=media_message()).status_code == 403
    assert twilio_http["requests"] == []


def test_not_configured_no_download(client, caplog, monkeypatch, known_sender, twilio_http):
    caplog.set_level(logging.INFO, logger="whatsapp")
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    assert_empty_twiml(post(client, media_message()))
    assert twilio_http["requests"] == []
    assert "rejected: TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is not set" in caplog.text


OTHER_ACCOUNT = "AC" + "d" * 32
OTHER_MESSAGE = "MM" + "e" * 32


@pytest.mark.parametrize("fields, reason", [
    ({"AccountSid": OTHER_ACCOUNT}, "AccountSid is not this Twilio account"),
    ({"AccountSid": ""}, "AccountSid is not this Twilio account"),
    ({"MessageSid": "SM123"}, "MessageSid is not a Twilio message SID"),
    ({"MediaContentType0": "application/zip"}, "file type application/zip is not supported"),
    ({"MediaContentType0": "message/rfc822"}, "file type message/rfc822 is not supported"),
    ({"MediaContentType0": "text/html"}, "file type text/html is not supported"),
    ({"MediaContentType0": ""}, "file type unknown is not supported"),
    ({"MediaUrl0": MEDIA_URL.replace("https://", "http://")}, "not a Twilio media URL"),
    ({"MediaUrl0": MEDIA_URL.replace("api.twilio.com", "evil.test")}, "not a Twilio media URL"),
    ({"MediaUrl0": MEDIA_URL.replace("api.twilio.com", "api.twilio.com.evil.test")}, "not a Twilio media URL"),
    ({"MediaUrl0": MEDIA_URL.replace("api.twilio.com", "user:pw@api.twilio.com")}, "not a Twilio media URL"),
    ({"MediaUrl0": MEDIA_URL.replace("api.twilio.com", "api.twilio.com:8443")}, "not a Twilio media URL"),
    ({"MediaUrl0": MEDIA_URL + "?redirect=https://evil.test"}, "not a Twilio media URL"),
    ({"MediaUrl0": MEDIA_URL.replace("/Media/", "/../../Media/")}, "not a Twilio media URL"),
    ({"MediaUrl0": "not a url at all"}, "not a Twilio media URL"),
    ({"MediaUrl0": MEDIA_URL.replace(ACCOUNT_SID, OTHER_ACCOUNT)}, "media URL is for another account or message"),
    ({"MediaUrl0": MEDIA_URL.replace(MESSAGE_SID, OTHER_MESSAGE)}, "media URL is for another account or message"),
], ids=["other-account", "no-account", "bad-message-sid", "zip", "eml", "html", "no-type", "http", "other-host",
        "lookalike-host", "userinfo", "port", "query", "dot-segments", "garbage", "url-other-account",
        "url-other-message"])
def test_rejected_before_any_request(client, caplog, known_sender, twilio_http, fields, reason):
    caplog.set_level(logging.INFO, logger="whatsapp")
    assert_empty_twiml(post(client, media_message(**fields)))
    assert twilio_http["requests"] == []
    assert f"media 0 rejected: {reason}" in caplog.text
    assert_no_secrets_logged(caplog)


@pytest.mark.parametrize("location, host", [
    ("https://evil.test/file", "evil.test"),
    ("https://media.twiliocdn.com.evil.test/file", "media.twiliocdn.com.evil.test"),
    ("https://notamazonaws.com/file", "notamazonaws.com"),
    ("http://media.twiliocdn.com/file", "media.twiliocdn.com"),         # not HTTPS
    ("https://media.twiliocdn.com:8443/file", "media.twiliocdn.com"),
    ("https://user:pw@media.twiliocdn.com/file", "media.twiliocdn.com"),
    ("https://169.254.169.254/latest/meta-data", "169.254.169.254"),
])
def test_redirect_to_other_host_not_followed(client, caplog, known_sender, twilio_http, location, host):
    caplog.set_level(logging.INFO, logger="whatsapp")
    twilio_http["responses"][MEDIA_URL] = lambda: httpx.Response(302, headers={"Location": location})
    assert_empty_twiml(post(client, media_message()))
    assert len(twilio_http["requests"]) == 1                   # only api.twilio.com was called
    assert f"media 0 rejected: redirect to a host that is not allowed ({host})" in caplog.text
    assert_no_secrets_logged(caplog)


def test_credentials_never_sent_after_redirects(client, caplog, known_sender, twilio_http):
    caplog.set_level(logging.INFO, logger="whatsapp")
    s3 = "https://s3-external-1.amazonaws.com/media/file"
    twilio_http["responses"][CDN_URL] = lambda: httpx.Response(301, headers={"Location": s3})
    twilio_http["responses"][s3] = lambda: httpx.Response(200, content=PDF)
    assert_empty_twiml(post(client, media_message()))
    first, *others = twilio_http["requests"]
    assert first.headers["Authorization"] == basic_auth()
    assert len(others) == 2 and all("Authorization" not in r.headers for r in others)
    assert "media 0 downloaded and checked: pdf" in caplog.text


def test_too_many_redirects(client, caplog, known_sender, twilio_http):
    caplog.set_level(logging.INFO, logger="whatsapp")
    twilio_http["responses"][CDN_URL] = lambda: httpx.Response(302, headers={"Location": CDN_URL})
    assert_empty_twiml(post(client, media_message()))
    assert len(twilio_http["requests"]) == whatsapp.MAX_REDIRECTS + 1
    assert "media 0 rejected: too many redirects" in caplog.text


@pytest.mark.parametrize("response, reason", [
    (lambda: httpx.Response(404), "HTTP 404"),
    (lambda: httpx.Response(401), "HTTP 401"),
    (lambda: httpx.Response(302, headers={"Location": " "}), "redirect without a location"),
    (lambda: httpx.Response(200, content=b""), "the file is empty"),
    (lambda: httpx.Response(200, content=b"\x89PNG\r\n\x1a\n0000"), "content is not a .pdf file"),
    (lambda: httpx.Response(200, content=b"<html>login</html>", headers={"Content-Type": "text/html"}),
     "served as text/html, announced as application/pdf"),
], ids=["404", "401", "empty-location", "empty-file", "png-as-pdf", "html"])
def test_bad_response_rejected(client, caplog, known_sender, twilio_http, response, reason):
    caplog.set_level(logging.INFO, logger="whatsapp")
    twilio_http["responses"][MEDIA_URL] = response
    assert_empty_twiml(post(client, media_message()))
    assert f"media 0 rejected: {reason}" in caplog.text
    assert_no_secrets_logged(caplog)


@pytest.mark.parametrize("ext, content", [
    (".xlsx", office_file("word/document.xml")),       # a Word file announced as Excel
    (".docx", PDF),
    (".csv", b"a,b\x00\x01"),
    (".jpg", PDF),
    (".webp", b"RIFF\x00\x00\x00\x00WAVE"),
])
def test_content_mismatch(ext, content):
    assert not whatsapp.content_matches(ext, content)


@pytest.mark.parametrize("with_length", [True, False], ids=["content-length", "streamed"])
def test_too_large(client, caplog, monkeypatch, known_sender, twilio_http, with_length):
    caplog.set_level(logging.INFO, logger="whatsapp")
    monkeypatch.setattr(settings, "max_upload_mb", 1)
    big = PDF + b"0" * (1024 * 1024)
    if with_length:
        make = lambda: httpx.Response(200, content=big)   # noqa: E731
    else:   # no Content-Length: the size is counted while reading
        make = lambda: httpx.Response(200, content=iter([big[i:i + 65536] for i in range(0, len(big), 65536)]))  # noqa: E731
    twilio_http["responses"][CDN_URL] = make
    assert_empty_twiml(post(client, media_message()))
    assert "media 0 rejected: larger than 1 MB" in caplog.text


def test_timeout_logged_without_url(client, caplog, known_sender, twilio_http):
    caplog.set_level(logging.INFO, logger="whatsapp")

    def timeout():
        raise httpx.ReadTimeout(f"timed out reading {CDN_URL}")

    twilio_http["responses"][CDN_URL] = timeout
    assert_empty_twiml(post(client, media_message()))
    assert "media 0 download failed: ReadTimeout" in caplog.text
    assert_no_secrets_logged(caplog)


def test_unexpected_error_still_answers(client, caplog, known_sender, twilio_http):
    caplog.set_level(logging.INFO, logger="whatsapp")

    def boom():
        raise RuntimeError(f"broken {CDN_URL}")

    twilio_http["responses"][CDN_URL] = boom
    assert_empty_twiml(post(client, media_message()))
    assert "media 0: unexpected RuntimeError" in caplog.text
    assert_no_secrets_logged(caplog)


def test_several_media_checked_one_by_one(client, caplog, known_sender, twilio_http):
    caplog.set_level(logging.INFO, logger="whatsapp")
    second_url = MEDIA_URL[:-32] + "f" * 32
    twilio_http["responses"][second_url] = lambda: httpx.Response(200, content=b"PK\x03\x04 not really")
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert_empty_twiml(post(client, media_message(NumMedia="2", MediaUrl1=second_url, MediaContentType1=docx)))
    assert "media 0 downloaded and checked: pdf" in caplog.text
    assert "media 1 rejected: content is not a .docx file" in caplog.text


# ---------- ingest: checked attachments go to ingest_document()

MEDIA_SID = "ME" + "c" * 32
MASKED_SENDER = "whatsapp:+91******3210"
JPG = b"\xff\xd8\xff\xe0" + b"1" * 100


def media_url(media_sid: str) -> str:
    return MEDIA_URL.replace(MEDIA_SID, media_sid)


def test_checked_attachment_ingested_once(client, caplog, known_sender, twilio_http, fake_ingest, fake_ai):
    caplog.set_level(logging.INFO, logger="whatsapp")
    assert_empty_twiml(post(client, media_message()))
    (call,) = fake_ingest.calls
    assert call == dict(content=PDF, filename=f"whatsapp-{MESSAGE_SID}-0.pdf", source="whatsapp", user_id=1,
                        source_ref=f"{MESSAGE_SID}/{MEDIA_SID}", sender=MASKED_SENDER, email_subject=None)
    assert len(twilio_http["requests"]) == 2            # downloaded once (Twilio + its redirect), not again
    assert f"{MESSAGE_SID} media 0 -> invoice 101, queued for processing" in caplog.text
    assert fake_ai.calls == []                          # the worker reads it, not the webhook
    assert_no_secrets_logged(caplog)


def test_user_id_comes_from_the_mapping(client, monkeypatch, fake_users, twilio_http, fake_ingest):
    fake_users[0].add(5)
    monkeypatch.setattr(settings, "whatsapp_senders", "+14155550100:1,+919876543210:5")
    post(client, media_message())
    assert [c["user_id"] for c in fake_ingest.calls] == [5]


def test_image_ingested(client, known_sender, twilio_http, fake_ingest):
    twilio_http["responses"][CDN_URL] = lambda: httpx.Response(200, content=JPG, headers={"Content-Type": "image/jpeg"})
    assert_empty_twiml(post(client, media_message(MediaContentType0="image/jpeg")))
    assert [(c["filename"], c["content"]) for c in fake_ingest.calls] == [(f"whatsapp-{MESSAGE_SID}-0.jpg", JPG)]


def test_never_ingested_without_valid_signature(client, known_sender, twilio_http, fake_ingest):
    assert client.post(PATH, data=media_message()).status_code == 403
    assert post(client, media_message(), signature=sign(media_message(), token="other")).status_code == 403
    assert fake_ingest.calls == [] and twilio_http["requests"] == []


@pytest.mark.parametrize("configured", ["", "+14155550100:1", "+919876543210:abc", "+919876543210:7"],
                         ids=["no-senders", "other-number", "invalid-mapping", "missing-user"])
def test_never_ingested_for_unmapped_sender(client, monkeypatch, fake_users, twilio_http, fake_ingest, configured):
    monkeypatch.setattr(settings, "whatsapp_senders", configured)
    assert_empty_twiml(post(client, media_message()))
    assert fake_ingest.calls == [] and twilio_http["requests"] == []


def _raise_timeout():
    raise httpx.ConnectTimeout("timed out")


@pytest.mark.parametrize("fields, cdn_response", [
    ({"AccountSid": "AC" + "d" * 32}, None),
    ({"MessageSid": "SM123"}, None),
    ({"MediaContentType0": "application/zip"}, None),
    ({"MediaUrl0": MEDIA_URL.replace("api.twilio.com", "evil.test")}, None),
    ({}, lambda: httpx.Response(200, content=b"<html>login</html>", headers={"Content-Type": "text/html"})),
    ({}, lambda: httpx.Response(200, content=JPG)),                       # announced as PDF
    ({}, lambda: httpx.Response(404)),
    ({}, _raise_timeout),
], ids=["other-account", "bad-message-sid", "zip", "other-host", "served-as-html", "content-mismatch",
        "http-404", "timeout"])
def test_never_ingested_when_attachment_fails(client, known_sender, twilio_http, fake_ingest, fields, cdn_response):
    if cdn_response:
        twilio_http["responses"][CDN_URL] = cdn_response
    assert_empty_twiml(post(client, media_message(**fields)))
    assert fake_ingest.calls == []


def test_twilio_retry_is_the_same_file(client, caplog, known_sender, twilio_http, fake_ingest):
    """Twilio sending the same message twice: ingest_document()'s file hash keeps one invoice."""
    caplog.set_level(logging.INFO, logger="whatsapp")
    post(client, media_message())
    post(client, media_message())
    assert len(fake_ingest.calls) == 2 and len(fake_ingest.by_hash) == 1
    assert "media 0 -> invoice 101, queued for processing" in caplog.text
    assert "media 0 is the same file as invoice 101; not queued again" in caplog.text


def test_same_file_twice_in_one_message(client, caplog, known_sender, twilio_http, fake_ingest):
    caplog.set_level(logging.INFO, logger="whatsapp")
    second = media_url("ME" + "f" * 32)
    twilio_http["responses"][second] = lambda: httpx.Response(200, content=PDF)
    post(client, media_message(NumMedia="2", MediaUrl1=second, MediaContentType1="application/pdf"))
    assert [c["source_ref"] for c in fake_ingest.calls] == [f"{MESSAGE_SID}/{MEDIA_SID}", f"{MESSAGE_SID}/ME{'f' * 32}"]
    assert "media 0 -> invoice 101" in caplog.text and "media 1 is the same file as invoice 101" in caplog.text


def test_one_failed_ingest_does_not_block_the_next(client, caplog, known_sender, twilio_http, fake_ingest):
    caplog.set_level(logging.INFO, logger="whatsapp")
    second = media_url("ME" + "f" * 32)
    twilio_http["responses"][second] = lambda: httpx.Response(200, content=JPG)
    fake_ingest.fail.add(f"whatsapp-{MESSAGE_SID}-0.pdf")
    assert_empty_twiml(post(client, media_message(NumMedia="2", MediaUrl1=second, MediaContentType1="image/jpeg")))
    assert len(fake_ingest.calls) == 2
    assert "media 0 not ingested: RuntimeError" in caplog.text
    assert "media 1 -> invoice 101, queued for processing" in caplog.text
    assert "database unavailable" not in caplog.text    # the class only, never the message


def test_rejected_attachment_does_not_block_the_next(client, caplog, known_sender, twilio_http, fake_ingest):
    caplog.set_level(logging.INFO, logger="whatsapp")
    second = media_url("ME" + "f" * 32)
    twilio_http["responses"][second] = lambda: httpx.Response(200, content=JPG)
    post(client, media_message(NumMedia="2", MediaContentType0="application/zip",
                               MediaUrl1=second, MediaContentType1="image/jpeg"))
    assert [c["filename"] for c in fake_ingest.calls] == [f"whatsapp-{MESSAGE_SID}-1.jpg"]
    assert "media 0 rejected" in caplog.text and "media 1 -> invoice 101" in caplog.text


class FakeSession:
    """Just enough of a SQLAlchemy session for the real ingest_document(), with no database.
    scalar() (the file-hash lookup) returns `existing`."""

    def __init__(self, existing=None):
        self.existing, self.added, self.commits, self.closed = existing, [], 0, False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def scalar(self, statement):
        return self.existing

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for obj in self.added:
            if isinstance(obj, Invoice) and obj.id is None:
                obj.id = 501

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


@pytest.fixture
def real_ingest(monkeypatch, tmp_path):
    """The real ingest_document() with FakeSession and a temporary STORAGE_DIR."""
    session = FakeSession()
    monkeypatch.setattr(whatsapp, "ingest_document", ingest_document)
    monkeypatch.setattr(whatsapp, "SessionLocal", lambda: session)
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    return session


def test_real_ingest_document_saves_and_queues(client, caplog, known_sender, twilio_http, real_ingest):
    caplog.set_level(logging.INFO, logger="whatsapp")
    caplog.set_level(logging.INFO, logger="ingest")
    twilio_http["responses"][CDN_URL] = lambda: httpx.Response(200, content=SAMPLE_PDF)
    assert_empty_twiml(post(client, media_message()))

    inv = next(o for o in real_ingest.added if isinstance(o, Invoice))
    assert (inv.id, inv.user_id, inv.source, inv.status) == (501, 1, "whatsapp", Status.RECEIVED)
    assert inv.source_ref == f"{MESSAGE_SID}/{MEDIA_SID}" and inv.sender == MASKED_SENDER
    assert inv.original_filename == f"whatsapp-{MESSAGE_SID}-0.pdf"
    assert inv.file_hash == hashlib.sha256(SAMPLE_PDF).hexdigest()
    assert Path(inv.file_path).read_bytes() == SAMPLE_PDF and Path(inv.file_path).suffix == ".pdf"
    (entry,) = [o for o in real_ingest.added if isinstance(o, AuditLog)]
    assert entry.message == "Received via whatsapp" and entry.data["sender"] == MASKED_SENDER
    assert real_ingest.commits == 1 and real_ingest.closed
    assert "media 0 -> invoice 501, queued for processing" in caplog.text
    assert_no_secrets_logged(caplog)


def test_real_ingest_document_same_file(client, caplog, known_sender, twilio_http, real_ingest, tmp_path):
    caplog.set_level(logging.INFO, logger="whatsapp")
    real_ingest.existing = SimpleNamespace(id=42)       # the hash lookup finds an earlier invoice
    assert_empty_twiml(post(client, media_message()))
    assert real_ingest.added == [] and real_ingest.commits == 0 and real_ingest.closed
    assert not (tmp_path / "storage").exists()         # the file is not stored a second time
    assert "media 0 is the same file as invoice 42; not queued again" in caplog.text


def test_media_client_settings():
    with whatsapp.media_client() as c:
        assert c.follow_redirects is False and c.trust_env is False
        assert c.timeout.connect == 5.0 and c.timeout.read == 10.0


# ---------- rejected requests

MESSAGE = {"From": SENDER, "Body": "Invoice INV-42 for Rs 6,136", "MessageSid": "SM321", "NumMedia": "0"}


def assert_rejected(response, caplog):
    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid Twilio signature"}
    # The rejection log never contains the token, a signature, the sender or the text
    for secret in (TOKEN, sign(MESSAGE), "9876543210", "INV-42", "SM321"):
        assert secret not in caplog.text


def test_missing_signature(client, caplog):
    caplog.set_level(logging.INFO, logger="whatsapp")
    r = client.post(PATH, data=MESSAGE)
    assert_rejected(r, caplog)
    assert "no X-Twilio-Signature" in caplog.text


@pytest.mark.parametrize("signature", [
    "",                                            # empty header
    "bm90LWEtcmVhbC1zaWduYXR1cmU=",                # made up
    sign(MESSAGE, token="some-other-token"),       # signed with another account's token
    sign(MESSAGE, url="https://evil.test" + PATH), # signed for another URL
    sign(MESSAGE, url="http://example.test" + PATH),  # http instead of https
], ids=["empty", "made-up", "wrong-token", "wrong-host", "wrong-scheme"])
def test_invalid_signature(client, caplog, signature):
    caplog.set_level(logging.INFO, logger="whatsapp")
    assert_rejected(post(client, MESSAGE, signature=signature), caplog)


def test_tampered_field(client, caplog):
    caplog.set_level(logging.INFO, logger="whatsapp")
    signature = sign(MESSAGE)
    assert_rejected(post(client, {**MESSAGE, "Body": "Invoice INV-42 for Rs 99,999"}, signature=signature), caplog)


def test_unsigned_request_rejected_before_field_validation(client, caplog):
    """An unsigned request with missing fields gets 403, not 422: nothing is parsed for it."""
    caplog.set_level(logging.INFO, logger="whatsapp")
    assert_rejected(client.post(PATH, data={"Body": "INV-42"}), caplog)


def test_no_token_configured_rejects_everything(client, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="whatsapp")
    signature = sign(MESSAGE)                      # even a request signed with the old token
    monkeypatch.setattr(settings, "twilio_auth_token", "")   # the default when TWILIO_AUTH_TOKEN is not set
    assert_rejected(post(client, MESSAGE, signature=signature), caplog)
    assert "TWILIO_AUTH_TOKEN is not set" in caplog.text
