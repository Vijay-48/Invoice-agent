# ruff: noqa: F811  (pytest fixtures imported from test_gmail)
"""Gmail connection health: expired/revoked access is noticed, shown and skipped (Composio faked)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import gmail, gmail_health, gmail_listener, reports
from app.api import app
from app.config import settings
from app.db import SessionLocal
from app.models import GmailHealth, User
from tests.test_gmail import fake   # noqa: F401 (fixture)


def test_expired_access_is_detected_and_cleared(users, fake, session):
    user = session.get(User, users["priya"].id)
    fake.statuses[user.gmail_account_id] = "EXPIRED"
    h = gmail_health.check(session, user)
    assert h.status == "reconnect" and "expired" in h.detail and h.problem_since is not None
    assert gmail_health.needs_reconnect(session, user)
    fake.statuses[user.gmail_account_id] = "ACTIVE"
    h = gmail_health.check(session, user)
    assert h.status == "ok" and h.problem_since is None


def test_offline_check_changes_nothing(users, fake, session, monkeypatch):
    user = session.get(User, users["priya"].id)

    def offline(account_id):
        raise gmail.NetworkError("no internet")
    monkeypatch.setattr(fake.connected_accounts, "get", offline)
    monkeypatch.setattr(gmail.time, "sleep", lambda s: None)   # skip the retry waits
    assert gmail_health.check(session, user) is None
    assert session.get(GmailHealth, user.id) is None


def test_broken_inbox_is_skipped_by_sync_and_shown(users, fake, session, monkeypatch):
    user = session.get(User, users["priya"].id)
    gmail_health.record(session, user, gmail_health.RECONNECT, "Gmail access is revoked: connect Gmail again")
    synced = []
    monkeypatch.setattr(gmail_listener, "sync_user", lambda s, u: synced.append(u.id))
    gmail_listener.sync_all()
    assert synced == []                                       # no failing sync every 5 minutes
    with TestClient(app) as c:
        me = next(u for u in c.get("/api/v1/users").json() if u["id"] == user.id)
        assert me["gmail"]["health"]["status"] == "reconnect"
        st = c.get("/api/v1/gmail/status").json()
        assert st["inboxes"][0]["health"]["status"] == "reconnect"
    end = datetime.now(timezone.utc)
    _, body = reports.render(reports.build(session, end - timedelta(days=1), end))
    assert "Reconnect Gmail" in body and "revoked" in body


def test_auth_error_during_sync_triggers_a_check(users, fake, session, monkeypatch):
    user = session.get(User, users["priya"].id)
    fake.statuses[user.gmail_account_id] = "REVOKED"

    def sync(s, u):
        raise RuntimeError("Error 401: invalid_grant (token has been expired or revoked)")
    monkeypatch.setattr(gmail_listener, "sync_user", sync)
    gmail_listener.sync_all()
    with SessionLocal() as s:
        assert s.get(GmailHealth, user.id).status == "reconnect"


def test_manual_sync_with_expired_access_says_reconnect(users, fake, monkeypatch):
    fake.statuses["ca_priya"] = "EXPIRED"

    def sync(*a):
        raise RuntimeError("401 Unauthorized")
    monkeypatch.setattr(gmail, "sync_user", sync)
    with TestClient(app) as c:
        r = c.post(f"/api/v1/users/{users['priya'].id}/gmail/sync")
        assert r.status_code == 409 and "Connect Gmail" in r.json()["detail"]
        r = c.post(f"/api/v1/users/{users['priya'].id}/gmail/check")
        assert r.status_code == 200 and r.json()["gmail"]["health"]["status"] == "reconnect"


def test_reconnecting_marks_the_inbox_healthy(users, fake, session):
    user = session.get(User, users["priya"].id)
    gmail_health.record(session, user, gmail_health.RECONNECT, "expired")
    fake.accounts = ["ca_new"]
    gmail.activate(session, user)
    assert session.get(GmailHealth, user.id).status == "ok"


def test_alert_email_sent_once_from_a_working_inbox(users, fake, session, monkeypatch):
    monkeypatch.setattr(settings, "gmail_health_alerts", True)
    ravi = session.get(User, users["ravi"].id)
    ravi.gmail_account_id = "ca_ravi"
    session.commit()
    sent = []
    monkeypatch.setattr(gmail, "send_email", lambda u, to, subject, body: sent.append((u.email, to, subject)))
    priya = session.get(User, users["priya"].id)
    fake.statuses["ca_priya"] = "EXPIRED"
    gmail_health.check(session, priya)
    gmail_health.check(session, priya)                   # still broken: no second email
    assert sent == [(ravi.email, [priya.email], "Action needed: reconnect your Gmail to the invoice app")]


def test_auth_error_recognition():
    assert gmail_health.looks_like_auth_error(RuntimeError("Error 401: invalid_grant"))
    assert gmail_health.looks_like_auth_error(RuntimeError("Connected account is not active"))
    assert not gmail_health.looks_like_auth_error(RuntimeError("Composio 500"))


# ---------- webhook delivery (Phase 1 item 6)

def _signed(body: str, secret: str = "whsec_test", ts: int | None = None) -> dict:
    import base64
    import hashlib
    import hmac
    import time
    ts = str(ts or int(time.time()))
    sig = base64.b64encode(hmac.new(secret.encode(), f"msg_1.{ts}.{body}".encode(), hashlib.sha256).digest()).decode()
    return {"webhook-id": "msg_1", "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}",
            "content-type": "application/json"}


def _webhook_body(account="ca_priya", slug="GMAIL_NEW_GMAIL_MESSAGE"):
    import json
    return json.dumps({"trigger_name": slug, "connection_id": account, "trigger_id": "ti_1",
                       "payload": {"id": "m-web-1", "message_id": "m-web-1", "subject": "Invoice"}})


def test_webhook_off_unless_configured(users, fake, monkeypatch):
    monkeypatch.setattr(settings, "gmail_delivery", "websocket")
    with TestClient(app) as c:
        assert c.post("/webhooks/composio", content=_webhook_body(), headers=_signed(_webhook_body())).status_code == 404


def test_signed_webhook_event_is_handled_and_forgeries_refused(users, fake, monkeypatch):
    import threading
    monkeypatch.setattr(settings, "gmail_delivery", "webhook")
    monkeypatch.setattr(settings, "composio_webhook_secret", "whsec_test")
    monkeypatch.setitem(gmail_listener.status, "running", False)   # listener in another process
    handled, done = [], threading.Event()
    monkeypatch.setattr(gmail_listener, "on_event", lambda ev: (handled.append(ev), done.set()))
    from composio import Composio
    monkeypatch.setattr(fake, "triggers", Composio(api_key="x", allow_tracking=False).triggers, raising=False)
    body = _webhook_body()
    with TestClient(app) as c:
        r = c.post("/webhooks/composio", content=body, headers=_signed(body))
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert done.wait(5)
        assert handled[0]["user_id"] == users["priya"].composio_user_id          # found by connected account
        assert handled[0]["payload"]["id"] == "m-web-1" and handled[0]["trigger_slug"] == gmail.TRIGGER
        # wrong secret, tampered body, old timestamp: refused
        assert c.post("/webhooks/composio", content=body, headers=_signed(body, "whsec_other")).status_code == 401
        assert c.post("/webhooks/composio", content=body.replace("Invoice", "Hacked"),
                      headers=_signed(body)).status_code == 401
        assert c.post("/webhooks/composio", content=body, headers=_signed(body, ts=1_000_000_000)).status_code == 401
        # an inbox we don't know: accepted (so Composio doesn't retry) but ignored
        other = _webhook_body(account="ca_unknown")
        assert c.post("/webhooks/composio", content=other, headers=_signed(other)).json()["ignored"] is True
    assert len(handled) == 1


# ---------- invoices behind download links (Phase 1 item 3)

def _portal(monkeypatch, routes):
    """httpx with a fake web: routes = {url: (status, headers, body)}"""
    import httpx
    seen = []

    def handler(request):
        seen.append(str(request.url))
        status, headers, body = routes.get(str(request.url), (404, {}, b"not found"))
        return httpx.Response(status, headers=headers, content=body)
    real_client = httpx.Client
    monkeypatch.setattr(gmail.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return seen


def _link_email(text):
    return {"messageId": "mlink", "subject": "Your invoice", "sender": "billing@zoho.in",
            "messageTimestamp": "2026-10-06T02:00:00Z", "messageText": text, "attachmentList": [],
            "payload": {"parts": []}}


def test_invoice_behind_an_allowed_link_is_downloaded(users, fake, session, monkeypatch):
    from app.models import EmailMessage, Invoice
    from sqlalchemy import select
    monkeypatch.setattr(settings, "gmail_link_domains", "zoho.in")
    pdf = b"%PDF-1.4 link invoice"
    seen = _portal(monkeypatch, {
        "https://books.zoho.in/inv/9": (302, {"location": "https://files.zoho.in/x/INV-9.pdf"}, b""),
        "https://files.zoho.in/x/INV-9.pdf": (200, {"content-type": "application/pdf"}, pdf),
    })
    user = session.get(User, users["priya"].id)
    ids = gmail.handle_message(session, user, _link_email(
        "Dear customer, download your invoice: https://books.zoho.in/inv/9. Also see https://evil.example/x.pdf"))
    assert len(ids) == 1 and seen == ["https://books.zoho.in/inv/9", "https://files.zoho.in/x/INV-9.pdf"]
    inv = session.get(Invoice, ids[0])
    assert inv.original_filename == "INV-9.pdf" and inv.source_ref == "mlink/link1"
    row = session.scalar(select(EmailMessage).where(EmailMessage.message_id == "mlink"))
    assert row.outcome == "done" and "from link" in row.attachments[0]["note"]


def test_links_are_never_followed_outside_the_allow_list(users, fake, session, monkeypatch):
    from app.models import EmailMessage
    from sqlalchemy import select
    monkeypatch.setattr(settings, "gmail_link_domains", "zoho.in")
    monkeypatch.setattr(settings, "gmail_read_body_invoices", False)
    seen = _portal(monkeypatch, {
        "https://books.zoho.in/a": (302, {"location": "http://192.168.1.1/admin"}, b""),
        "https://books.zoho.in/login": (200, {"content-type": "text/html"}, b"<html>Sign in</html>"),
    })
    user = session.get(User, users["priya"].id)
    assert gmail.handle_message(session, user, _link_email(
        "https://books.zoho.in/a https://books.zoho.in/login http://books.zoho.in/plain https://notzoho.in/x")) == []
    assert seen == ["https://books.zoho.in/a", "https://books.zoho.in/login"]      # no http, no other domains
    notes = [a["note"] for a in session.scalar(select(EmailMessage).where(EmailMessage.message_id == "mlink")).attachments]
    assert any("outside the allowed domains" in n for n in notes) and any("needs a login" in n for n in notes)


def test_links_ignored_when_no_domains_are_allowed(monkeypatch):
    monkeypatch.setattr(settings, "gmail_link_domains", "")
    assert gmail.links_in("https://books.zoho.in/inv/9") == []
