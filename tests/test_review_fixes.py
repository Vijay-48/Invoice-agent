# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""Regression tests for problems found in the code review (October 2026)."""
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import update

from app import assistant, excel, worker
from app.api import app
from app.db import SessionLocal
from app.models import ChatMessage, Invoice, Status
from app.pipeline.extract import AIServiceUnavailable
from app.pipeline.validate import normalize_extracted
from tests.conftest import make_extracted
from tests.test_assistant import FakeStreamAgent, sse_events
from tests.test_flow import client, fake_extract, get, h, run_worker, upload   # noqa: F401 (fixtures)


# ---------- worker

def test_requeued_invoice_goes_behind_the_others(client, users, fake_extract, monkeypatch):
    """A document the AI service keeps failing on must not hold up the whole queue."""
    monkeypatch.setattr(worker, "AI_DOWN_PAUSE_SECONDS", 0)
    first = upload(client, users["priya"], b"%PDF one")["invoice_id"]
    second = upload(client, users["priya"], b"%PDF two")["invoice_id"]
    fake_extract.append(AIServiceUnavailable("timeout"))
    assert worker.claim_next() == first
    assert worker.process(first) is False
    assert worker.claim_next() == second          # not the same one again


def test_late_ai_result_does_not_overwrite_another_worker(client, users, fake_extract, monkeypatch):
    """The AI took so long that the invoice was reclaimed and claimed by another worker:
    this worker's result is dropped."""
    inv_id = upload(client, users["priya"])["invoice_id"]
    assert worker.claim_next() == inv_id

    def slow_extract(path, name=None, **kw):
        with SessionLocal() as s:   # meanwhile: reclaimed and claimed again elsewhere
            s.execute(update(Invoice).where(Invoice.id == inv_id).values(
                attempts=Invoice.attempts + 1, updated_at=datetime.now(timezone.utc)))
            s.commit()
        return make_extracted()
    monkeypatch.setattr(worker, "extract_invoice", slow_extract)
    worker.process(inv_id)
    assert get(client, inv_id)["status"] == Status.PROCESSING   # left to the other worker


# ---------- checks

def test_zero_total_is_never_auto_approved(client, users, fake_extract):
    fake_extract.append(make_extracted(lines=[dict(description="Free sample", hsn_sac="7306", quantity=1,
                                                   unit_price=0, taxable_value=0, gst_rate=18)]))
    inv_id = upload(client, users["priya"])["invoice_id"]
    run_worker()
    d = get(client, inv_id)
    assert d["status"] == Status.NEEDS_REVIEW and "Total is 0.00" in d["status_reason"]


def test_place_of_supply_written_loosely_still_matches():
    for written in ("36", "36-Telangana", "36 Telangana"):
        assert normalize_extracted(make_extracted(place_of_supply_state_code=written)).place_of_supply_state_code == "36"
    assert normalize_extracted(make_extracted(place_of_supply_state_code="7")).place_of_supply_state_code == "07"


def test_unregistered_suppliers_are_not_duplicates_of_each_other(client, users, fake_extract):
    """Two suppliers without GSTIN, same invoice number, date and amount: different suppliers."""
    fake_extract += [make_extracted(supplier_gstin=None, supplier_name="Ravi Hardware", invoice_number="1"),
                     make_extracted(supplier_gstin=None, supplier_name="Kumar Traders", invoice_number="1"),
                     make_extracted(supplier_gstin=None, supplier_name="Ravi Hardware", invoice_number="1")]
    a = upload(client, users["priya"], b"%PDF a")["invoice_id"]
    b = upload(client, users["priya"], b"%PDF b")["invoice_id"]
    c = upload(client, users["priya"], b"%PDF c")["invoice_id"]
    run_worker()
    assert get(client, b)["status"] != Status.DUPLICATE
    assert get(client, c)["status"] == Status.DUPLICATE and f"#{a}" in get(client, c)["status_reason"]


# ---------- Excel

def _inv(data, **kw):
    d = normalize_extracted(data).model_dump()
    return SimpleNamespace(id=1, extracted=d, invoice_date=d["invoice_date"], approved_by="auto",
                           approved_at=None, source="gmail", sender=kw.get("sender"), original_filename="x.pdf")


def test_excel_text_is_made_safe():
    row, lines = excel._rows(_inv(make_extracted(supplier_name="=HYPERLINK(\"http://x\")\x07Steels")), None)
    assert row[1] == "'=HYPERLINK(\"http://x\")Steels"      # text, not a formula; control character gone


def test_credit_note_is_negative_whatever_sign_the_ai_used():
    negative = make_extracted(document_type="credit_note", lines=[dict(
        description="Return", hsn_sac="7306", quantity=-2, unit_price=450, taxable_value=-900, gst_rate=18)])
    positive = make_extracted(document_type="credit_note", lines=[dict(
        description="Return", hsn_sac="7306", quantity=2, unit_price=450, taxable_value=900, gst_rate=18)])
    for data in (negative, positive):
        row, lines = excel._rows(_inv(data), None)
        assert row[8] < 0 and row[13] < 0 and lines[0][6] < 0 and lines[0][8] < 0


def test_one_unwritable_invoice_does_not_block_the_others(client, users, fake_extract, monkeypatch):
    fake_extract += [make_extracted(invoice_number="A-1"), make_extracted(invoice_number="B-2")]
    a = upload(client, users["priya"], b"%PDF a")["invoice_id"]
    b = upload(client, users["priya"], b"%PDF b")["invoice_id"]
    real_rows = excel._rows

    def rows(inv, user):
        if inv.id == a:
            raise KeyError("supplier_name")
        return real_rows(inv, user)
    monkeypatch.setattr(excel, "_rows", rows)
    run_worker()
    assert get(client, b)["status"] == Status.EXPORTED
    assert get(client, a)["status"] == Status.NEEDS_REVIEW and "Excel" in get(client, a)["status_reason"]


# ---------- API

def test_second_decision_on_the_same_invoice_is_refused(client, users, fake_extract):
    fake_extract.append(make_extracted(grand_total=6163))      # doesn't add up: review
    inv_id = upload(client, users["priya"])["invoice_id"]
    run_worker()
    assert client.post(f"/api/v1/invoices/{inv_id}/reject", headers=h(users["ravi"]), json={}).status_code == 200
    assert client.post(f"/api/v1/invoices/{inv_id}/approve", headers=h(users["priya"]), json={}).status_code == 409


def test_blank_question_and_chat_deleted_while_answering(users, monkeypatch):
    hdr = {"X-User-Id": str(users["priya"].id)}
    with TestClient(app) as c:
        cid = c.post("/api/v1/assistant/sessions", headers=hdr).json()["id"]
        assert c.post(f"/api/v1/assistant/sessions/{cid}/stream", headers=hdr, json={"content": "   "}).status_code == 422

        class DeletingAgent(FakeStreamAgent):
            def run(self, input, **kw):
                with SessionLocal() as s:   # the user deletes the chat while the answer is written
                    s.query(ChatMessage).filter(ChatMessage.session_id == cid).delete()
                    s.execute(update(Invoice).values())   # no-op, keeps the session busy like a real tool
                    from app.models import ChatSession
                    s.delete(s.get(ChatSession, cid))
                    s.commit()
                yield from super().run(input, **kw)
        monkeypatch.setattr(assistant, "_agent", lambda: DeletingAgent())
        r = c.post(f"/api/v1/assistant/sessions/{cid}/stream", headers=hdr, json={"content": "hi"})
        evs = sse_events(r.text)
        assert r.status_code == 200 and evs[-1] == {"type": "error", "message": "This chat was deleted"}


def test_assistant_knows_todays_date(monkeypatch):
    """The agent is rebuilt each day, so 'today' in its instructions is never stale."""
    assistant._agent_for.cache_clear()
    from datetime import date
    first = assistant._agent()
    assert assistant._agent() is first
    assert assistant._agent_for(date(2030, 1, 1)) is not first
