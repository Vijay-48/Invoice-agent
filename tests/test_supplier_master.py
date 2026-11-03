# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""Supplier master and bank details (app/pipeline/supplier.py, D-104)."""
from app.models import AuditLog, Invoice, Status, Supplier, SupplierEvent
from app.pipeline import supplier as master
from tests.conftest import SUPPLIER_GSTIN, make_extracted
from tests.test_flow import client, fake_extract, get, h, run_worker, upload  # noqa: F401 (fixtures)

ACCOUNT, IFSC = "50200012345678", "HDFC0001234"
NEW_ACCOUNT, NEW_IFSC = "918020099887766", "UTIB0000456"


def read(client, users, fake_extract, number, **kw):
    """One invoice through upload, AI (faked) and checks; returns its id."""
    fake_extract.append(make_extracted(invoice_number=number, **kw))
    inv = upload(client, users["priya"], f"%PDF {number}".encode())["invoice_id"]
    run_worker()
    return inv


def check(client, inv, name):
    return next((c for c in get(client, inv)["checks"] if c["name"] == name), None)


def supplier(session):
    session.expire_all()
    return session.query(Supplier).filter_by(gstin=SUPPLIER_GSTIN).one_or_none()


def test_the_first_approved_invoice_records_the_supplier_and_its_bank(client, users, fake_extract, session):
    inv = read(client, users, fake_extract, "S/1", supplier_bank_account="5020 0012 3456 78", supplier_ifsc="hdfc0001234")
    assert get(client, inv)["status"] == Status.EXPORTED
    assert "First bank details" in check(client, inv, "bank_details")["message"]
    sup = supplier(session)
    assert (sup.name, sup.pan, sup.bank_account, sup.bank_ifsc, sup.bank_set_by) == (
        "Sri Lakshmi Steels Pvt Ltd", SUPPLIER_GSTIN[2:12], ACCOUNT, IFSC, "auto")
    assert [e.kind for e in session.query(SupplierEvent).order_by(SupplierEvent.id)] == ["created", "bank_recorded"]
    # the invoice's history says so too, without the whole account number
    note = session.query(AuditLog).filter_by(invoice_id=inv, stage="supplier").one().message
    assert "XXXX5678" in note and ACCOUNT not in note


def test_same_bank_passes_and_no_bank_is_no_check(client, users, fake_extract):
    read(client, users, fake_extract, "S/1", supplier_bank_account=ACCOUNT, supplier_ifsc=IFSC)
    same = read(client, users, fake_extract, "S/2", supplier_bank_account=ACCOUNT, supplier_ifsc=IFSC)
    assert get(client, same)["status"] == Status.EXPORTED
    assert check(client, same, "bank_details")["status"] == "PASS"
    none = read(client, users, fake_extract, "S/3")
    assert get(client, none)["status"] == Status.EXPORTED and check(client, none, "bank_details") is None


def test_changed_bank_details_hold_the_invoice_until_confirmed(client, users, fake_extract, session):
    read(client, users, fake_extract, "S/1", supplier_bank_account=ACCOUNT, supplier_ifsc=IFSC)
    inv = read(client, users, fake_extract, "S/2", supplier_bank_account=NEW_ACCOUNT, supplier_ifsc=NEW_IFSC)
    d = get(client, inv)
    assert d["status"] == Status.NEEDS_REVIEW and "Bank details differ" in d["status_reason"]
    c = check(client, inv, "bank_details")
    assert c["status"] == "FAIL" and "XXXX5678" in c["message"] and "XXXX7766" in c["message"]
    assert ACCOUNT not in c["message"] and NEW_ACCOUNT not in c["message"]

    # Approving is refused while the details differ
    r = client.post(f"/api/v1/invoices/{inv}/approve", headers=h(users["ravi"]))
    assert r.status_code == 409 and "Bank details differ" in r.json()["detail"]
    assert supplier(session).bank_account == ACCOUNT

    # A person confirms the new account with the supplier, then approval works
    body = {"bank_account": NEW_ACCOUNT, "bank_ifsc": NEW_IFSC, "invoice_id": inv,
            "note": "Called Ramesh (accounts) on the number in our vendor file; he confirmed the new account"}
    r = client.post(f"/api/v1/suppliers/{SUPPLIER_GSTIN}/bank", json=body, headers=h(users["ravi"]))
    assert r.status_code == 200 and r.json()["bank_account"] == NEW_ACCOUNT and r.json()["bank_set_by"] == "Ravi"
    assert client.post(f"/api/v1/invoices/{inv}/approve", headers=h(users["ravi"])).status_code == 200
    events = client.get(f"/api/v1/suppliers/{SUPPLIER_GSTIN}").json()["events"]
    assert events[0]["kind"] == "bank_confirmed" and "Called Ramesh" in events[0]["message"]
    assert NEW_ACCOUNT not in events[0]["message"]
    confirmed = session.query(SupplierEvent).filter_by(kind="bank_confirmed").one()
    assert confirmed.data["before"]["account"] == ACCOUNT and confirmed.data["after"]["account"] == NEW_ACCOUNT


def test_a_misread_account_can_be_corrected_instead(client, users, fake_extract):
    read(client, users, fake_extract, "S/1", supplier_bank_account=ACCOUNT, supplier_ifsc=IFSC)
    inv = read(client, users, fake_extract, "S/2", supplier_bank_account="50200012345679", supplier_ifsc=IFSC)
    assert get(client, inv)["status"] == Status.NEEDS_REVIEW
    r = client.patch(f"/api/v1/invoices/{inv}", json={"supplier_bank_account": ACCOUNT, "note": "AI misread a digit"},
                     headers=h(users["ravi"]))
    assert r.status_code == 200 and check(client, inv, "bank_details")["status"] == "PASS"
    assert client.post(f"/api/v1/invoices/{inv}/approve", headers=h(users["ravi"])).status_code == 200


def test_approval_never_changes_recorded_bank_details(client, users, fake_extract, session):
    """Even when an invoice with other details gets through (e.g. approved before the check existed)."""
    read(client, users, fake_extract, "S/1", supplier_bank_account=ACCOUNT, supplier_ifsc=IFSC)
    inv = session.get(Invoice, 1)
    master.record_approved(session, inv, make_extracted(supplier_bank_account=NEW_ACCOUNT, supplier_ifsc=NEW_IFSC), "Ravi")
    session.commit()
    assert supplier(session).bank_account == ACCOUNT


def test_confirming_needs_a_real_note_and_valid_details(client, users, fake_extract):
    read(client, users, fake_extract, "S/1", supplier_bank_account=ACCOUNT, supplier_ifsc=IFSC)
    url = f"/api/v1/suppliers/{SUPPLIER_GSTIN}/bank"
    ok = {"bank_account": NEW_ACCOUNT, "bank_ifsc": NEW_IFSC, "note": "Called on the known number, confirmed"}
    assert client.post(url, json={**ok, "note": "ok"}, headers=h(users["ravi"])).status_code == 422
    assert client.post(url, json={**ok, "bank_ifsc": "HDFC1234"}, headers=h(users["ravi"])).status_code == 422
    assert client.post(url, json={**ok, "bank_account": "12-3"}, headers=h(users["ravi"])).status_code == 422
    assert client.post(url, json=ok).status_code == 422                                          # no X-User-Id
    assert client.post("/api/v1/suppliers/29AAACS1234A1ZY/bank", json=ok, headers=h(users["ravi"])).status_code == 404


def test_a_malformed_ifsc_is_flagged(client, users, fake_extract):
    inv = read(client, users, fake_extract, "S/1", supplier_bank_account=ACCOUNT, supplier_ifsc="HDFC1234")
    d = get(client, inv)
    assert d["status"] == Status.NEEDS_REVIEW and check(client, inv, "bank_ifsc")["status"] == "WARN"


def test_suppliers_list_includes_the_master(client, users, fake_extract):
    read(client, users, fake_extract, "S/1", supplier_bank_account=ACCOUNT, supplier_ifsc=IFSC)
    row, = client.get("/api/v1/suppliers").json()
    assert row["gstin"] == SUPPLIER_GSTIN and row["invoices"] == 1 and row["in_master"] is True
    assert row["bank_account"] == ACCOUNT and row["bank_ifsc"] == IFSC and row["pan"] == SUPPLIER_GSTIN[2:12]
    one = client.get(f"/api/v1/suppliers/{SUPPLIER_GSTIN.lower()}").json()
    assert one["name"] == "Sri Lakshmi Steels Pvt Ltd" and one["invoices"] == 1 and one["events"]
    assert client.get("/api/v1/suppliers/29AAACS1234A1ZY").status_code == 404
