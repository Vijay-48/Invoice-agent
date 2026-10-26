"""Scoring rules of the golden-set evaluation (app/evaluation.py). No AI calls."""
from app.evaluation import compare, predict_decision, same_value, DocResult, FieldResult
from app.models import Status
from tests.conftest import make_extracted, not_an_invoice


def golden(expected, alternatives=None):
    return {"file": "x.pdf", "verified": True, "expected": expected, "alternatives": alternatives or {}}


def by_name(results):
    return {r.name: r for r in results}


def test_tolerances_and_normalisation():
    assert same_value("grand_total", 6136, 6136.9) and not same_value("grand_total", 6136, 6137.5)
    assert same_value("invoice_number", "SLS/26-27/118", "sls 26-27 118")
    assert same_value("supplier_gstin", "36AAACS1234A1Z3", "36aaacs1234a1z3 ")
    assert same_value("supplier_name", "HFCL Limited", "HFCL LTD.")
    assert same_value("supplier_name", "ITI Ltd. Raebareli", "ITI LTD. RAEBARELI")
    assert not same_value("supplier_name", "HFCL Limited", "ITI Limited")
    assert same_value("place_of_supply_state_code", "9", "09")
    assert same_value("supplier_gstin", None, None) and not same_value("supplier_gstin", None, "06AAACH4041D1Z8")


def test_compare_scores_only_listed_fields_and_lines():
    data = make_extracted()
    r = by_name(compare(golden({"invoice_number": "SLS/26-27/118", "grand_total": 6136,
                                "lines": [{"quantity": 10, "unit_price": 450}, {"quantity": 21}]}), data))
    assert set(r) == {"invoice_number", "grand_total", "lines"}
    assert r["invoice_number"].ok and r["grand_total"].ok
    assert not r["lines"].ok and "line 2 quantity: 20.0, expected 21" in r["lines"].note


def test_alternatives_count_as_correct():
    data = make_extracted(invoice_number="B025260106")
    r = by_name(compare(golden({"invoice_number": "BO25260106"}, {"invoice_number": ["B025260106"]}), data))
    assert r["invoice_number"].ok


def test_predicted_decisions():
    assert predict_decision(make_extracted())[0] == Status.APPROVED
    assert predict_decision(make_extracted(grand_total=1))[0] == Status.NEEDS_REVIEW
    assert predict_decision(make_extracted(document_type="journal_entry"))[0] == Status.NEEDS_REVIEW
    assert predict_decision(not_an_invoice())[0] == Status.NOT_INVOICE


def test_wrong_auto_approve_is_flagged():
    ok = DocResult("a", fields=[FieldResult("grand_total", 1, 1, True)],
                   decision_expected=Status.APPROVED, decision_actual=Status.APPROVED)
    wrong_value = DocResult("b", fields=[FieldResult("grand_total", 1, 2, False)],
                            decision_expected=Status.APPROVED, decision_actual=Status.APPROVED)
    should_review = DocResult("c", decision_expected=Status.NEEDS_REVIEW, decision_actual=Status.APPROVED)
    assert not ok.wrong_auto_approve and wrong_value.wrong_auto_approve and should_review.wrong_auto_approve
