"""API + worker + ingest + ledger, end to end with a fake extractor."""
import base64
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app import worker
from app.api import app
from app.db import SessionLocal
from app.ingest import common, gmail_poller
from app.ledger import safe_name
from app.models import Invoice, Status
from app.pipeline import extract
from tests.conftest import make_extracted, ledger_path, SUPPLIER_GSTIN


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_extract(monkeypatch):
    """Each call pops the next ExtractedInvoice (or exception) from the queue."""
    queue = []

    def _extract(path):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(worker, "extract_invoice", _extract)
    monkeypatch.setattr(worker, "page_count", lambda path: 1)   # uploads here aren't real PDFs
    return queue


def upload(client, content=b"%PDF-1.4 fake", name="inv.pdf"):
    r = client.post("/invoices/upload", files={"file": (name, content, "application/pdf")})
    assert r.status_code == 201, r.text
    return r.json()


def run_worker():
    while (inv_id := worker.claim_next()) is not None:
        worker.process(inv_id)


def status(client, inv_id):
    return client.get(f"/invoices/{inv_id}").json()["status"]


# ---------- upload / ingest

def test_upload_validation(client):
    assert client.post("/invoices/upload", files={"file": ("a.docx", b"x")}).status_code == 400
    first = upload(client, b"same bytes")
    again = upload(client, b"same bytes")
    assert again == {**first, "duplicate_file": True}
    assert client.get("/invoices/999").status_code == 404


def test_concurrent_same_file_ingest(master):
    """Bug: two simultaneous ingests of the same file raised a unique-key error."""
    results, errors = [], []
    barrier = threading.Barrier(4)

    def go():
        try:
            with SessionLocal() as s:
                barrier.wait()
                inv, created = common.ingest_file(s, b"racing bytes", "r.pdf", "upload")
                results.append((inv.id, created))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert len({i for i, _ in results}) == 1 and sum(c for _, c in results) == 1


# ---------- pipeline outcomes

def test_auto_post_writes_ledgers(client, master, fake_extract):
    fake_extract.append(make_extracted())
    inv_id = upload(client)["id"]
    run_worker()

    detail = client.get(f"/invoices/{inv_id}").json()
    assert detail["status"] == Status.POSTED, detail
    assert [a["stage"] for a in detail["audit"]] == ["ingest", "extract", "decide", "post"]

    for path in [ledger_path("daybook.md"), ledger_path("users", "system.md"),
                 ledger_path("suppliers", f"{safe_name(SUPPLIER_GSTIN)}.md")]:
        text = path.read_text(encoding="utf-8")
        assert f"<!-- voucher:{inv_id} -->" in text and "6,136.00" in text

    assert client.get("/ledgers").json() == {
        "daybook": True, "suppliers": [safe_name(SUPPLIER_GSTIN)], "users": ["system"]}
    assert "SLS/26-27/118" in client.get("/ledgers/users/system").text
    assert client.get("/ledgers/users/nobody").status_code == 404
    assert client.get("/ledgers/secret/x").status_code == 404
    assert client.get("/ledgers/users/..%2F..%2Fdaybook").status_code == 404


def test_approval_flow_posts_under_reviewer(client, master, fake_extract):
    fake_extract.append(make_extracted(po_number=None))        # non-PO -> needs approval
    inv_id = upload(client)["id"]
    run_worker()
    assert status(client, inv_id) == Status.PENDING_APPROVAL
    assert not ledger_path("daybook.md").exists()

    r = client.post(f"/invoices/{inv_id}/approve", json={"reviewer": "Priya Sharma", "note": "ok"})
    assert r.status_code == 200 and r.json()["status"] == Status.POSTED
    assert "approved by Priya Sharma" in ledger_path("users", "priya_sharma.md").read_text(encoding="utf-8")

    assert client.post(f"/invoices/{inv_id}/approve", json={"reviewer": "x"}).status_code == 409
    assert client.post(f"/invoices/{inv_id}/post", json={"reviewer": "x"}).status_code == 409


def test_exception_reject_and_reprocess(client, master, fake_extract):
    fake_extract += [make_extracted(supplier_gstin="27AAACS1234A1Z7"), make_extracted()]
    inv_id = upload(client)["id"]
    run_worker()
    assert status(client, inv_id) == Status.EXCEPTION
    assert client.post(f"/invoices/{inv_id}/approve", json={"reviewer": "x"}).status_code == 409

    # Master data fixed -> reprocess -> passes
    assert client.post(f"/invoices/{inv_id}/reprocess", json={"reviewer": "ops"}).json()["status"] == Status.RECEIVED
    run_worker()
    assert status(client, inv_id) == Status.POSTED

    fake_extract.append(make_extracted(invoice_number="OTHER-1", grand_total=1))
    other = upload(client, b"another file")["id"]
    run_worker()
    assert status(client, other) == Status.EXCEPTION
    assert client.post(f"/invoices/{other}/reject", json={"reviewer": "ops"}).json()["status"] == Status.REJECTED


def test_duplicate_invoice_different_file(client, master, fake_extract):
    fake_extract += [make_extracted(), make_extracted(invoice_number="sls/26-27/118 ")]
    first = upload(client, b"scan 1")["id"]
    second = upload(client, b"scan 2 of the same invoice")["id"]
    run_worker()
    assert status(client, first) == Status.POSTED
    detail = client.get(f"/invoices/{second}").json()
    assert detail["status"] == Status.EXCEPTION
    assert any(c["name"] == "duplicate" and c["status"] == "FAIL" for c in detail["checks"])


def test_retries_then_failed(client, master, fake_extract):
    fake_extract += [RuntimeError("openai down")] * worker.MAX_ATTEMPTS
    inv_id = upload(client)["id"]
    run_worker()
    detail = client.get(f"/invoices/{inv_id}").json()
    assert detail["status"] == Status.FAILED and "openai down" in detail["error"]
    assert client.post(f"/invoices/{inv_id}/reprocess", json={"reviewer": "ops"}).status_code == 200


def test_messy_llm_values_do_not_crash(client, master, fake_extract):
    """Bug: a GSTIN with spaces or an unconverted date overflowed the DB columns,
    so a readable invoice ended up FAILED instead of EXCEPTION."""
    fake_extract.append(make_extracted(supplier_gstin="36 AAACS 1234 A1Z3",
                                       invoice_date="15th September 2026"))
    inv_id = upload(client)["id"]
    run_worker()
    detail = client.get(f"/invoices/{inv_id}").json()
    assert detail["status"] == Status.EXCEPTION, detail
    assert detail["supplier_gstin"] == SUPPLIER_GSTIN


def test_stale_processing_is_reclaimed(client, master, fake_extract):
    """Bug: a worker crash left invoices in PROCESSING forever."""
    inv_id = upload(client)["id"]
    assert worker.claim_next() == inv_id          # claimed, then the worker "crashes"
    assert worker.claim_next() is None
    with SessionLocal() as s:
        s.execute(update(Invoice).where(Invoice.id == inv_id).values(
            updated_at=datetime.now(timezone.utc) - timedelta(hours=1)))
        s.commit()
    fake_extract.append(make_extracted())
    run_worker()
    assert status(client, inv_id) == Status.POSTED


def test_posting_failure_keeps_status_and_retry_works(client, master, fake_extract, monkeypatch):
    from app.pipeline import post as post_mod
    real = post_mod.write_voucher

    def broken(v):
        raise OSError("disk full")

    monkeypatch.setattr(post_mod, "write_voucher", broken)
    fake_extract.append(make_extracted())
    inv_id = upload(client)["id"]
    run_worker()
    detail = client.get(f"/invoices/{inv_id}").json()
    assert detail["status"] == Status.AUTO_POST and "disk full" in detail["error"]

    monkeypatch.setattr(post_mod, "write_voucher", real)
    r = client.post(f"/invoices/{inv_id}/post", json={"reviewer": "ops"})
    assert r.json()["status"] == Status.POSTED and r.json()["error"] is None
    text = ledger_path("daybook.md").read_text(encoding="utf-8")
    assert text.count(f"<!-- voucher:{inv_id} -->") == 1


def test_too_many_pages_needs_approval(client, master, fake_extract, monkeypatch):
    """Bug: pages past MAX_PAGES were silently ignored."""
    monkeypatch.setattr(worker, "page_count", lambda path: 9)
    fake_extract.append(make_extracted())
    inv_id = upload(client)["id"]
    run_worker()
    detail = client.get(f"/invoices/{inv_id}").json()
    assert detail["status"] == Status.PENDING_APPROVAL
    assert any(c["name"] == "pages" for c in detail["checks"])


# ---------- extract (no OpenAI call)

def _pdf(path, pages):
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"TAX INVOICE page {i + 1} total 6136.00")
    doc.save(path)
    doc.close()


def test_prepare_pages_and_extract(tmp_path, monkeypatch):
    pdf = tmp_path / "a.pdf"
    _pdf(pdf, 7)
    images, text = extract.prepare_pages(str(pdf))
    assert len(images) == 5 and "page 5" in text and "page 6" not in text
    assert extract.page_count(str(pdf)) == 7

    png = tmp_path / "b.png"
    png.write_bytes(b"\x89PNG")
    assert extract.prepare_pages(str(png))[1] == "" and extract.page_count(str(png)) == 1

    seen = {}

    def fake_run(prompt, images):
        seen["prompt"], seen["n"] = prompt, len(images)
        return SimpleNamespace(content=make_extracted())

    monkeypatch.setattr(extract.extractor, "run", fake_run)
    assert extract.extract_invoice(str(pdf)).invoice_number == "SLS/26-27/118"
    assert seen["n"] == 5 and "6136.00" in seen["prompt"]

    monkeypatch.setattr(extract.extractor, "run", lambda *a, **k: SimpleNamespace(content="sorry"))
    with pytest.raises(ValueError):
        extract.extract_invoice(str(pdf))


# ---------- Gmail poller (fake API)

class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value() if callable(self.value) else self.value


class FakeGmail:
    def __init__(self, messages):
        self.msgs, self.labels_added = messages, {}

    def users(self): return self
    def messages(self): return self
    def labels(self): return SimpleNamespace(
        list=lambda userId: _Exec({"labels": [{"name": "ap-processed", "id": "L1"}]}),
        create=lambda userId, body: _Exec({"id": "L2"}))
    def attachments(self): return SimpleNamespace(
        get=lambda userId, messageId, id: _Exec({"data": base64.urlsafe_b64encode(b"att:" + id.encode()).decode()}))
    def list(self, userId, q, maxResults): return _Exec({"messages": [{"id": k} for k in self.msgs]})
    def get(self, userId, id, format): return _Exec(self.msgs[id])
    def modify(self, userId, id, body):
        return _Exec(lambda: self.labels_added.setdefault(id, body["addLabelIds"]))


def test_gmail_poll_once():
    svc = FakeGmail({"m1": {"payload": {
        "headers": [{"name": "From", "value": "billing@sls.in"}],
        "parts": [
            {"filename": "", "body": {"data": "aGk="}},                                   # body text
            {"filename": "inv.pdf", "body": {"attachmentId": "A1"}},
            {"filename": "logo.gif", "body": {"attachmentId": "A2"}},                     # unsupported
            {"filename": "", "parts": [{"filename": "scan.jpg",
                                        "body": {"data": base64.urlsafe_b64encode(b"jpg").decode()}}]},
        ]}}})
    assert gmail_poller.get_or_create_label(svc, "ap-processed") == "L1"
    assert gmail_poller.poll_once(svc, "L1") == 2
    assert svc.labels_added == {"m1": ["L1"]}
    assert gmail_poller.poll_once(svc, "L1") == 0      # same files again -> deduped
    with SessionLocal() as s:
        rows = s.query(Invoice).order_by(Invoice.id).all()
        assert [(r.original_filename, r.source, r.source_ref) for r in rows] == [
            ("inv.pdf", "gmail", "m1|billing@sls.in"), ("scan.jpg", "gmail", "m1|billing@sls.in")]


# ---------- two workers at once

def _process_in_parallel(client, monkeypatch, invoices):
    """Run one thread per invoice; the fake extractor holds them all until each has
    extracted, so validation and matching overlap."""
    barrier = threading.Barrier(len(invoices))
    by_file = {}

    def _extract(path):
        barrier.wait(timeout=10)
        return by_file[path]

    monkeypatch.setattr(worker, "extract_invoice", _extract)
    monkeypatch.setattr(worker, "page_count", lambda path: 1)
    ids = []
    for i, data in enumerate(invoices):
        inv_id = upload(client, f"parallel file {i}".encode())["id"]
        with SessionLocal() as s:
            by_file[s.get(Invoice, inv_id).file_path] = data
        ids.append(inv_id)
    claimed = [worker.claim_next() for _ in ids]
    threads = [threading.Thread(target=worker.process, args=(i,)) for i in claimed]
    [t.start() for t in threads]
    [t.join() for t in threads]
    return sorted(status(client, i) for i in ids)


def test_parallel_duplicates_only_one_passes(client, master, monkeypatch):
    assert _process_in_parallel(client, monkeypatch, [make_extracted(), make_extracted()]) == [
        Status.EXCEPTION, Status.POSTED]


def test_parallel_overbilling_only_one_passes(client, master, monkeypatch):
    lines = [dict(description="MS Pipe 25mm", hsn_sac="7306", quantity=80, unit_price=100,
                  taxable_value=8000, gst_rate=18)]
    a = make_extracted(lines=lines, invoice_number="A-1")
    b = make_extracted(lines=lines, invoice_number="B-1")
    assert _process_in_parallel(client, monkeypatch, [a, b]) == [Status.EXCEPTION, Status.POSTED]
