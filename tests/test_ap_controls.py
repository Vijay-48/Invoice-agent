# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""The approval matrix (D-108), tax treatment (D-109) and payables (D-110), through the API with a fake
extractor."""
from datetime import date
from types import SimpleNamespace

from app import payables
from app.config import settings
from app.models import Supplier
from app.pipeline import tax
from app.pipeline.validate import CHARS
from tests.conftest import SUPPLIER_GSTIN, make_extracted
from tests.test_flow import client, excel_rows, fake_extract, get, h, run_worker, upload  # noqa: F401

API = "/api/v1"


def line(hsn, taxable, rate=18, description="Service"):
    return dict(description=description, hsn_sac=hsn, quantity=1, unit_price=taxable, taxable_value=taxable,
                gst_rate=rate)


def add(client, user, fake_extract, n, lines=None, **kw):
    """Upload invoice number `n` (distinct file and number), read it, return its details."""
    fake_extract.append(make_extracted(invoice_number=f"T/{n}", **({"lines": lines} if lines else {}), **kw))
    inv_id = upload(client, user, content=f"%PDF-1.4 invoice {n}".encode())["invoice_id"]
    run_worker()
    return get(client, inv_id)


def big(client, user, fake_extract, n=1):
    """60,000 + GST: above the auto-approve limit, so it waits for a person."""
    d = add(client, user, fake_extract, n, [line("7208", 60000, description="Steel coil")])
    assert d["status"] == "NEEDS_REVIEW"
    return d["invoice_id"]


def approve(client, user, inv_id):
    return client.post(f"{API}/invoices/{inv_id}/approve", json={}, headers=h(user))


def check(d, name):
    return next((c for c in d["checks"] if c["name"] == name), None)


def with_checksum(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        product = CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return first14 + CHARS[(36 - total % 36) % 36]


# ---------- approval matrix

def test_approval_limit(client, users, fake_extract):
    inv_id = big(client, users["priya"], fake_extract)
    ravi = users["ravi"].id
    assert client.put(f"{API}/users/{ravi}/approval-limit", json={"limit": 10000},
                      headers=h(users["priya"])).json()["approval_limit"] == 10000
    r = approve(client, users["ravi"], inv_id)
    assert r.status_code == 403 and "approval limit 10,000.00" in r.json()["detail"]
    client.put(f"{API}/users/{ravi}/approval-limit", json={"limit": 0}, headers=h(users["priya"]))
    assert "may not approve" in approve(client, users["ravi"], inv_id).json()["detail"]
    client.put(f"{API}/users/{ravi}/approval-limit", json={"limit": None}, headers=h(users["priya"]))
    assert approve(client, users["ravi"], inv_id).json()["status"] in ("APPROVED", "EXPORTED")


def test_two_approvers_above_the_second_approval_limit(client, users, fake_extract, monkeypatch):
    monkeypatch.setattr(settings, "second_approval_above", 50000)
    inv_id = big(client, users["priya"], fake_extract)
    first = approve(client, users["priya"], inv_id).json()
    assert first["status"] == "NEEDS_REVIEW" and "(1 of 2)" in first["status_reason"]
    again = approve(client, users["priya"], inv_id)
    assert again.status_code == 409 and "another person" in again.json()["detail"]
    second = approve(client, users["ravi"], inv_id).json()
    assert second["status"] in ("APPROVED", "EXPORTED") and second["approved_by"] == "Priya, Ravi"
    assert [a["user_name"] for a in get(client, inv_id)["approvals"]] == ["Priya", "Ravi"]


def test_a_correction_clears_the_approvals_given(client, users, fake_extract, monkeypatch):
    monkeypatch.setattr(settings, "second_approval_above", 50000)
    inv_id = big(client, users["priya"], fake_extract)
    approve(client, users["priya"], inv_id)
    r = client.patch(f"{API}/invoices/{inv_id}", json={"po_number": "PO-9"}, headers=h(users["ravi"]))
    assert r.status_code == 200 and get(client, inv_id)["approvals"] == []
    assert approve(client, users["ravi"], inv_id).json()["status"] == "NEEDS_REVIEW"   # 1 of 2 again


def test_maker_checker(client, users, fake_extract, monkeypatch):
    monkeypatch.setattr(settings, "maker_checker", True)
    inv_id = big(client, users["priya"], fake_extract)
    r = approve(client, users["priya"], inv_id)
    assert r.status_code == 403 and "sent this invoice in" in r.json()["detail"]
    client.patch(f"{API}/invoices/{inv_id}", json={"po_number": "PO-9"}, headers=h(users["ravi"]))
    r = approve(client, users["ravi"], inv_id)
    assert r.status_code == 403 and "corrected this invoice" in r.json()["detail"]


# ---------- tax

def test_tds_professional_fees(client, users, fake_extract):
    d = add(client, users["priya"], fake_extract, 1, [line("998314", 60000, description="IT consulting")])
    c = check(d, "tds")
    assert c["status"] == "PASS" and "194J" in c["message"] and "₹6,000.00" in c["message"]
    assert d["tds"] == {"section": "194J", "base": 60000.0, "rate": 10.0, "amount": 6000.0}


def test_tds_below_the_threshold_is_information_only(client, users, fake_extract):
    d = add(client, users["priya"], fake_extract, 1, [line("998314", 20000)])
    assert d["status"] in ("APPROVED", "EXPORTED")
    assert "not due yet" in check(d, "tds")["message"] and d["tds"]["amount"] is None


def test_crossing_the_years_threshold_flags_the_earlier_invoices(client, users, fake_extract):
    first = add(client, users["priya"], fake_extract, 1, [line("998314", 40000)])
    assert first["status"] in ("APPROVED", "EXPORTED") and first["tds"]["amount"] is None
    second = add(client, users["priya"], fake_extract, 2, [line("998314", 20000)])
    c = check(second, "tds")
    assert second["status"] == "NEEDS_REVIEW" and c["status"] == "WARN"
    assert "earlier ₹40,000.00 too" in c["message"] and second["tds"]["amount"] == 2000


def test_tds_rate_depends_on_the_suppliers_pan(session):
    """194C: 1% for an individual's PAN (4th character P), 2% for a company's."""
    person = with_checksum("36ABCPK1234A1Z")
    for gstin, rate in ((SUPPLIER_GSTIN, 2), (person, 1)):
        inv = SimpleNamespace(id=0)
        data = make_extracted(supplier_gstin=gstin, lines=[line("998719", 50000, description="AC repair")])
        assert tax.check_tds(session, inv, data)[0].status == "PASS"
        assert (inv.tds_section, inv.tds_rate, inv.tds_amount) == ("194C", rate, 500 * rate)


def test_reverse_charge_and_blocked_credit(client, users, fake_extract):
    d = add(client, users["priya"], fake_extract, 1, [line("996511", 8000, rate=0, description="Freight")])
    assert d["status"] == "NEEDS_REVIEW" and check(d, "rcm")["status"] == "WARN"
    assert "5% GST = ₹400.00" in check(d, "rcm")["message"]
    d = add(client, users["priya"], fake_extract, 2, [line("8703", 30000, rate=28, description="Car")])
    assert check(d, "itc_blocked")["status"] == "WARN" and "motor car" in check(d, "itc_blocked")["message"]


# ---------- payables

def test_due_dates_follow_the_suppliers_terms(client, users, fake_extract, session):
    d = add(client, users["priya"], fake_extract, 1)
    assert d["status"] in ("APPROVED", "EXPORTED") and d["due_date"] == "2026-10-15"   # 15 Sep + 30 days
    terms = f"{API}/suppliers/{SUPPLIER_GSTIN}/terms"
    r = client.put(terms, json={"payment_terms_days": 60, "msme_category": "small",
                                "udyam_number": "UDYAM-TS-02-0012345"}, headers=h(users["priya"]))
    assert r.status_code == 200 and r.json()["due_dates_changed"] == 1
    assert get(client, d["invoice_id"])["due_date"] == "2026-10-30"     # MSME: at most 45 days
    client.put(terms, json={"msme_category": "micro"}, headers=h(users["priya"]))
    assert get(client, d["invoice_id"])["due_date"] == "2026-09-30"     # MSME, nothing agreed: 15 days
    client.put(terms, json={"payment_terms_days": 60}, headers=h(users["priya"]))
    assert get(client, d["invoice_id"])["due_date"] == "2026-11-14"     # not MSME: as agreed
    assert client.put(terms, json={"udyam_number": "12345"}, headers=h(users["priya"])).status_code == 422
    assert client.put(f"{API}/suppliers/36AAACX0000A1Z5/terms", json={}, headers=h(users["priya"])).status_code == 404
    session.expire_all()
    assert session.query(Supplier).one().payment_terms_days == 60


def test_ageing_payment_and_due_list(client, users, fake_extract):
    d = add(client, users["priya"], fake_extract, 1, [line("998314", 40000)])      # due 2026-10-15
    client.put(f"{API}/suppliers/{SUPPLIER_GSTIN}/terms", json={"payment_terms_days": 30, "msme_category": "micro"},
               headers=h(users["priya"]))
    report = client.get(f"{API}/payables/ageing?as_of=2026-11-03").json()
    (row,) = report["suppliers"]
    assert row["1-30"] == 47200 and row["msme"] == "micro" and report["totals"]["total"] == 47200
    assert [x["invoice_id"] for x in report["msme_overdue"]] == [d["invoice_id"]]
    assert [x["days_overdue"] for x in client.get(f"{API}/payables/due?days=7").json()][0] > 0

    pay = f"{API}/invoices/{d['invoice_id']}/payment"
    assert client.post(pay, json={"paid_on": "2099-01-01"}, headers=h(users["priya"])).status_code == 422
    paid = client.post(pay, json={"paid_on": "2026-10-20", "reference": "UTR123"}, headers=h(users["priya"])).json()
    assert (paid["paid_on"], paid["paid_reference"], paid["paid_by"]) == ("2026-10-20", "UTR123", "Priya")
    assert client.post(pay, json={"paid_on": "2026-10-20"}, headers=h(users["priya"])).status_code == 409
    assert client.get(f"{API}/payables/ageing").json()["suppliers"] == []
    assert client.request("DELETE", pay, json={"note": "wrong invoice"}, headers=h(users["priya"])).json()["paid_on"] is None


def test_net_payable_is_after_tds_and_credit_notes_reduce_it():
    inv = SimpleNamespace(grand_total=70800, tds_amount=6000, extracted={"document_type": "tax_invoice"})
    assert payables.net_payable(inv) == 64800
    inv = SimpleNamespace(grand_total=1180, tds_amount=None, extracted={"document_type": "credit_note"})
    assert payables.net_payable(inv) == -1180
    assert payables.bucket(date(2026, 10, 1), date(2026, 11, 3)) == "31-60"
    assert payables.bucket(None, date(2026, 11, 3)) == "not_due"


def test_excel_has_due_date_tds_and_net_payable(client, users, fake_extract):
    add(client, users["priya"], fake_extract, 1, [line("998314", 40000)])
    (row,) = excel_rows()
    due, section, tds_amount, net = row[-4:]
    assert (due.date(), section, tds_amount, net) == (date(2026, 10, 15), None, None, 47200)
