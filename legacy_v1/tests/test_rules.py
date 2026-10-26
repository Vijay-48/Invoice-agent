"""validate.py, match.py, decide.py"""
from datetime import date, timedelta

from app.models import Invoice, Status, POLine
from app.pipeline.checks import Check, PASS, WARN, FAIL
from app.pipeline.decide import decide
from app.pipeline.match import match
from app.pipeline.validate import gstin_valid, normalize_invoice_no, financial_year, validate
from tests.conftest import make_extracted, SUPPLIER_GSTIN, OTHER_GSTIN


def by_name(checks):
    return {c.name: c for c in checks}


def new_invoice(session, n=1, **fields):
    inv = Invoice(source="upload", original_filename=f"{n}.pdf", file_path=f"{n}.pdf",
                  file_hash=f"{n:064d}", **fields)
    session.add(inv)
    session.flush()
    return inv


# ---------- validate

def test_gstin_checksum():
    assert gstin_valid(SUPPLIER_GSTIN)
    assert gstin_valid(OTHER_GSTIN)
    assert gstin_valid("36AABCT1234F1ZR")
    assert not gstin_valid("36AAACS1234A1Z4")      # wrong check digit
    assert not gstin_valid("36aaacs1234a1z3")
    assert not gstin_valid(None)
    assert not gstin_valid("")


def test_normalize_and_fy():
    assert normalize_invoice_no("sls/26-27 / 118") == "SLS2627118"
    assert financial_year(date(2026, 3, 31)) == 2025
    assert financial_year(date(2026, 4, 1)) == 2026


def test_clean_invoice_passes(session):
    inv = new_invoice(session)
    checks = validate(session, inv, make_extracted())
    assert all(c.status == PASS for c in checks), [c for c in checks if c.status != PASS]


def test_not_an_invoice(session):
    checks = validate(session, new_invoice(session), make_extracted(is_invoice=False))
    assert [c.name for c in checks] == ["document_type"] and checks[0].status == FAIL


def test_low_confidence_and_wrong_buyer(session):
    c = by_name(validate(session, new_invoice(session),
                         make_extracted(confidence=0.5, buyer_gstin=OTHER_GSTIN)))
    assert c["confidence"].status == FAIL
    assert c["buyer_gstin"].status == FAIL


def test_gst_type(session):
    inv = new_invoice(session)
    ok = make_extracted()
    assert by_name(validate(session, inv, ok))["gst_type"].status == PASS
    igst_intra = make_extracted(cgst=0, sgst=0, igst=ok.cgst * 2)
    assert by_name(validate(session, inv, igst_intra))["gst_type"].status == FAIL
    inter_with_cgst = make_extracted(place_of_supply_state_code="29")
    assert by_name(validate(session, inv, inter_with_cgst))["gst_type"].status == FAIL


def test_arithmetic(session):
    inv = new_invoice(session)
    c = by_name(validate(session, inv, make_extracted(grand_total=9999)))
    assert c["grand_total"].status == FAIL
    c = by_name(validate(session, inv, make_extracted(taxable_total=6000)))
    assert c["line_total"].status == FAIL


def test_dates(session):
    inv = new_invoice(session)
    future = (date.today() + timedelta(days=5)).isoformat()
    assert by_name(validate(session, inv, make_extracted(invoice_date=future)))["invoice_date"].status == FAIL
    assert by_name(validate(session, inv, make_extracted(invoice_date="15/09/2026")))["invoice_date"].status == FAIL


def test_duplicate_same_fy(session):
    new_invoice(session, 1, supplier_gstin=SUPPLIER_GSTIN, invoice_no_norm="SLS2627118",
                invoice_date="2026-05-01", status=Status.POSTED)
    inv = new_invoice(session, 2)
    assert by_name(validate(session, inv, make_extracted()))["duplicate"].status == FAIL


def test_duplicate_ignores_other_fy_and_rejected(session):
    new_invoice(session, 1, supplier_gstin=SUPPLIER_GSTIN, invoice_no_norm="SLS2627118",
                invoice_date="2025-05-01", status=Status.POSTED)
    new_invoice(session, 2, supplier_gstin=SUPPLIER_GSTIN, invoice_no_norm="SLS2627118",
                invoice_date="2026-05-01", status=Status.REJECTED)
    inv = new_invoice(session, 3)
    assert by_name(validate(session, inv, make_extracted()))["duplicate"].status == PASS


def test_duplicate_check_survives_bad_stored_date(session):
    """Bug: another invoice with an unparseable stored date crashed validation.
    Its year is unknown, so it's flagged as a possible duplicate for a human to check."""
    new_invoice(session, 1, supplier_gstin=SUPPLIER_GSTIN, invoice_no_norm="SLS2627118",
                invoice_date="15/09/26", status=Status.EXCEPTION)
    inv = new_invoice(session, 2)
    c = by_name(validate(session, inv, make_extracted()))["duplicate"]
    assert c.status == FAIL and "[1]" in c.message


# ---------- match

def test_match_happy_path(session, master):
    checks, supplier, po = match(session, make_extracted())
    assert supplier.id == master["supplier"].id and po.id == master["po"].id
    assert all(c.status == PASS for c in checks), checks


def test_unknown_supplier(session, master):
    checks, supplier, po = match(session, make_extracted(supplier_gstin="27AAACS1234A1Z7"))
    assert supplier is None and by_name(checks)["supplier_match"].status == FAIL


def test_non_po_invoice_needs_approval(session, master):
    checks, _, po = match(session, make_extracted(po_number=None))
    assert po is None and by_name(checks)["po_match"].status == WARN


def test_po_of_other_supplier(session, master):
    checks, _, _ = match(session, make_extracted(supplier_gstin=OTHER_GSTIN))
    assert by_name(checks)["po_match"].status == FAIL


def test_price_and_qty(session, master):
    lines = [dict(description="MS Pipe 25mm", hsn_sac="7306", quantity=10, unit_price=455,
                  taxable_value=4550, gst_rate=18),
             dict(description="GI Elbow 25mm", hsn_sac="7307", quantity=160, unit_price=40,
                  taxable_value=6400, gst_rate=18)]
    c = by_name(match(session, make_extracted(lines=lines))[0])
    assert c["line_1_price"].status == WARN       # +1.1%, within tolerance
    assert c["line_2_price"].status == FAIL       # +14%
    assert c["line_2_qty"].status == FAIL         # 160 > 150 received


def test_line_not_on_po(session, master):
    lines = [dict(description="Copper wire 4sqmm", hsn_sac="7408", quantity=1, unit_price=10,
                  taxable_value=10, gst_rate=18)]
    assert by_name(match(session, make_extracted(lines=lines))[0])["line_1"].status == FAIL


def test_zero_price_po_line_does_not_crash(session, master):
    """Bug: a PO line with unit_price 0 raised ZeroDivisionError."""
    po = master["po"]
    po.lines.append(POLine(description="Free sample", hsn_sac="9999", qty_ordered=1,
                           qty_received=1, unit_price=0, gst_rate=0))
    session.commit()
    lines = [dict(description="Free sample", hsn_sac="9999", quantity=1, unit_price=5,
                  taxable_value=5, gst_rate=0)]
    c = by_name(match(session, make_extracted(lines=lines))[0])
    assert c["line_1_price"].status == FAIL


def test_qty_counts_earlier_invoices_on_same_po(session, master):
    """Bug: two invoices for all 100 received pipes both passed."""
    lines = [dict(description="MS Pipe 25mm", hsn_sac="7306", quantity=80, unit_price=450,
                  taxable_value=36000, gst_rate=18)]
    first = new_invoice(session, 1, status=Status.POSTED)
    c = by_name(match(session, make_extracted(lines=lines), first)[0])
    assert c["line_1_qty"].status == PASS
    session.commit()

    second = new_invoice(session, 2)
    c = by_name(match(session, make_extracted(lines=lines, invoice_number="X2"), second)[0])
    assert c["line_1_qty"].status == FAIL, c["line_1_qty"].message


def test_qty_ignores_rejected_invoices(session, master):
    lines = [dict(description="MS Pipe 25mm", hsn_sac="7306", quantity=80, unit_price=450,
                  taxable_value=36000, gst_rate=18)]
    first = new_invoice(session, 1)
    match(session, make_extracted(lines=lines), first)
    first.status = Status.REJECTED
    session.commit()
    second = new_invoice(session, 2)
    c = by_name(match(session, make_extracted(lines=lines), second)[0])
    assert c["line_1_qty"].status == PASS


def test_rematch_replaces_previous_line_usage(session, master):
    """Reprocessing must not count the invoice's own earlier match against itself."""
    lines = [dict(description="MS Pipe 25mm", hsn_sac="7306", quantity=80, unit_price=450,
                  taxable_value=36000, gst_rate=18)]
    inv = new_invoice(session, 1, status=Status.PROCESSING)
    match(session, make_extracted(lines=lines), inv)
    session.commit()
    c = by_name(match(session, make_extracted(lines=lines), inv)[0])
    assert c["line_1_qty"].status == PASS


# ---------- decide

def test_decide():
    assert decide([Check("a", PASS, "")], 1000)[0] == Status.AUTO_POST
    assert decide([Check("a", WARN, "w")], 1000) == (Status.PENDING_APPROVAL, "w")
    assert decide([Check("a", WARN, "w"), Check("b", FAIL, "f")], 1000) == (Status.EXCEPTION, "f")
    assert decide([Check("a", PASS, "")], 50000.01)[0] == Status.PENDING_APPROVAL
    assert decide([Check("a", PASS, "")], 50000)[0] == Status.AUTO_POST
