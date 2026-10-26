"""validate.py and decide.py"""
from datetime import date, timedelta

from app.models import Invoice, Status
from app.pipeline import validate as validate_mod
from app.pipeline.checks import PASS, WARN, FAIL
from app.pipeline.decide import decide
from app.pipeline.validate import (gstin_valid, normalize_invoice_no, normalize_extracted,
                                   financial_year, validate)
from tests.conftest import make_extracted, not_an_invoice, SUPPLIER_GSTIN, OTHER_GSTIN


def by_name(result):
    return {c.name: c for c in result.checks}


def new_invoice(session, users, n=1, **fields):
    inv = Invoice(user_id=users["priya"].id, source="upload", original_filename=f"{n}.pdf",
                  file_path=f"{n}.pdf", file_hash=f"{n:064d}", **fields)
    session.add(inv)
    session.flush()
    return inv


def earlier(**fields):
    """Fields of an already-processed copy of make_extracted()'s invoice."""
    return dict(supplier_gstin=SUPPLIER_GSTIN, invoice_no_norm="SLS2627118",
                invoice_date="2026-09-15", grand_total=6136, status=Status.EXPORTED) | fields


# ---------- helpers

def test_gstin_checksum():
    assert gstin_valid(SUPPLIER_GSTIN) and gstin_valid(OTHER_GSTIN) and gstin_valid("36AABCT1234F1ZR")
    assert not gstin_valid("36AAACS1234A1Z4")      # wrong check digit
    assert not gstin_valid("36aaacs1234a1z3")
    assert not gstin_valid(None) and not gstin_valid("")


def test_normalize():
    assert normalize_invoice_no("sls/26-27 / 118") == "SLS2627118"
    assert financial_year(date(2026, 3, 31)) == 2025 and financial_year(date(2026, 4, 1)) == 2026
    d = normalize_extracted(make_extracted(supplier_gstin=" 36 aaacs-1234 a1z3 ", po_number="  ",
                                           invoice_number=" X-1 "))
    assert (d.supplier_gstin, d.po_number, d.invoice_number) == (SUPPLIER_GSTIN, None, "X-1")


# ---------- validate

def test_clean_invoice_passes(session, users):
    result = validate(session, new_invoice(session, users), make_extracted())
    assert [c for c in result.checks if c.status != PASS] == []
    assert result.duplicate_of is None


def test_low_confidence_and_wrong_buyer(session, users):
    c = by_name(validate(session, new_invoice(session, users),
                         make_extracted(confidence=0.5, low_confidence_fields=["grand_total"],
                                        buyer_gstin=OTHER_GSTIN)))
    assert c["confidence"].status == FAIL and "grand_total" in c["confidence"].message
    assert c["buyer_gstin"].status == FAIL


def test_missing_fields(session, users):
    c = by_name(validate(session, new_invoice(session, users),
                         make_extracted(supplier_name="", invoice_number="", supplier_gstin=None)))
    assert c["supplier_name"].status == FAIL and c["invoice_number"].status == FAIL
    assert c["supplier_gstin"].status == FAIL


def test_gst_type(session, users):
    inv = new_invoice(session, users)
    ok = make_extracted()
    assert by_name(validate(session, inv, ok))["gst_type"].status == PASS
    assert by_name(validate(session, inv, make_extracted(cgst=0, sgst=0, igst=ok.cgst * 2)))["gst_type"].status == FAIL
    assert by_name(validate(session, inv, make_extracted(place_of_supply_state_code="29")))["gst_type"].status == FAIL
    unknown = make_extracted(place_of_supply_state_code=None, buyer_gstin=None)
    assert by_name(validate(session, inv, unknown))["gst_type"].status == WARN


def test_arithmetic(session, users):
    inv = new_invoice(session, users)
    assert by_name(validate(session, inv, make_extracted(grand_total=9999)))["grand_total"].status == FAIL
    assert by_name(validate(session, inv, make_extracted(taxable_total=6000)))["line_total"].status == FAIL
    bad_line = [dict(description="MS Pipe 25mm", hsn_sac="7306", quantity=10, unit_price=450,
                     taxable_value=5400, gst_rate=18)]
    c = by_name(validate(session, inv, make_extracted(lines=bad_line)))
    assert c["line_1"].status == FAIL and "4500.00" in c["line_1"].message
    assert by_name(validate(session, inv, make_extracted(lines=[])))["lines"].status == FAIL


def test_dates(session, users):
    inv = new_invoice(session, users)
    future = (date.today() + timedelta(days=5)).isoformat()
    assert by_name(validate(session, inv, make_extracted(invoice_date=future)))["invoice_date"].status == FAIL
    assert by_name(validate(session, inv, make_extracted(invoice_date="15/09/2026")))["invoice_date"].status == FAIL


def test_exact_duplicate(session, users):
    first = new_invoice(session, users, 1, **earlier())
    result = validate(session, new_invoice(session, users, 2), make_extracted(invoice_number="sls/26-27/118"))
    assert result.duplicate_of == first.id


def test_same_number_different_amount_needs_review(session, users):
    new_invoice(session, users, 1, **earlier(grand_total=9000))
    result = validate(session, new_invoice(session, users, 2), make_extracted())
    assert result.duplicate_of is None
    assert by_name(result)["duplicate"].status == FAIL and "#1" in by_name(result)["duplicate"].message


def test_duplicate_ignores_other_year_rejected_and_other_supplier(session, users):
    new_invoice(session, users, 1, **earlier(invoice_date="2025-09-15"))
    new_invoice(session, users, 2, **earlier(status=Status.REJECTED))
    new_invoice(session, users, 3, **earlier(supplier_gstin=OTHER_GSTIN))
    new_invoice(session, users, 4, **earlier(status=Status.NOT_INVOICE))
    result = validate(session, new_invoice(session, users, 5), make_extracted())
    assert result.duplicate_of is None and by_name(result)["duplicate"].status == PASS


def test_unreadable_stored_date_flagged_not_crash(session, users):
    new_invoice(session, users, 1, **earlier(invoice_date=None))
    result = validate(session, new_invoice(session, users, 2), make_extracted())
    assert by_name(result)["duplicate"].status == FAIL


# ---------- decide

def test_decide(session, users):
    inv = new_invoice(session, users)
    clean = make_extracted()
    assert decide(clean, validate(session, inv, clean))[0] == Status.APPROVED
    assert decide(not_an_invoice(), None) == (Status.NOT_INVOICE, "Not an invoice (looks like: other)")
    bad = make_extracted(grand_total=1)
    assert decide(bad, validate(session, inv, bad))[0] == Status.NEEDS_REVIEW


def test_auto_approve_limit(session, users):
    inv = new_invoice(session, users)
    lines = [dict(description="Steel coil", hsn_sac="7208", quantity=1, unit_price=50000,
                  taxable_value=50000, gst_rate=18)]
    big = make_extracted(lines=lines)          # 59,000 incl. GST
    status, reason = decide(big, validate(session, inv, big))
    assert status == Status.NEEDS_REVIEW and "auto-approve limit" in reason
    lines[0].update(unit_price=42000, taxable_value=42000)
    small = make_extracted(lines=lines)        # 49,560
    assert decide(small, validate(session, inv, small))[0] == Status.APPROVED


def test_buyer_gstin_list_and_same_pan(session, users, monkeypatch):
    inv = new_invoice(session, users)
    monkeypatch.setattr(validate_mod.settings, "company_gstin", "36AABCT1234F1ZR, 29AAACS1234A1ZY")
    assert by_name(validate(session, inv, make_extracted(buyer_gstin=OTHER_GSTIN)))["buyer_gstin"].status == PASS
    # Same PAN (AABCT1234F) registered in Karnataka: still our company
    same_pan = "29AABCT1234F1ZM"
    assert gstin_valid(same_pan)
    c = by_name(validate(session, inv, make_extracted(buyer_gstin=same_pan, place_of_supply_state_code="36")))
    assert c["buyer_gstin"].status == PASS and "state 29" in c["buyer_gstin"].message
    c = by_name(validate(session, inv, make_extracted(buyer_gstin="19AABCT1332L2ZA")))
    assert c["buyer_gstin"].status == FAIL and "not one of your GSTINs" in c["buyer_gstin"].message
