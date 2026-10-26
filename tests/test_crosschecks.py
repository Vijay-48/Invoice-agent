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
