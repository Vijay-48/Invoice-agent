# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""Second, guided reading when checks fail (Phase 2 items 2-3), with a fake AI."""
import pytest

from app.config import settings
from app.models import Status
from app.pipeline import reading
from tests.conftest import make_extracted
from tests.test_flow import client, fake_extract, get, run_worker, upload   # noqa: F401 (fixtures)

MISREAD = dict(grand_total=6163)          # lines and taxes add up to 6,136: the total was misread


class FakeAI:
    def __init__(self, *readings):
        self.readings, self.calls = list(readings), []

    def __call__(self, path, name=None, **kw):
        self.calls.append(kw)
        item = self.readings.pop(0)
        if isinstance(item, Exception):
            raise item
        item._model = kw.get("model") or "gpt-4o-mini"
        item._usage = {"input_tokens": 100, "output_tokens": 10}
        return item


@pytest.fixture(autouse=True)
def second_pass_on(monkeypatch):
    monkeypatch.setattr(settings, "ai_second_pass", True)
    monkeypatch.setattr(settings, "ai_second_pass_model", "gpt-4o")


def test_misread_total_is_fixed_by_a_second_reading():
    ai = FakeAI(make_extracted(**MISREAD), make_extracted())
    r = reading.read_document("x.pdf", "x.pdf", extract_fn=ai)
    assert r.data.grand_total == 6136 and r.second_pass["fixed"] == ["grand_total"]
    assert ai.calls[1]["model"] == "gpt-4o" and "expected 6136.00, got 6163.00" in ai.calls[1]["feedback"]
    assert "never change a number just to make the totals add up" in ai.calls[1]["feedback"]
    assert r.data._usage["input_tokens"] == 200 and r.data._model == "gpt-4o-mini + gpt-4o"
    assert r.checks[-1].name == "second_reading" and "fixed: grand_total" in r.checks[-1].message


def test_same_problem_twice_keeps_the_first_reading():
    first = make_extracted(**MISREAD)
    r = reading.read_document("x.pdf", "x.pdf", extract_fn=FakeAI(first, make_extracted(**MISREAD)))
    assert r.data is first and r.second_pass["fixed"] == []
    assert "probably in the document itself" in r.checks[-1].message


def test_clean_reading_costs_one_call():
    ai = FakeAI(make_extracted())
    r = reading.read_document("x.pdf", "x.pdf", extract_fn=ai)
    assert len(ai.calls) == 1 and r.second_pass is None and r.checks == []


def test_business_rule_failures_do_not_trigger_a_second_reading():
    """Over the auto-approve limit, a credit note, a USD invoice: reading again changes nothing."""
    ai = FakeAI(make_extracted(currency="USD"))
    reading.read_document("x.pdf", "x.pdf", extract_fn=ai)
    assert len(ai.calls) == 1


def test_second_reading_failing_keeps_the_first(caplog):
    first = make_extracted(**MISREAD)
    r = reading.read_document("x.pdf", "x.pdf", extract_fn=FakeAI(first, RuntimeError("Connection error.")))
    assert r.data is first and "failed" in r.checks[-1].message


def _pdf(text: str) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_worker_uses_the_better_reading(client, users, fake_extract):
    """The corrected total is printed in the document: fixed without a person."""
    fake_extract += [make_extracted(**MISREAD), make_extracted()]
    inv_id = upload(client, users["priya"], _pdf("TAX INVOICE   Grand Total 6,136.00"))["invoice_id"]
    run_worker()
    d = get(client, inv_id)
    assert d["status"] == Status.EXPORTED
    assert any("Second reading: fixed grand_total" in a["message"] for a in d["audit"])


def test_second_reading_amounts_not_in_the_document_go_to_review(client, users, fake_extract):
    """The AI changed the total to one the document's text doesn't show: it may have made the numbers
    add up instead of reading them, so a person confirms."""
    fake_extract += [make_extracted(**MISREAD), make_extracted()]
    inv_id = upload(client, users["priya"], _pdf("TAX INVOICE   Grand Total 6,163.00"))["invoice_id"]
    run_worker()
    d = get(client, inv_id)
    assert d["status"] == Status.NEEDS_REVIEW and "please confirm the amounts" in d["status_reason"]


def test_second_reading_cannot_escape_the_checks_by_changing_the_document():
    """Becoming a USD invoice (no Indian GST checks) or another supplier doesn't 'fix' anything."""
    for second in (make_extracted(currency="USD"), make_extracted(document_type="other"),
                   make_extracted(supplier_gstin="29AAACI4625C1ZX")):
        first = make_extracted(**MISREAD)
        r = reading.read_document("x.pdf", "x.pdf", extract_fn=FakeAI(first, second))
        assert r.data is first and r.second_pass["fixed"] == [] and "first reading is kept" in r.checks[-1].message


def test_nothing_to_read_better_means_no_second_call():
    """A missing GSTIN (unregistered supplier) or a foreign invoice: a second reading can't help."""
    for data in (make_extracted(supplier_gstin=None), make_extracted(currency="USD", supplier_gstin=None,
                                                                     buyer_gstin=None, **MISREAD)):
        ai = FakeAI(data)
        reading.read_document("x.pdf", "x.pdf", extract_fn=ai)
        assert len(ai.calls) == 1


def test_reviewer_corrections_are_remembered_per_supplier(client, users, fake_extract, monkeypatch):
    """A reviewer corrected the date on an earlier invoice from this supplier: the next second
    reading of that supplier's document is told about it."""
    from app.db import SessionLocal
    from app.pipeline.learning import correction_notes
    fake_extract.append(make_extracted(grand_total=6163, invoice_date="2026-03-09"))     # to review
    inv_id = upload(client, users["priya"])["invoice_id"]
    run_worker()
    r = client.patch(f"/api/v1/invoices/{inv_id}", headers={"X-User-Id": str(users["priya"].id)},
                     json={"grand_total": 6136, "invoice_date": "2026-09-03"})
    assert r.status_code == 200
    with SessionLocal() as s:
        notes = correction_notes(s, make_extracted().supplier_gstin)
    assert "invoice_date was read as '2026-03-09', the correct value was '2026-09-03'" in notes
    ai = FakeAI(make_extracted(**MISREAD, invoice_number="X-2"), make_extracted(invoice_number="X-2"))
    with SessionLocal() as s:
        reading.read_document("x.pdf", "x.pdf", extract_fn=ai, supplier_notes=lambda g: correction_notes(s, g))
    assert "the correct value was '2026-09-03'" in ai.calls[1]["feedback"]
