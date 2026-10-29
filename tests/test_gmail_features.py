"""Email log, more file types, Gmail labels, daily check, email hints, protected PDFs."""
import email.message
import io
import zipfile
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import select

from app import gmail, worker
from app.api import app
from app.db import SessionLocal
from app.models import EmailMessage, GmailLabel, Invoice, PdfPassword, Status, User
from app.pipeline import extract
from tests.conftest import make_extracted
from tests.test_gmail import FakeComposio, event


class FakeGmail(FakeComposio):
    """FakeComposio plus the label tools."""

    def __init__(self):
        super().__init__()
        self.labels = {"INBOX": "INBOX"}       # name -> id

    def execute(self, user, slug, args):
        if slug == "GMAIL_LIST_LABELS":
            self.calls.append((slug, user.composio_user_id, args))
            return {"labels": [{"id": i, "name": n} for n, i in self.labels.items()]}
        if slug == "GMAIL_CREATE_LABEL":
            self.calls.append((slug, user.composio_user_id, args))
            self.labels[args["label_name"]] = f"Label_{len(self.labels)}"
            return {"id": self.labels[args["label_name"]], "name": args["label_name"]}
        if slug == "GMAIL_ADD_LABEL_TO_EMAIL":
            self.calls.append((slug, user.composio_user_id, args))
            return {"id": args["message_id"]}
        return super().execute(user, slug, args)


@pytest.fixture
def fake(monkeypatch):
    f = FakeGmail()
    monkeypatch.setattr(gmail, "_composio", lambda: f)
    monkeypatch.setattr(gmail, "execute", f.execute)
    monkeypatch.setattr(gmail, "download", f.download)
    return f


def pdf_bytes(text="TAX INVOICE INV-1 total 6136.00", password=None) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    buf = io.BytesIO()
    if password:
        doc.save(buf, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password + "-owner")
    else:
        doc.save(buf)
    doc.close()
    return buf.getvalue()


def email_row(session, message_id="18c1a2b3c4d5e6f7"):
    session.expire_all()
    return session.scalar(select(EmailMessage).where(EmailMessage.message_id == message_id))


def by_result(row):
    return {a["name"]: a for a in row.attachments}


# ---------- email log

def test_email_log_records_every_attachment(users, fake, session):
    fake.files.update({"A1": pdf_bytes(), "A2": b"GIF89a"})
    gmail.handle_event(session, event(users["priya"], attachments=[
        {"filename": "INV-1.pdf", "mimeType": "application/pdf", "attachmentId": "A1"},
        {"filename": "logo.gif", "mimeType": "image/gif", "attachmentId": "A2"}]))
    row = email_row(session)
    assert row.outcome == "done" and row.sender.startswith("ABC Traders") and row.folders == ["INBOX"]
    att = by_result(row)
    assert att["INV-1.pdf"]["result"] == "invoice" and att["INV-1.pdf"]["invoice_id"] == 1
    assert att["logo.gif"]["result"] == "unsupported"


def test_failed_email_is_logged_with_the_error(users, fake, session):
    fake.fail_attachment.add("A1")
    with pytest.raises(RuntimeError):
        gmail.handle_event(session, event(users["priya"]))
    row = email_row(session)
    assert row.outcome == "failed" and "Composio 500" in row.error


def test_keyword_skip_is_logged(users, fake, session, monkeypatch):
    monkeypatch.setattr(gmail.settings, "gmail_subject_keywords", "invoice")
    gmail.handle_event(session, event(users["priya"], subject="Holiday photos"))
    assert email_row(session).outcome == "skipped"


# ---------- more file types

def test_invoice_in_the_email_body_becomes_a_pdf(users, fake, session):
    ev = event(users["priya"], attachments=[], subject="Tax Invoice INV-77")
    ev["payload"]["payload"] = {"parts": []}       # full message: there really are no attachments
    ev["payload"]["message_text"] = ("Dear Sir, our Tax Invoice INV-77.\nGSTIN: 36AAACS1234A1Z3\n"
                                     "Taxable Rs 1,000.00\nIGST Rs 180.00\nTotal Rs 1,180.00")
    assert gmail.handle_event(session, ev) == [1]
    inv = session.get(Invoice, 1)
    assert inv.original_filename == "Email body - Tax Invoice INV-77.pdf" and inv.source_ref.endswith("/body")
    with pymupdf.open(inv.file_path) as d:
        assert "1,180.00" in d[0].get_text()


def test_our_own_reminder_email_is_not_read_as_an_invoice(users, fake, session):
    """A follow-up reminder lists invoices with amounts: without the marker it would look like an invoice."""
    from app import outbound
    text = ("Hello Priya, 2 invoices are waiting for review: #4 Sri Lakshmi Steels Rs 1,180.00, "
            "#5 Ganesh Traders Rs 6,136.00. Open the invoice app to deal with them.")
    ev = event(users["priya"], attachments=[], subject="Invoice app reminder: 2 waiting for review")
    ev["payload"]["payload"] = {"parts": []}
    ev["payload"]["message_text"] = f"{text}\nSent by the invoice app · {outbound.email_marker()}"
    assert gmail.handle_event(session, ev) == []
    assert email_row(session).outcome == "skipped" and session.query(Invoice).count() == 0
    # a forged marker (made without our COMPOSIO_API_KEY) does not hide an invoice
    ev = event(users["priya"], attachments=[], subject="Tax Invoice INV-78", message_id="m2")
    ev["payload"]["payload"] = {"parts": []}
    ev["payload"]["message_text"] = ("Our Tax Invoice INV-78. GSTIN: 36AAACS1234A1Z3\nTaxable Rs 1,000.00\n"
                                     "IGST Rs 180.00\nTotal Rs 1,180.00\nref ap-0123456789abcdef")
    assert gmail.handle_event(session, ev) != []


def test_ordinary_email_without_attachments_is_no_documents(users, fake, session):
    ev = event(users["priya"], attachments=[], subject="Lunch?")
    ev["payload"]["payload"] = {"parts": []}
    ev["payload"]["message_text"] = "Shall we have lunch at 1? The bill is on me."
    assert gmail.handle_event(session, ev) == []
    assert email_row(session).outcome == "no_documents"


def test_zip_and_forwarded_email_are_unpacked(users, fake, session):
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("inv-a.pdf", pdf_bytes("INVOICE A"))
        zf.writestr("inv-b.pdf", pdf_bytes("INVOICE B"))
        zf.writestr("readme.txt", "hello")
    fwd = email.message.EmailMessage()
    fwd["Subject"], fwd["From"] = "Fwd: bill", "vendor@x.com"
    fwd.set_content("see attached")
    fwd.add_attachment(pdf_bytes("INVOICE C"), maintype="application", subtype="pdf", filename="inv-c.pdf")
    fake.files.update({"Z1": z.getvalue(), "E1": fwd.as_bytes()})
    ids = gmail.handle_event(session, event(users["priya"], attachments=[
        {"filename": "invoices.zip", "mimeType": "application/zip", "attachmentId": "Z1"},
        {"filename": "forwarded.eml", "mimeType": "message/rfc822", "attachmentId": "E1"}]))
    assert len(ids) == 3
    names = [i.original_filename for i in session.scalars(select(Invoice).order_by(Invoice.id))]
    assert names == ["inv-a.pdf", "inv-b.pdf", "inv-c.pdf"]
    att = by_result(email_row(session))
    assert att["invoices.zip"]["result"] == "unpacked" and len(att["invoices.zip"]["invoice_ids"]) == 2
    assert "readme.txt" in att["invoices.zip"]["note"]
    # Again (e.g. the daily check): nothing downloaded or created twice
    calls_before = len(fake.calls)
    assert gmail.handle_event(session, event(users["priya"], attachments=[
        {"filename": "invoices.zip", "mimeType": "application/zip", "attachmentId": "Z1"},
        {"filename": "forwarded.eml", "mimeType": "message/rfc822", "attachmentId": "E1"}])) == []
    assert not any(c[0] == "GMAIL_GET_ATTACHMENT" for c in fake.calls[calls_before:])


def test_excel_attachment_is_read_as_text(users, fake, session, tmp_path, monkeypatch):
    wb = Workbook()
    wb.active.append(["Tax Invoice", "INV-9"])
    wb.active.append(["Grand Total", 1180])
    buf = io.BytesIO()
    wb.save(buf)
    fake.files["X1"] = buf.getvalue()
    [inv_id] = gmail.handle_event(session, event(users["priya"], attachments=[
        {"filename": "bill.xlsx", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "attachmentId": "X1"}]))
    seen = {}

    def fake_run(prompt, images=None):
        seen["prompt"], seen["images"] = prompt, images
        return SimpleNamespace(content=make_extracted(), metrics=None)

    monkeypatch.setattr(extract.extractor, "run", fake_run)
    extract.extract_invoice(session.get(Invoice, inv_id).file_path, "bill.xlsx")
    assert seen["images"] is None and "INV-9" in seen["prompt"] and "Grand Total\t1180" in seen["prompt"]


# ---------- Gmail labels

def test_labels_follow_invoice_status(users, fake, session):
    fake.files["A1"] = pdf_bytes()
    gmail.handle_event(session, event(users["priya"]))
    user = session.get(User, users["priya"].id)
    assert gmail.update_labels(session, user) == 0            # still RECEIVED: no label yet

    inv = session.get(Invoice, 1)
    inv.status = Status.NEEDS_REVIEW
    session.commit()
    assert gmail.update_labels(session, user) == 1
    add = [c for c in fake.calls if c[0] == "GMAIL_ADD_LABEL_TO_EMAIL"][-1][2]
    assert add["add_label_ids"] == [fake.labels["AP/Needs review"]] and "remove_label_ids" not in add

    inv.status = Status.EXPORTED
    session.commit()
    assert gmail.update_labels(session, user) == 1
    add = [c for c in fake.calls if c[0] == "GMAIL_ADD_LABEL_TO_EMAIL"][-1][2]
    assert add == {"message_id": "18c1a2b3c4d5e6f7", "add_label_ids": [fake.labels["AP/Processed"]],
                   "remove_label_ids": [fake.labels["AP/Needs review"]]}
    assert gmail.update_labels(session, user) == 0            # already right: nothing to do
    assert {r.name for r in session.scalars(select(GmailLabel))} == {"AP/Needs review", "AP/Processed"}


def test_existing_gmail_label_is_reused_not_recreated(users, fake, session):
    fake.labels["AP/Not invoice"] = "Label_existing"
    fake.files["A1"] = pdf_bytes()
    gmail.handle_event(session, event(users["priya"]))
    inv = session.get(Invoice, 1)
    inv.status = Status.NOT_INVOICE
    session.commit()
    gmail.update_labels(session, session.get(User, users["priya"].id))
    assert not any(c[0] == "GMAIL_CREATE_LABEL" for c in fake.calls)
    assert [c for c in fake.calls if c[0] == "GMAIL_ADD_LABEL_TO_EMAIL"][-1][2]["add_label_ids"] == ["Label_existing"]


# ---------- daily check

def test_reconcile_finds_emails_missing_from_the_log(users, fake, session):
    fake.files.update({"C1": pdf_bytes("one"), "C2": pdf_bytes("two")})
    fake.inbox = [{"messageId": f"m{i}", "subject": "Invoice", "sender": "v@x.com",
                   "messageTimestamp": "2026-10-06T10:00:00Z",
                   "attachmentList": [{"filename": f"inv{i}.pdf", "attachmentId": f"C{i}"}]} for i in (1, 2)]
    gmail.handle_message(session, session.get(User, users["priya"].id), fake.inbox[0])   # m1 seen live
    summary = gmail.reconcile_user(session, session.get(User, users["priya"].id), days=30)
    assert summary["emails_found_missing"] == ["m2"] and summary["new_invoices"] == [2]
    assert summary["by_outcome"] == {"done": 2}


# ---------- email hints for the AI

def test_email_hints_and_known_supplier(users, fake, session):
    fake.files.update({"A1": pdf_bytes("first"), "A2": pdf_bytes("second")})
    gmail.handle_event(session, event(users["priya"], message_id="m1", attachments=[
        {"filename": "inv1.pdf", "attachmentId": "A1"}]))
    first = session.get(Invoice, 1)
    first.status, first.supplier_name, first.supplier_gstin = Status.EXPORTED, "ABC Traders", "36AAACS1234A1Z3"
    session.commit()
    gmail.handle_event(session, event(users["priya"], message_id="m2", subject="Bill for October", attachments=[
        {"filename": "inv2.pdf", "attachmentId": "A2"}]))
    hints = gmail.email_hints(session, session.get(Invoice, 2))
    assert "Subject: Bill for October" in hints and "Body: Please find attached." in hints
    assert "Earlier approved invoices from this sender were from ABC Traders (GSTIN 36AAACS1234A1Z3)" in hints


def test_public_mail_sender_matches_by_exact_address(users, fake, session):
    for i, (sender, gstin) in enumerate([("A <a@gmail.com>", "36AAACS1234A1Z3"),
                                         ("B <b@gmail.com>", "29AAACS1234A1ZY")], start=1):
        session.add(Invoice(user_id=users["priya"].id, source="gmail", original_filename=f"{i}.pdf",
                            file_path=f"{i}.pdf", file_hash=f"{i:064d}", status=Status.EXPORTED,
                            sender=sender, supplier_name=f"S{i}", supplier_gstin=gstin))
    session.commit()
    assert gmail.known_supplier(session, "A <a@gmail.com>") == ("S1", "36AAACS1234A1Z3")
    assert gmail.known_supplier(session, "C <c@gmail.com>") is None


# ---------- password-protected PDFs

def test_protected_pdf_waits_for_password_then_is_read(users, monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "page_count", lambda path: 1)
    seen = []

    def run_extract(path, name=None, **kw):
        images, text, pages = extract.prepare_pages(path, kw.get("passwords"))   # real password handling
        seen.append(text)
        return make_extracted()

    monkeypatch.setattr(worker, "extract_invoice", run_extract)
    h = {"X-User-Id": str(users["priya"].id)}
    with TestClient(app) as client:
        r = client.post("/api/v1/invoices/upload", headers=h,
                        files={"file": ("locked.pdf", pdf_bytes("SECRET INVOICE", password="PAN1234"))})
        inv = r.json()["invoice_id"]
        worker.process(worker.claim_next())
        d = client.get(f"/api/v1/invoices/{inv}").json()
        assert d["status"] == Status.NEEDS_PASSWORD and "password" in d["status_reason"]
        assert client.post(f"/api/v1/invoices/{inv}/password", json={"password": "wrong"}, headers=h).status_code == 400
        r = client.post(f"/api/v1/invoices/{inv}/password", json={"password": "PAN1234"}, headers=h)
        assert r.json()["status"] == Status.RECEIVED
        worker.process(worker.claim_next())
        assert "SECRET INVOICE" in seen[-1]                      # read with the password
        assert client.get(f"/api/v1/invoices/{inv}").json()["status"] == Status.APPROVED
        # The next PDF protected with the same password opens by itself
        r = client.post("/api/v1/invoices/upload", headers=h,
                        files={"file": ("locked2.pdf", pdf_bytes("SECOND", password="PAN1234"))})
        worker.process(worker.claim_next())
        assert seen[-1].strip().endswith("SECOND")              # opened with the remembered password
    with SessionLocal() as s:
        assert [p.password for p in s.scalars(select(PdfPassword))] == ["PAN1234"]


# ---------- API

def test_email_log_api(users, fake):
    fake.files["A1"] = pdf_bytes()
    with SessionLocal() as s:
        gmail.handle_event(s, event(users["priya"]))
    with TestClient(app) as client:
        [e] = client.get("/api/v1/emails").json()
        assert e["outcome"] == "done" and e["attachments"][0]["invoices"] == [{"invoice_id": 1, "status": "RECEIVED"}]
        assert client.get(f"/api/v1/emails/{e['id']}").json()["body_snippet"] == "Please find attached."
        assert client.get("/api/v1/emails?outcome=failed").json() == []
        r = client.post("/api/v1/invoices/upload", headers={"X-User-Id": str(users["priya"].id)},
                        files={"file": ("bundle.zip", _zip({"a.pdf": pdf_bytes("A"), "b.pdf": pdf_bytes("B")}))})
        assert [d["file_name"] for d in r.json()["documents"]] == ["a.pdf", "b.pdf"]


def _zip(files: dict) -> bytes:
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return z.getvalue()


def test_emails_processed_before_the_log_existed_are_recorded(users, fake, session):
    """Upgrading: attachments already turned into invoices must show in the log, not 'no documents'."""
    same = pdf_bytes("one")                    # one file, sent in two emails
    fake.files.update({"A1": same, "A2": same})
    gmail.handle_event(session, event(users["priya"], message_id="m1", attachments=[
        {"filename": "inv.pdf", "attachmentId": "A1"}]))
    gmail.handle_event(session, event(users["priya"], message_id="m2", attachments=[
        {"filename": "same.pdf", "attachmentId": "A2"}]))
    session.query(EmailMessage).delete()      # as if the log was added after these emails
    session.commit()
    for mid, att in (("m1", "inv.pdf"), ("m2", "same.pdf")):
        gmail.handle_event(session, event(users["priya"], message_id=mid, attachments=[
            {"filename": att, "attachmentId": "A1" if mid == "m1" else "A2"}]))
    assert by_result(email_row(session, "m1"))["inv.pdf"] | {"ref": None} == {
        "ref": None, "name": "inv.pdf", "result": "invoice", "invoice_id": 1}
    assert by_result(email_row(session, "m2"))["same.pdf"]["result"] == "same_file"
    assert email_row(session, "m1").outcome == email_row(session, "m2").outcome == "done"


# ---------- the same email handled twice at the same moment

def test_same_email_handled_twice_at_once(users, fake):
    """Real case: one new-email event delivered twice within a second -> UniqueViolation on the
    email log. Now: no error, one email row, one invoice."""
    import threading
    import time as _time
    fake.files["A1"] = pdf_bytes()
    slow = fake.download

    def slow_download(url):              # keep the first handler busy while the second arrives
        _time.sleep(0.3)
        return slow(url)

    gmail.download = slow_download
    errors, results = [], []

    def handle():
        try:
            with SessionLocal() as s:
                results.append(gmail.handle_event(s, event(users["priya"])))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=handle) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert errors == []
    assert sorted(len(r) for r in results) == [0, 1]       # one created the invoice, the other found it done
    with SessionLocal() as s:
        assert s.query(EmailMessage).count() == 1 and s.query(Invoice).count() == 1
        assert s.query(EmailMessage).one().outcome == "done"


def real_event(message_id: str) -> dict:
    """The shape Composio's SDK really delivers: "id" is the TRIGGER id, the same for every email."""
    return {"id": "ti_DSL35Qf4FzFB", "uuid": "ti_DSL35Qf4FzFB", "user_id": "ap-user-1",
            "trigger_slug": "GMAIL_NEW_GMAIL_MESSAGE", "payload": {"id": message_id, "message_id": message_id}}


def test_every_new_email_is_handled_not_only_the_first(monkeypatch):
    """Real bug: duplicates were recognised by event["id"], which is the trigger id, so every
    email after the first was dropped as a 'repeat' (the automation worked once, then stopped)."""
    from app import gmail_listener
    handled = []
    monkeypatch.setattr(gmail_listener, "handle_event", lambda s, ev: handled.append(ev["payload"]["id"]) or [])
    monkeypatch.setattr(gmail_listener, "_recent_events", gmail_listener.OrderedDict())
    for mid in ("m1", "m2", "m3"):
        gmail_listener.on_event(real_event(mid))
    gmail_listener.on_event(real_event("m2"))           # the same email delivered again
    assert handled == ["m1", "m2", "m3"]
    assert gmail_listener.status["last_event_at"] is not None


# ---------- listener watchdog and status

class _StuckSub:
    """A subscription whose realtime connection never gets back to 'connected'."""
    def __init__(self, state):
        self._connection = SimpleNamespace(state=state)
        self.stopped = False
    def handle(self, **kw):
        return lambda fn: fn
    def is_alive(self):
        return not self.stopped
    def has_errored(self):
        return False
    def stop(self):
        self.stopped = True


def _run(monkeypatch, subs, seconds):
    import threading
    import time as _time
    from app import gmail_listener
    syncs = []
    monkeypatch.setattr(gmail_listener, "sync_all", lambda: syncs.append(_time.monotonic()) or True)
    monkeypatch.setattr(gmail_listener, "update_all_labels", lambda: None)
    monkeypatch.setattr(gmail_listener, "RECONNECT_DELAYS", (0.05, 0.1))
    it = iter(subs)
    monkeypatch.setattr(gmail_listener, "_composio",
                        lambda: SimpleNamespace(triggers=SimpleNamespace(subscribe=lambda: next(it))))
    stop = threading.Event()
    t = threading.Thread(target=gmail_listener.run, args=(stop,))
    t.start()
    _time.sleep(seconds)
    stop.set()
    t.join(5)
    return syncs


def test_connection_stuck_reconnecting_is_replaced(monkeypatch):
    """A connection that stays 'connecting'/'failed' without erroring is dropped and replaced,
    and a sync fetches what was missed."""
    from app import gmail_listener
    monkeypatch.setattr(gmail_listener, "NOT_CONNECTED_LIMIT_SECONDS", 0.3)
    stuck, healthy = _StuckSub("connecting"), _StuckSub("connected")
    syncs = _run(monkeypatch, [stuck, healthy], seconds=2.5)
    assert stuck.stopped
    assert len(syncs) >= 2                                            # start + after reconnecting


def test_connection_is_refreshed_regularly(monkeypatch):
    from app import gmail_listener
    monkeypatch.setattr(gmail_listener, "REFRESH_EVERY_MINUTES", 0.01)   # 0.6 s
    first, second = _StuckSub("connected"), _StuckSub("connected")
    syncs = _run(monkeypatch, [first, second] + [_StuckSub("connected") for _ in range(5)], seconds=2.0)
    assert first.stopped and len(syncs) >= 2
    assert gmail_listener.status["reconnects"] >= 1


def test_gmail_status_endpoint(users):
    with TestClient(app) as client:
        st = client.get("/api/v1/gmail/status").json()
    assert set(st["listener"]) >= {"running", "connected", "last_event_at", "last_sync_at", "reconnects"}
    assert st["sync_every_minutes"] == 5
    assert [i["user_id"] for i in st["inboxes"]] == [users["priya"].id]     # only connected inboxes
