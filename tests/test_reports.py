"""Summary report by email: schedule, contents, sending once per slot (Gmail faked)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import gmail, reports
from app.api import app
from app.config import settings
from app.models import ReportRun, Status
from tests.test_assistant import add

IST = timezone(timedelta(hours=5, minutes=30))


def at(y, m, d, hh=9, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


@pytest.fixture
def sent(monkeypatch):
    """Captures emails instead of sending them through Composio."""
    out = []
    monkeypatch.setattr(gmail, "send_email", lambda user, to, subject, body: out.append(
        {"from": user.email, "to": to, "subject": subject, "body": body}))
    return out


@pytest.fixture
def connected(users):
    return users["priya"]          # the only user with Gmail connected


def test_indian_money_format():
    assert reports._money(1065908.16) == "₹10,65,908.16"
    assert reports._money(999) == "₹999.00" and reports._money(-6136) == "-₹6,136.00"
    assert reports._money(1234.5, "USD") == "USD 1,234.50"


def test_due_slots(monkeypatch):
    monkeypatch.setattr(settings, "report_time", "09:00")
    monkeypatch.setattr(settings, "report_schedule", "off")
    assert reports.due_slot(at(2026, 10, 19)) is None and reports.next_run() is None
    monkeypatch.setattr(settings, "report_schedule", "daily")
    slot, start, end = reports.due_slot(at(2026, 10, 14, 9, 5))
    assert slot == "daily:2026-10-14" and (end - start) == timedelta(days=1) and end.date().day == 14
    assert reports.due_slot(at(2026, 10, 14, 8, 59)) is None          # before the send time
    monkeypatch.setattr(settings, "report_schedule", "weekly")
    monkeypatch.setattr(settings, "report_weekday", "Monday")
    assert reports.due_slot(at(2026, 10, 19, 23, 0))[0] == "weekly:2026-10-19"   # 19 Oct 2026 is a Monday
    assert reports.due_slot(at(2026, 10, 20, 10, 0)) is None
    assert reports.next_run(at(2026, 10, 14, 12, 0)).date().isoformat() == "2026-10-19"
    monkeypatch.setattr(settings, "report_schedule", "monthly")
    slot, start, end = reports.due_slot(at(2026, 11, 1, 9, 30))
    assert slot == "monthly:2026-11" and start.date().isoformat() == "2026-10-01"
    assert reports.due_slot(at(2026, 11, 2, 9, 30)) is None


def test_report_contents(session, users):
    add(session, users, 1, Status.EXPORTED, approved_by="auto", approved_at=datetime.now(timezone.utc))
    add(session, users, 2, Status.NEEDS_REVIEW, status_reason="Amount above <b>limit</b>",
        extracted={"invoice_number": "R-2", "supplier_name": "<script>x</script> Traders"})
    add(session, users, 3, Status.EXPORTED, approved_by="Priya", approved_at=datetime.now(timezone.utc),
        extracted={"document_type": "credit_note", "invoice_number": "CN-1"})
    add(session, users, 4, Status.DUPLICATE)
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    data = reports.build(session, end - timedelta(days=7), end)
    assert data["received"]["documents"] == 4 and data["received"]["counted"] == 3
    assert data["approved"]["documents"] == 2 and data["approved"]["auto"] == 1
    assert data["approved"]["totals"] == {"INR": 0.0}                 # invoice minus an equal credit note
    assert data["waiting"]["documents"] == 1 and data["waiting"]["oldest"][0]["invoice_no"] == "R-2"
    subject, body = reports.render(data)
    assert "2 approved, 1 waiting for review" in subject
    assert "<script>" not in body and "&lt;script&gt;" in body and "&lt;b&gt;limit" in body


def test_send_once_per_slot_and_retry_after_failure(session, users, connected, sent, monkeypatch):
    monkeypatch.setattr(settings, "report_schedule", "daily")
    monkeypatch.setattr(settings, "report_recipients", "boss@example.com, ap@example.com")
    now = datetime.now(IST).replace(hour=10)
    run = reports.run_due(session, now)
    assert run.status == "sent" and run.slot.startswith("daily:")
    assert sent[0]["to"] == ["boss@example.com", "ap@example.com"] and sent[0]["from"] == connected.email
    assert reports.run_due(session, now + timedelta(minutes=5)) is None and len(sent) == 1   # not twice

    def broken(*a):
        raise RuntimeError("Gmail said no")
    monkeypatch.setattr(gmail, "send_email", broken)
    tomorrow = now + timedelta(days=1)
    failed = reports.run_due(session, tomorrow)
    assert failed.status == "failed" and "Gmail said no" in failed.error and "#failed-" in failed.slot
    assert reports.run_due(session, tomorrow + timedelta(minutes=5)) is None      # waits before retrying
    monkeypatch.setattr(gmail, "send_email", lambda *a: None)
    session.query(ReportRun).filter(ReportRun.id == failed.id).update(
        {"created_at": datetime.now(timezone.utc) - timedelta(hours=1)})
    session.commit()
    assert reports.run_due(session, tomorrow + timedelta(minutes=40)).status == "sent"


def test_no_gmail_to_send_from(session, users, sent):
    users["priya"].gmail_account_id = None
    session.commit()
    end = datetime.now(timezone.utc)
    run = reports.send(session, end - timedelta(days=1), end)
    assert run.status == "failed" and "No Gmail account" in run.error and sent == []


def test_report_endpoints(session, users, connected, sent, monkeypatch):
    monkeypatch.setattr(settings, "report_recipients", "ravi@example.com, accounts@example.com")
    h = {"X-User-Id": str(users["ravi"].id)}       # Ravi has no Gmail: sent from Priya's
    with TestClient(app) as client:
        info = client.get("/api/v1/reports").json()
        assert info["schedule"] == "off" and info["sender"]["email"] == connected.email
        assert info["recipients"] == ["ravi@example.com", "accounts@example.com"]
        p = client.get("/api/v1/reports/preview?period=month").json()
        assert p["subject"].startswith("Invoice summary") and "<h1" in p["html"]
        assert client.get("/api/v1/reports/preview?period=year").status_code == 422
        r = client.post("/api/v1/reports/send", headers=h, json={"period": "day", "recipients": ["Ravi@Example.com"]})
        assert r.status_code == 200 and r.json()["status"] == "sent" and r.json()["sent_by"] == users["ravi"].name
        assert [t.lower() for t in sent[-1]["to"]] == ["ravi@example.com"] and sent[-1]["from"] == connected.email
        # not to outside addresses (it is sent from a colleague's mailbox)
        out = client.post("/api/v1/reports/send", headers=h, json={"recipients": ["x@example.com"]})
        assert out.status_code == 422 and "x@example.com" in out.json()["detail"]
        assert client.get("/api/v1/reports").json()["runs"][0]["id"] == r.json()["id"]
        assert client.post("/api/v1/reports/send", headers=h, json={"recipients": ["not-an-email"]}).status_code == 422



def test_creating_a_user_does_not_make_an_address_a_report_recipient(session, users, connected, sent, monkeypatch):
    """Anyone can create a user (no login yet): that must not open the report to their address."""
    monkeypatch.setattr(settings, "report_recipients", "accounts@example.com")
    with TestClient(app) as client:
        uid = client.post("/api/v1/users", json={"name": "X", "email": "attacker@evil.example"}).json()["id"]
        r = client.post("/api/v1/reports/send", headers={"X-User-Id": str(uid)},
                        json={"recipients": ["attacker@evil.example"]})
        assert r.status_code == 422 and sent == []
    assert reports.default_recipients(session) == ["accounts@example.com"]


def test_scheduled_report_waits_for_configured_recipients(session, users, connected, sent, monkeypatch):
    monkeypatch.setattr(settings, "report_schedule", "daily")
    monkeypatch.setattr(settings, "report_recipients", "")
    assert reports.run_due(session, datetime.now(IST).replace(hour=10)) is None and sent == []
