"""Gmail via Composio, with Composio faked (payload shapes from the GMAIL_NEW_GMAIL_MESSAGE schema)."""
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import gmail, gmail_listener
from app.api import app
from app.db import SessionLocal
from app.models import EmailMessage, Invoice, User


class FakeComposio:
    """Records calls; serves attachments from `files` and emails from `inbox`."""

    def __init__(self):
        self.files = {}          # attachment_id -> bytes
        self.inbox = []          # messages returned by GMAIL_FETCH_EMAILS
        self.calls = []
        self.accounts = []       # ACTIVE connected account ids
        self.fail_attachment = set()
        self.statuses = {}       # connected account id -> status, for the connect watcher
        self.active_triggers = []   # trigger ids already on the inbox
        self.triggers = SimpleNamespace(
            create=self._create_trigger, delete=self._delete_trigger,
            list_active=lambda **kw: SimpleNamespace(items=[
                SimpleNamespace(id=t, trigger_config={}) for t in self.active_triggers]))
        self.connected_accounts = SimpleNamespace(list=self._list_accounts, get=self._get_account)

    def _create_trigger(self, slug, *, user_id, connected_account_id, trigger_config):
        self.calls.append(("trigger", slug, user_id, connected_account_id, trigger_config))
        return SimpleNamespace(trigger_id=f"ti_{connected_account_id}")

    def _delete_trigger(self, trigger_id):
        self.calls.append(("delete_trigger", trigger_id))
        self.active_triggers.remove(trigger_id)

    def _get_account(self, account_id):
        return SimpleNamespace(id=account_id, status=self.statuses.get(account_id, "INITIATED"))

    def _list_accounts(self, **kw):
        self.calls.append(("list_accounts", kw["user_ids"]))
        return SimpleNamespace(items=[SimpleNamespace(id=a) for a in self.accounts])

    def create(self, user_id):
        def authorize(toolkit, callback_url=None):
            self.calls.append(("authorize", user_id, toolkit, callback_url))
            return SimpleNamespace(id=f"ca_new_{user_id}",
                                   redirect_url=f"https://connect.composio.dev/link/{user_id}/{toolkit}")
        return SimpleNamespace(authorize=authorize)

    def execute(self, user, slug, args):
        self.calls.append((slug, user.composio_user_id, args))
        if slug == "GMAIL_GET_ATTACHMENT":
            if args["attachment_id"] in self.fail_attachment:
                raise RuntimeError("Composio 500")
            return {"file": {"name": args["file_name"], "mimetype": "application/pdf",
                             "s3url": f"https://s3.example/{args['attachment_id']}"}}
        if slug == "GMAIL_FETCH_EMAILS":
            return {"messages": self.inbox}
        if slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
            return next(m for m in self.inbox if m["messageId"] == args["message_id"])
        raise AssertionError(slug)

    def download(self, url):
        return self.files[url.rsplit("/", 1)[1]]


@pytest.fixture
def fake(monkeypatch):
    f = FakeComposio()
    monkeypatch.setattr(gmail, "_composio", lambda: f)
    monkeypatch.setattr(gmail, "execute", f.execute)
    monkeypatch.setattr(gmail, "download", f.download)
    return f


def event(user, message_id="18c1a2b3c4d5e6f7", attachments=None, subject="Invoice INV-10291 — ABC Traders",
          ts="2026-10-06T09:30:00Z"):
    """A GMAIL_NEW_GMAIL_MESSAGE trigger event as the SDK's subscribe() delivers it."""
    if attachments is None:
        attachments = [{"filename": "INV-10291.pdf", "mimeType": "application/pdf", "attachmentId": "A1"}]
    return {"user_id": user.composio_user_id, "trigger_slug": gmail.TRIGGER, "payload": {
        "id": message_id, "message_id": message_id, "thread_id": "t1", "sender": "ABC Traders <vendor@gmail.com>",
        "subject": subject, "message_text": "Please find attached.", "message_timestamp": ts,
        "attachment_list": attachments, "label_ids": ["INBOX"]}}


def invoices():
    with SessionLocal() as s:
        return s.scalars(select(Invoice).order_by(Invoice.id)).all()


# ---------- events

def test_event_with_invoice_and_terms(users, fake, session):
    fake.files.update({"A1": b"%PDF invoice", "A2": b"%PDF terms", "A3": b"GIF"})
    ids = gmail.handle_event(session, event(users["priya"], attachments=[
        {"filename": "INV-10291.pdf", "mimeType": "application/pdf", "attachmentId": "A1"},
        {"filename": "terms.pdf", "mimeType": "application/pdf", "attachmentId": "A2"},
        {"filename": "logo.gif", "mimeType": "image/gif", "attachmentId": "A3"},
    ]))
    assert ids == [1, 2]       # each document is its own job; classification drops terms.pdf later
    rows = invoices()
    assert [(i.original_filename, i.source, i.user_id) for i in rows] == [
        ("INV-10291.pdf", "gmail", users["priya"].id), ("terms.pdf", "gmail", users["priya"].id)]
    assert rows[0].sender == "ABC Traders <vendor@gmail.com>" and rows[0].email_subject.startswith("Invoice INV-10291")
    assert rows[0].source_ref == "18c1a2b3c4d5e6f7/INV-10291.pdf"
    get = [c for c in fake.calls if c[0] == "GMAIL_GET_ATTACHMENT"]
    assert get[0] == ("GMAIL_GET_ATTACHMENT", "ap-user-1",
                      {"message_id": "18c1a2b3c4d5e6f7", "attachment_id": "A1", "file_name": "INV-10291.pdf"})


def test_same_event_twice_downloads_once(users, fake, session):
    fake.files["A1"] = b"%PDF invoice"
    gmail.handle_event(session, event(users["priya"]))
    assert gmail.handle_event(session, event(users["priya"])) == []
    assert sum(c[0] == "GMAIL_GET_ATTACHMENT" for c in fake.calls) == 1


def test_attachment_without_extension_uses_mime_type(users, fake, session):
    fake.files["A9"] = b"%PDF scan"
    gmail.handle_event(session, event(users["priya"], attachments=[
        {"filename": "Scan_0042", "mimeType": "application/pdf", "attachmentId": "A9"}]))
    assert invoices()[0].original_filename == "Scan_0042.pdf"


def test_trimmed_event_fetches_full_message(users, fake, session):
    fake.files["B1"] = b"%PDF from raw payload"
    fake.inbox = [{"messageId": "m2", "subject": "Bill", "sender": "x@y.com", "payload": {
        "mimeType": "multipart/mixed", "parts": [
            {"filename": "", "body": {"data": "aGk="}},
            {"filename": "", "parts": [{"filename": "bill.pdf", "mimeType": "application/pdf",
                                        "body": {"attachmentId": "B1"}}]}]}}]
    ids = gmail.handle_event(session, event(users["priya"], message_id="m2", attachments=[]))
    assert len(ids) == 1 and ("GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID", "ap-user-1",
                              {"message_id": "m2", "format": "full"}) in fake.calls


def test_unknown_user_ignored(users, fake, session):
    assert gmail.handle_event(session, {"user_id": "someone-else", "payload": {"id": "x"}}) == []
    assert gmail.handle_event(session, {"user_id": "ap-user-999", "payload": {"id": "x"}}) == []


def test_subject_keywords(users, fake, session, monkeypatch):
    monkeypatch.setattr(gmail.settings, "gmail_subject_keywords", "invoice, bill")
    fake.files["A1"] = b"%PDF"
    assert gmail.handle_event(session, event(users["priya"], subject="Holiday photos")) == []
    assert gmail.handle_event(session, event(users["priya"], subject="Your BILL for Sept")) == [1]


def test_listener_failure_rewinds_sync_point(users, fake):
    fake.fail_attachment.add("A1")
    with SessionLocal() as s:
        s.get(User, users["priya"].id).gmail_last_sync_at = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
        s.commit()
    gmail_listener.on_event(event(users["priya"], ts="2026-10-06T09:30:00Z"))   # must not raise
    with SessionLocal() as s:
        assert s.get(User, users["priya"].id).gmail_last_sync_at == datetime(2026, 10, 6, 9, 29, tzinfo=timezone.utc)
    assert invoices() == []


# ---------- sync (catch-up)

def test_sync_catches_up_and_skips_failures(users, fake, session):
    fake.files.update({"C1": b"%PDF one", "C3": b"%PDF three"})
    fake.fail_attachment.add("C2")
    fake.inbox = [
        {"messageId": f"m{i}", "subject": "Invoice", "sender": "v@x.com", "messageTimestamp": f"2026-10-06T0{i}:00:00Z",
         "attachmentList": [{"filename": f"inv{i}.pdf", "mimeType": "application/pdf", "attachmentId": f"C{i}"}]}
        for i in (1, 2, 3)]
    user = session.get(User, users["priya"].id)
    since = datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert gmail.sync_user(session, user, since) == [1, 2]
    query = next(c[2]["query"] for c in fake.calls if c[0] == "GMAIL_FETCH_EMAILS")
    assert query == f"has:attachment after:{int((since - gmail.SYNC_OVERLAP).timestamp())}"   # 10 min overlap
    # sync point stops just before the failed email, so the next sync retries it
    assert user.gmail_last_sync_at == datetime(2026, 10, 6, 1, 59, tzinfo=timezone.utc)

    fake.fail_attachment.clear()
    fake.files["C2"] = b"%PDF two"
    assert gmail.sync_user(session, user) == [3]
    assert user.gmail_last_sync_at > datetime.now(timezone.utc) - timedelta(minutes=1)


def test_sync_requires_connection(users, fake, session):
    with pytest.raises(gmail.GmailNotConnected):
        gmail.sync_user(session, session.get(User, users["ravi"].id))


# ---------- API: connect, activate, sync

def test_connect_and_activate_flow(users, fake, monkeypatch):
    monkeypatch.setattr(gmail, "_activate_when_connected", lambda *a: None)   # tested separately
    ravi = users["ravi"]
    with TestClient(app) as client:
        r = client.post(f"/api/v1/users/{ravi.id}/gmail/connect")
        assert r.json()["connect_url"] == f"https://connect.composio.dev/link/ap-user-{ravi.id}/gmail"
        assert ("authorize", f"ap-user-{ravi.id}", "gmail",
                f"http://localhost:8000/api/v1/users/{ravi.id}/gmail/callback") in fake.calls

        r = client.post(f"/api/v1/users/{ravi.id}/gmail/activate")       # not authorised yet
        assert r.status_code == 409 and "connect link" in r.json()["detail"]

        fake.accounts = ["ca_ravi"]
        r = client.post(f"/api/v1/users/{ravi.id}/gmail/activate")
        assert r.status_code == 200, r.text
        g = r.json()["gmail"]
        assert g["connected"] and g["trigger_id"] == "ti_ca_ravi" and g["last_sync_at"]
        # user_id is required by Composio when 2FA is on for the project
        assert ("trigger", "GMAIL_NEW_GMAIL_MESSAGE", f"ap-user-{ravi.id}", "ca_ravi",
                {"interval": 1, "query": "has:attachment"}) in fake.calls

        fake.inbox = [{"messageId": "m1", "subject": "Invoice", "sender": "v@x.com",
                       "attachmentList": [{"filename": "a.pdf", "attachmentId": "Z1"}]}]
        fake.files["Z1"] = b"%PDF"
        assert client.post(f"/api/v1/users/{ravi.id}/gmail/sync?days=7").json() == {"new_invoice_ids": [1]}
        assert client.post(f"/api/v1/users/{users['ravi'].id + 5}/gmail/sync").status_code == 404


def test_composio_errors_become_502(users, monkeypatch):
    def boom():
        raise RuntimeError("401 invalid api key")
    monkeypatch.setattr(gmail, "_composio", boom)
    with TestClient(app) as client:
        r = client.post(f"/api/v1/users/{users['priya'].id}/gmail/connect")
        assert r.status_code == 502 and "invalid api key" in r.json()["detail"]


# ---------- automatic activation after connecting

def test_callback_page_activates(users, fake):
    fake.accounts = ["ca_priya2"]
    with TestClient(app) as client:
        r = client.get(f"/api/v1/users/{users['priya'].id}/gmail/callback?status=success")
        assert r.status_code == 200 and "Gmail connected" in r.text
        assert client.get(f"/api/v1/users/{users['priya'].id}").json()["gmail"]["trigger_id"] == "ti_ca_priya2"

        fake.accounts = []                         # user cancelled on the consent screen
        r = client.get(f"/api/v1/users/{users['ravi'].id}/gmail/callback?status=failed")
        assert "not connected" in r.text


def test_watcher_activates_when_callback_never_arrives(users, fake, monkeypatch):
    monkeypatch.setattr(gmail, "CONNECT_POLL_SECONDS", 0.05)
    ravi_id = users["ravi"].id
    t = threading.Thread(target=gmail._activate_when_connected, args=(ravi_id, "ca_ravi_new", 1))
    t.start()
    time.sleep(0.2)
    fake.accounts = ["ca_ravi_new"]
    fake.statuses["ca_ravi_new"] = "ACTIVE"       # user finished the consent screen
    t.join(5)
    assert not t.is_alive()
    with SessionLocal() as s:
        user = s.get(User, ravi_id)
        assert (user.gmail_account_id, user.gmail_trigger_id) == ("ca_ravi_new", "ti_ca_ravi_new")


def test_watcher_gives_up_on_failed_connection(users, fake, monkeypatch):
    monkeypatch.setattr(gmail, "CONNECT_POLL_SECONDS", 0.05)
    fake.statuses["ca_x"] = "FAILED"
    gmail._activate_when_connected(users["ravi"].id, "ca_x", 1)   # returns instead of waiting
    assert not any(c[0] == "trigger" for c in fake.calls)


def test_activate_removes_older_triggers(users, fake, session):
    """Real case: a trigger made with an old GMAIL_QUERY delivered every email twice."""
    fake.accounts = ["ca_priya"]
    fake.active_triggers = ["ti_old_query", "ti_ca_priya"]
    gmail.activate(session, session.get(User, users["priya"].id))
    assert fake.active_triggers == ["ti_ca_priya"]
    assert ("delete_trigger", "ti_old_query") in fake.calls


def test_same_file_name_twice_in_one_email(users, fake, session):
    fake.files.update({"D1": b"%PDF first", "D2": b"%PDF second"})
    ids = gmail.handle_event(session, event(users["priya"], attachments=[
        {"filename": "invoice.pdf", "mimeType": "application/pdf", "attachmentId": "D1"},
        {"filename": "invoice.pdf", "mimeType": "application/pdf", "attachmentId": "D2"}]))
    assert len(ids) == 2
    assert [i.source_ref for i in invoices()] == ["18c1a2b3c4d5e6f7/invoice.pdf", "18c1a2b3c4d5e6f7/invoice.pdf#2"]


def test_sync_keeps_a_rewind_from_a_failed_event(users, fake, session):
    """A live event fails while a sync runs: the sync must not move past that email."""
    user = session.get(User, users["priya"].id)
    user.gmail_last_sync_at = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
    session.commit()

    def fetch_and_meanwhile_rewind(u, slug, args):
        with SessionLocal() as other:            # the listener rewinds in its own session
            other.get(User, u.id).gmail_last_sync_at = datetime(2026, 10, 6, 9, 29, tzinfo=timezone.utc)
            other.commit()
        return {"messages": []}

    fake.execute = fetch_and_meanwhile_rewind
    gmail.execute = fetch_and_meanwhile_rewind
    gmail.sync_user(session, user)
    assert user.gmail_last_sync_at == datetime(2026, 10, 6, 9, 29, tzinfo=timezone.utc)


def test_callback_page_escapes_html(users, fake, session):
    user = session.get(User, users["priya"].id)
    user.name = "<script>alert(1)</script>"
    session.commit()
    fake.accounts = ["ca_x"]
    with TestClient(app) as client:
        r = client.get(f"/api/v1/users/{user.id}/gmail/callback")
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text


def test_default_query_skips_sent_mail(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("GMAIL_QUERY")
    q = Settings(_env_file=None).gmail_query
    # Spam included, sent mail not; also emails without attachments that mention invoice/bill/GSTIN
    assert q == "in:anywhere -in:sent -in:drafts -in:trash {has:attachment invoice bill gstin}"



def test_same_file_in_another_email_is_noted_once(users, fake, session):
    """Real case: the same journal PDF was emailed twice under different names: one record,
    the second delivery noted on it, and later syncs don't download it again."""
    fake.files.update({"J1": b"%PDF journal", "J2": b"%PDF journal"})
    first = gmail.handle_event(session, event(users["priya"], message_id="m1", attachments=[
        {"filename": "HFCL Journal posting Entry against.pdf", "mimeType": "application/pdf", "attachmentId": "J1"}]))
    again = event(users["priya"], message_id="m2", subject="", attachments=[
        {"filename": "HFCL Journal posting Entry against the Invoice No.RV2606000765.pdf",
         "mimeType": "application/pdf", "attachmentId": "J2"}])
    assert gmail.handle_event(session, again) == []
    assert gmail.handle_event(session, again) == []          # seen: not downloaded a second time
    assert sum(c[0] == "GMAIL_GET_ATTACHMENT" for c in fake.calls) == 2
    from app.models import AuditLog
    notes = session.query(AuditLog).filter(AuditLog.stage == "received_again").all()
    assert len(notes) == 1 and notes[0].invoice_id == first[0]
    assert "HFCL Journal posting Entry against the Invoice No.RV2606000765.pdf" in notes[0].message


# ---------- network trouble

def test_execute_retries_network_errors(users, monkeypatch, session):
    """Real case: 'connection forcibly closed' / 'getaddrinfo failed' while syncing."""
    import composio_client
    import httpx as _httpx
    monkeypatch.setattr(gmail.time, "sleep", lambda s: None)
    calls = []

    def flaky(slug, args, **kw):
        calls.append(slug)
        if len(calls) < 3:
            raise composio_client.APIConnectionError(request=_httpx.Request("POST", "https://backend.composio.dev"))
        return {"successful": True, "data": {"messages": []}}

    monkeypatch.setattr(gmail, "_composio", lambda: SimpleNamespace(tools=SimpleNamespace(execute=flaky)))
    user = session.get(User, users["priya"].id)
    assert gmail.execute(user, "GMAIL_FETCH_EMAILS", {}) == {"messages": []}
    assert len(calls) == 3                                   # failed twice, third attempt worked

    calls.clear()
    monkeypatch.setattr(gmail, "_composio", lambda: SimpleNamespace(tools=SimpleNamespace(
        execute=lambda *a, **k: (_ for _ in ()).throw(_httpx.ConnectError("getaddrinfo failed")))))
    with pytest.raises(gmail.NetworkError):
        gmail.execute(user, "GMAIL_FETCH_EMAILS", {})


def test_offline_sync_is_one_warning_not_a_traceback(users, monkeypatch, caplog):
    def offline(user, slug, args):
        raise gmail.NetworkError("Composio GMAIL_FETCH_EMAILS: network error after 4 attempts")
    monkeypatch.setattr(gmail, "execute", offline)
    gmail_listener.sync_all()
    records = [r for r in caplog.records if r.name == "gmail_listener" and "skipped" in r.message]
    assert len(records) == 1 and records[0].levelname == "WARNING" and records[0].exc_info is None


# ---------- automatic sync after reconnecting

class _Sub:
    """A Composio subscription that drops after `lifetime` seconds (None = stays up)."""
    def __init__(self, lifetime):
        self.end = None if lifetime is None else time.monotonic() + lifetime
    def handle(self, **kw):
        return lambda fn: fn
    def is_alive(self):
        return self.end is None or time.monotonic() < self.end
    def has_errored(self):
        return False
    def stop(self):
        self.end = 0


def _run_listener(monkeypatch, subscriptions, sync_results, seconds):
    syncs = []

    def fake_sync_all():
        syncs.append(time.monotonic())
        return sync_results.pop(0) if sync_results else True

    monkeypatch.setattr(gmail_listener, "sync_all", fake_sync_all)
    monkeypatch.setattr(gmail_listener, "RECONNECT_DELAYS", (0.05, 0.1))
    monkeypatch.setattr(gmail_listener, "RETRY_SYNC_SECONDS", 0.2)
    subs = iter(subscriptions)
    monkeypatch.setattr(gmail_listener, "_composio",
                        lambda: SimpleNamespace(triggers=SimpleNamespace(subscribe=lambda: next(subs))))
    stop = threading.Event()
    t = threading.Thread(target=gmail_listener.run, args=(stop,))
    t.start()
    time.sleep(seconds)
    stop.set()
    t.join(5)
    return syncs


def test_reconnect_triggers_an_immediate_sync(monkeypatch):
    # connected, drops after 0.3s, reconnects: one sync at start + one right after reconnecting,
    # instead of waiting 15 minutes for the next periodic sync
    syncs = _run_listener(monkeypatch, [_Sub(0.3), _Sub(None)], [], seconds=1.5)
    assert len(syncs) == 2 and syncs[1] - syncs[0] < 2.0   # the listener checks the connection every second


def test_sync_that_failed_on_network_retries_soon(monkeypatch):
    syncs = _run_listener(monkeypatch, [_Sub(None)], [False, False, True], seconds=1.2)
    assert len(syncs) == 3            # failed, retried, retried and succeeded; then waits 15 min



def _mail(i, hour, att_id=None):
    att_id = att_id or f"P{i}"
    return {"messageId": f"p{i}", "subject": "Invoice", "messageTimestamp": f"2026-10-06T{hour:02d}:00:00Z",
            "attachmentList": [{"filename": f"p{i}.pdf", "mimeType": "application/pdf", "attachmentId": att_id}]}


def test_capped_sync_continues_next_time(users, fake, session, monkeypatch):
    """More emails than one sync takes: the sync point stays at the oldest one handled, so
    the older ones are fetched next time instead of being skipped (Gmail lists newest first)."""
    fake.files.update({f"P{i}": f"%PDF {i}".encode() for i in range(4)})
    fake.inbox = [_mail(i, 9 - i) for i in range(4)]
    pages = {None: {"messages": fake.inbox[:2], "nextPageToken": "t2"}, "t2": {"messages": fake.inbox[2:]}}

    def execute(user, slug, args):
        return pages[args.get("page_token")] if slug == "GMAIL_FETCH_EMAILS" else fake.execute(user, slug, args)
    monkeypatch.setattr(gmail, "execute", execute)
    user = session.get(User, users["priya"].id)
    assert len(gmail.sync_user(session, user, datetime(2026, 10, 5, tzinfo=timezone.utc), max_messages=2)) == 2
    assert user.gmail_last_sync_at == datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)   # p1, the oldest handled
    assert len(gmail.sync_user(session, user)) == 2                                     # p2 and p3 now


def test_email_failing_for_days_no_longer_holds_back_sync(users, fake, session):
    fake.fail_attachment.add("BAD")
    fake.inbox = [_mail(1, 2, "BAD")]
    user = session.get(User, users["priya"].id)
    gmail.sync_user(session, user, datetime(2026, 10, 5, tzinfo=timezone.utc))
    assert user.gmail_last_sync_at == datetime(2026, 10, 6, 1, 59, tzinfo=timezone.utc)   # retried at first
    row = session.scalar(select(EmailMessage).where(EmailMessage.message_id == "p1"))
    assert row.outcome == "failed"
    row.created_at = datetime.now(timezone.utc) - timedelta(days=2)                       # failing for 2 days
    session.commit()
    gmail.sync_user(session, user)
    assert user.gmail_last_sync_at > datetime.now(timezone.utc) - timedelta(minutes=1)    # left to the daily check


def test_too_large_attachment_is_recorded_not_retried(users, fake, session, monkeypatch):
    def too_large(url):
        raise gmail.TooLarge("larger than 15 MB (MAX_UPLOAD_MB)")
    monkeypatch.setattr(gmail, "download", too_large)
    fake.inbox = [_mail(1, 2)]
    user = session.get(User, users["priya"].id)
    assert gmail.sync_user(session, user, datetime(2026, 10, 5, tzinfo=timezone.utc)) == []
    row = session.scalar(select(EmailMessage).where(EmailMessage.message_id == "p1"))
    assert row.outcome == "no_documents" and row.attachments[0]["result"] == "too_large"
    assert user.gmail_last_sync_at > datetime.now(timezone.utc) - timedelta(minutes=1)


def test_reconnecting_keeps_the_sync_point(users, fake, session):
    """Access expired, user reconnects (new account id): emails from the outage are still fetched."""
    user = session.get(User, users["priya"].id)
    user.gmail_last_sync_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    session.commit()
    fake.accounts = ["ca_reconnected"]
    gmail.activate(session, user)
    assert user.gmail_account_id == "ca_reconnected"
    assert user.gmail_last_sync_at == datetime(2026, 10, 1, tzinfo=timezone.utc)
