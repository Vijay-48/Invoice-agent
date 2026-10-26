# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""Cross-checks on the document itself (Phase 2 item 8): total in words, look-alike invoice numbers."""
import pytest

from app.models import Invoice, Status
from app.pipeline.validate import validate
from app.pipeline.words import parse_amount
from tests.conftest import make_extracted
from tests.test_flow import client, fake_extract, get, run_worker, upload   # noqa: F401 (fixtures)


@pytest.mark.parametrize("text, amount", [
    ("TEN LAKH SIXTY FIVE THOUSAND NINE HUNDRED EIGHT RUPEES SIXTEEN PAISE", 1065908.16),
    ("Rupees Six Thousand One Hundred Thirty Six Only", 6136),
    ("INR Two Crore Five Lakh Ninety-Nine Thousand and Fifty Only", 20599050),
    ("Rupees One Hundred and One and Fifty Paise Only", 101.5),
    ("Rs. 10,65,908.16 only", 1065908.16),
    ("US Dollars Ninety Three and Fifty Cents", 93.5),
    ("Only", None), (None, None),
])
def test_amount_in_words(text, amount):
    assert parse_amount(text) == amount


def _check(data, name):
    return next((c for c in validate(None, Invoice(id=0), data).checks if c.name == name), None)


def test_total_in_words_catches_a_misread_figure():
    assert _check(make_extracted(amount_in_words="Rupees Six Thousand One Hundred Thirty Six Only"),
                  "amount_in_words").status == "PASS"
    bad = _check(make_extracted(amount_in_words="Rupees Six Thousand One Hundred Sixty Three Only"), "amount_in_words")
    assert bad.status == "FAIL" and "6,163.00" in bad.message
    assert _check(make_extracted(amount_in_words=None), "amount_in_words") is None


def test_look_alike_invoice_number_goes_to_review(client, users, fake_extract):
    fake_extract += [make_extracted(invoice_number="SLS/26-27/1O8"), make_extracted(invoice_number="SLS/26-27/108",
                                                                                     grand_total=6136)]
    first = upload(client, users["priya"], b"%PDF a")["invoice_id"]
    second = upload(client, users["priya"], b"%PDF b")["invoice_id"]
    run_worker()
    assert get(client, first)["status"] == Status.EXPORTED
    d = get(client, second)
    assert d["status"] == Status.NEEDS_REVIEW and "looks like SLS/26-27/1O8" in d["status_reason"]


# ---------- HSN-wise tax summary

def _hsn(rows, **kw):
    return make_extracted(hsn_summary=rows, **kw)


def test_hsn_summary_agrees_with_lines():
    ok = _hsn([dict(hsn_sac="7306", taxable_value=4500, tax_amount=810),
               dict(hsn_sac="7307", taxable_value=700, tax_amount=126)])
    assert _check(ok, "hsn_summary").status == "PASS"
    assert _check(make_extracted(), "hsn_summary") is None   # no summary printed, no check


def test_hsn_summary_catches_a_misread_figure():
    bad = _hsn([dict(hsn_sac="7306", taxable_value=4600, tax_amount=810),
                dict(hsn_sac="7307", taxable_value=700, tax_amount=126)])
    c = _check(bad, "hsn_summary")
    assert c.status == "FAIL" and "HSN 7306" in c.message and "4600.00" in c.message
    wrong_tax = _hsn([dict(hsn_sac="7306", taxable_value=4500, tax_amount=800),
                      dict(hsn_sac="7307", taxable_value=700, tax_amount=126)])
    assert "tax 926.00" in _check(wrong_tax, "hsn_summary").message


def test_hsn_summary_prefix_match():
    lines = [dict(description="Cable", hsn_sac="85444999", quantity=1, unit_price=1000, taxable_value=1000, gst_rate=18)]
    ok = make_extracted(lines=lines, hsn_summary=[dict(hsn_sac="8544", taxable_value=1000, tax_amount=180)])
    assert _check(ok, "hsn_summary").status == "PASS"


# ---------- page selection

def test_select_pages_keeps_first_last_and_amount_pages():
    from app.pipeline.extract import select_pages
    assert select_pages(["x"] * 3, 3, 5) == [0, 1, 2]
    texts = ["header", "1,000.00 2,000.00 3,000.00", "Terms and Conditions of sale apply", "plain text",
             "4,000.00 5,000.00", "6,000.00", "totals 9,000.00"]
    assert select_pages(texts, len(texts), 5) == [0, 1, 4, 5, 6]   # terms page and plain page skipped


def test_pages_check_names_the_pages_read():
    from app.pipeline.evaluate import pages_check
    c = pages_check(9, 5, [1, 2, 5, 8, 9])[0]
    assert c.status == "WARN" and "pages 1, 2, 5, 8, 9 of 9" in c.message
    assert pages_check(3, 5) == []


# ---------- supplier history

def _approve(client, users, fake_extract, **kw):
    fake_extract.append(make_extracted(**kw))
    inv = upload(client, users["priya"], f"%PDF {kw.get('invoice_number')}".encode())["invoice_id"]
    run_worker()
    return inv


def test_new_supplier_is_noted_but_not_blocked(client, users, fake_extract):
    inv = _approve(client, users, fake_extract)
    d = get(client, inv)
    assert d["status"] == Status.EXPORTED
    assert next(c for c in d["checks"] if c["name"] == "supplier_history")["message"].startswith("New supplier")


def test_known_supplier_with_a_different_name_goes_to_review(client, users, fake_extract):
    _approve(client, users, fake_extract, invoice_number="A/1")
    inv = _approve(client, users, fake_extract, invoice_number="A/2", supplier_name="Totally Other Traders")
    d = get(client, inv)
    assert d["status"] == Status.NEEDS_REVIEW and "approved before as 'Sri Lakshmi Steels Pvt Ltd'" in d["status_reason"]
    # Same company written differently is fine
    ok = _approve(client, users, fake_extract, invoice_number="A/3", supplier_name="SRI LAKSHMI STEELS PRIVATE LIMITED")
    assert get(client, ok)["status"] == Status.EXPORTED


def test_amount_far_above_the_usual_goes_to_review(client, users, fake_extract):
    for n in (1, 2, 3):
        _approve(client, users, fake_extract, invoice_number=f"B/{n}")   # 6,136 each
    big = [dict(description="Steel", hsn_sac="7306", quantity=1, unit_price=20000, taxable_value=20000, gst_rate=18)]
    inv = _approve(client, users, fake_extract, invoice_number="B/4", lines=big)   # 23,600: under the limit, 3.8x usual
    d = get(client, inv)
    assert d["status"] == Status.NEEDS_REVIEW and "far above this supplier's usual" in d["status_reason"]


def test_suppliers_endpoint(client, users, fake_extract):
    _approve(client, users, fake_extract, invoice_number="C/1")
    rows = client.get("/api/v1/suppliers").json()
    assert rows[0]["gstin"] == "36AAACS1234A1Z3" and rows[0]["invoices"] == 1 and rows[0]["highest_total"] == 6136


# ---------- review fixes (D-95)

SUMMARY = [dict(hsn_sac="7306", taxable_value=4500, tax_amount=810), dict(hsn_sac="7307", taxable_value=700, tax_amount=126)]


def test_hsn_summary_split_by_rate_is_summed_per_hsn():
    lines = [dict(description="A", hsn_sac="7306", quantity=1, unit_price=1000, taxable_value=1000, gst_rate=12),
             dict(description="B", hsn_sac="7306", quantity=1, unit_price=2000, taxable_value=2000, gst_rate=18)]
    rows = [dict(hsn_sac="7306", taxable_value=1000, tax_amount=120), dict(hsn_sac="7306", taxable_value=2000, tax_amount=360)]
    assert _check(make_extracted(lines=lines, hsn_summary=rows), "hsn_summary").status == "PASS"


def test_hsn_summary_printed():
    from app.pipeline.extract import hsn_summary_printed
    assert not hsn_summary_printed("SNo Description HSN/SAC Code Quantity ... Grand Total", True)   # line column only
    assert hsn_summary_printed("HSN Qty Rate ...\nHSN Taxable Value CGST SGST", True)               # a second table
    assert hsn_summary_printed("HSN/SAC Qty ...\nHSN-wise Tax Summary", True)
    assert hsn_summary_printed("Description Qty\nHSN/SAC Taxable Value Integrated Tax", False)
    assert not hsn_summary_printed("", False)                                                        # scan: no text


def test_invented_hsn_summary_is_dropped(tmp_path, monkeypatch):
    """Real gpt-4o-mini behaviour (2026-10-26): asked to copy the table, it built one from the lines on
    invoices that print none, so the check would only compare the AI with itself."""
    from types import SimpleNamespace
    import pymupdf
    from app.pipeline import extract
    pdf = tmp_path / "a.pdf"
    doc = pymupdf.open()
    page = doc.new_page()   # enough text not to count as a scan
    page.insert_text((72, 72), "TAX INVOICE  Sri Lakshmi Steels Pvt Ltd  SLS/26-27/118")
    page.insert_text((72, 90), "Description HSN Qty Rate Taxable  MS Pipe 25mm 7306 10 450.00 4,500.00")
    page.insert_text((72, 108), "Grand Total 6,136.00")
    doc.save(pdf)
    doc.close()
    monkeypatch.setattr(extract.extractor, "run", lambda *a, **k: SimpleNamespace(content=make_extracted(hsn_summary=SUMMARY)))
    assert extract.extract_invoice(str(pdf)).hsn_summary == []


def test_second_reading_cannot_drop_or_rewrite_only_the_summary(monkeypatch):
    from app.config import settings
    from app.pipeline import reading
    from tests.test_reading import FakeAI
    monkeypatch.setattr(settings, "ai_second_pass", True)
    wrong = [dict(hsn_sac="7306", taxable_value=4600, tax_amount=810), SUMMARY[1]]
    r = reading.read_document("x.pdf", "x.pdf", extract_fn=FakeAI(make_extracted(hsn_summary=wrong), make_extracted()))
    assert r.data.hsn_summary and "HSN summary" in r.checks[-1].message and r.second_pass["fixed"] == []
    r = reading.read_document("x.pdf", "x.pdf", extract_fn=FakeAI(make_extracted(hsn_summary=wrong),
                                                                  make_extracted(hsn_summary=SUMMARY)))
    assert r.checks[-1].status == "WARN" and "Only the HSN summary changed" in r.checks[-1].message


def test_look_alike_search_runs_in_the_database(session, users):
    """Same supplier, same-length numbers: only the look-alike one comes back."""
    from app.pipeline.validate import _look_alike_numbers, normalize_invoice_no
    for n in ("SLS2627O108", "SLS26270109", "SLS26270110"):
        session.add(Invoice(user_id=users["priya"].id, source="upload", original_filename="f", file_path="f",
                            file_hash=n.ljust(64, "x"), status=Status.EXPORTED, supplier_gstin="36AAACS1234A1Z3",
                            invoice_no=n, invoice_no_norm=n, invoice_date="2026-09-01", extracted={"document_type": "tax_invoice"}))
    session.flush()
    found = _look_alike_numbers(session, Invoice(id=0), Invoice.supplier_gstin == "36AAACS1234A1Z3",
                                normalize_invoice_no("SLS26270108"), 2026)
    assert [o.invoice_no for o in found] == ["SLS2627O108"]
