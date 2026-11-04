# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""Branches, purchase orders, GRNs and three-way matching (D-111, D-112), through the API with a fake
extractor. The scenarios of docs/planning: invoice before delivery, partial delivery, damaged goods, the
same goods billed twice, and 50 laptops for five branches."""
import pytest

from app.config import settings
from tests.conftest import SUPPLIER_GSTIN, make_extracted
from tests.test_flow import client, fake_extract, get, h, run_worker, upload  # noqa: F401

API = "/api/v1"
BRANCH_GSTIN = "36AABCT1234F1ZR"     # the company's (conftest COMPANY_GSTIN)


@pytest.fixture(autouse=True)
def matching_on(monkeypatch):
    monkeypatch.setattr(settings, "po_matching", True)


def po_body(number="PO-2026-0042", lines=None, **kw):
    """Matches make_extracted's invoice: 10 pipes at 450, 20 elbows at 35."""
    return {"po_number": number, "supplier_name": "Sri Lakshmi Steels Pvt Ltd", "supplier_gstin": SUPPLIER_GSTIN,
            "po_date": "2026-09-01", "lines": lines or [
                {"description": "MS Pipe 25mm", "hsn_sac": "7306", "quantity": 10, "unit_price": 450, "gst_rate": 18},
                {"description": "GI Elbow 25mm", "hsn_sac": "7307", "quantity": 20, "unit_price": 35, "gst_rate": 18}],
            **kw}


def new_po(client, user, **kw):
    r = client.post(f"{API}/purchase-orders", json=po_body(**kw), headers=h(user))
    assert r.status_code == 201, r.text
    return r.json()


def grn(client, user, po_id, lines, confirm=True, **kw):
    return client.post(f"{API}/purchase-orders/{po_id}/grns", headers=h(user), json={
        "received_on": "2026-09-14", "lines": lines, "confirm": confirm, **kw})


def received(client, user, po_id, *qty):
    """A confirmed GRN accepting `qty[i]` of PO line i+1."""
    r = grn(client, user, po_id, [{"line_no": i, "received_qty": q} for i, q in enumerate(qty, start=1) if q])
    assert r.status_code == 201, r.text
    return r.json()


def invoice(client, user, fake_extract, n=1, **kw):
    fake_extract.append(make_extracted(invoice_number=f"SLS/{n}", **kw))
    inv_id = upload(client, user, content=f"%PDF-1.4 invoice {n}".encode())["invoice_id"]
    run_worker()
    return get(client, inv_id)


def po_check(d):
    return next(c for c in d["checks"] if c["name"] == "po")


def test_branches(client, users):
    r = client.post(f"{API}/branches", json={"code": "hyd", "name": "Hyderabad HO", "gstin": BRANCH_GSTIN})
    assert r.status_code == 201 and r.json()["code"] == "HYD" and r.json()["state_code"] == "36"
    assert client.post(f"{API}/branches", json={"code": "HYD", "name": "x"}).status_code == 409
    assert client.post(f"{API}/branches", json={"code": "BLR", "name": "x", "gstin": "29BAD"}).status_code == 422
    assert client.post(f"{API}/branches", json={"code": "BLR", "name": "x", "gstin": BRANCH_GSTIN,
                                               "state_code": "29"}).status_code == 422
    b = r.json()["id"]
    assert client.patch(f"{API}/branches/{b}", json={"city": "Hyderabad"}).json()["city"] == "Hyderabad"
    assert client.put(f"{API}/users/{users['ravi'].id}/branch", json={"branch_id": b}).json()["branch_id"] == b
    assert client.put(f"{API}/users/{users['ravi'].id}/branch", json={"branch_id": 999}).status_code == 422


def test_invoice_before_delivery_waits_then_goes_through(client, users, fake_extract):
    po = new_po(client, users["priya"])
    d = invoice(client, users["priya"], fake_extract)
    assert d["status"] == "NEEDS_REVIEW" and d["match_status"] == "unmatched" and d["po_id"] == po["id"]
    assert "nothing has been received yet" in po_check(d)["message"]

    r = received(client, users["ravi"], po["id"], 10, 20)
    assert r["po_status"] == "RECEIVED" and r["rechecked"] == [{"invoice_id": d["invoice_id"], "status": "APPROVED"}]
    d = get(client, d["invoice_id"])
    assert d["status"] in ("APPROVED", "EXPORTED") and d["match_status"] == "matched"
    lines = client.get(f"{API}/purchase-orders/{po['id']}").json()["lines"]
    assert [(ln["accepted"], ln["invoiced"], ln["to_receive"], ln["to_invoice"]) for ln in lines] == \
        [(10, 10, 0, 0), (20, 20, 0, 0)]


def test_partial_delivery_and_rejected_goods(client, users, fake_extract):
    po = new_po(client, users["priya"])
    r = grn(client, users["ravi"], po["id"], [{"line_no": 1, "received_qty": 8, "rejected_qty": 2}])
    assert r.status_code == 422 and "say why" in r.json()["detail"]
    r = grn(client, users["ravi"], po["id"], [{"line_no": 1, "received_qty": 8, "rejected_qty": 2,
                                               "reason": "two bent"}, {"line_no": 2, "received_qty": 20}])
    assert r.status_code == 201 and r.json()["po_status"] == "PARTIALLY_RECEIVED"
    d = invoice(client, users["priya"], fake_extract)        # bills all 10 pipes
    assert d["status"] == "NEEDS_REVIEW" and d["match_status"] == "partial"
    assert "bills 10 but only 6 is received and not billed (ordered 10, accepted 6" in po_check(d)["message"]


def test_price_above_the_po(client, users, fake_extract):
    lines = po_body()["lines"]
    lines[0]["unit_price"] = 440                             # invoice says 450: 2.3% more
    po = new_po(client, users["priya"], lines=lines)
    received(client, users["ravi"], po["id"], 10, 20)
    d = invoice(client, users["priya"], fake_extract)
    assert d["status"] == "NEEDS_REVIEW" and "price 450.00 is above the PO's 440.00" in po_check(d)["message"]


def test_the_same_goods_billed_twice(client, users, fake_extract):
    po = new_po(client, users["priya"])
    received(client, users["ravi"], po["id"], 10, 20)
    assert invoice(client, users["priya"], fake_extract, 1)["status"] in ("APPROVED", "EXPORTED")
    d = invoice(client, users["priya"], fake_extract, 2)     # another invoice number, same goods
    assert d["status"] == "NEEDS_REVIEW" and "billed before 10" in po_check(d)["message"]


def test_unknown_po_wrong_supplier_and_no_po(client, users, fake_extract, monkeypatch):
    d = invoice(client, users["priya"], fake_extract, 1)
    assert "is not in the system" in po_check(d)["message"] and d["match_status"] == "unmatched"
    bad = client.post(f"{API}/purchase-orders", json={**po_body("PO-78"), "supplier_gstin": "NOT-A-GSTIN"},
                      headers=h(users["priya"]))
    assert bad.status_code == 422 and "not a valid GSTIN" in bad.json()["detail"]
    new_po(client, users["priya"], number="PO-79", supplier_gstin=BRANCH_GSTIN)    # someone else's GSTIN
    d = invoice(client, users["priya"], fake_extract, 2, po_number="PO-79")
    assert po_check(d)["status"] == "FAIL" and "not this supplier" in po_check(d)["message"]
    monkeypatch.setattr(settings, "po_required", True)
    d = invoice(client, users["priya"], fake_extract, 3, po_number=None)
    assert d["match_status"] == "no_po" and po_check(d)["status"] == "WARN"


def test_po_arriving_after_the_invoice(client, users, fake_extract):
    d = invoice(client, users["priya"], fake_extract)
    assert d["match_status"] == "unmatched"
    po = new_po(client, users["priya"])
    assert po["rechecked"] == [{"invoice_id": d["invoice_id"], "status": "NEEDS_REVIEW"}]   # nothing received yet
    assert get(client, d["invoice_id"])["po_id"] == po["id"]
    assert received(client, users["ravi"], po["id"], 10, 20)["rechecked"][0]["status"] == "APPROVED"


def test_grn_rules_and_po_life(client, users, fake_extract):
    po = new_po(client, users["priya"])
    assert client.post(f"{API}/purchase-orders", json=po_body(), headers=h(users["priya"])).status_code == 409
    r = grn(client, users["ravi"], po["id"], [{"line_no": 1, "received_qty": 11}])
    assert r.status_code == 422 and "11 of 10 ordered" in r.json()["detail"]
    assert grn(client, users["ravi"], po["id"], [{"line_no": 9, "received_qty": 1}]).status_code == 422
    future = grn(client, users["ravi"], po["id"], [{"line_no": 1, "received_qty": 1}], received_on="2999-01-01")
    assert future.status_code == 422

    draft = grn(client, users["ravi"], po["id"], [{"line_no": 1, "received_qty": 6}], confirm=False).json()
    assert draft["status"] == "DRAFT"
    assert client.get(f"{API}/purchase-orders/{po['id']}").json()["lines"][0]["accepted"] == 0   # drafts don't count
    received(client, users["ravi"], po["id"], 5)                                                   # another GRN, confirmed
    r = client.post(f"{API}/grns/{draft['id']}/confirm", headers=h(users["ravi"]))
    assert r.status_code == 422 and "11 of 10 ordered" in r.json()["detail"]                       # checked again
    assert client.post(f"{API}/grns/{draft['id']}/cancel", json={"note": "counted twice"},
                       headers=h(users["ravi"])).json()["status"] == "CANCELLED"

    assert client.post(f"{API}/purchase-orders/{po['id']}/cancel", headers=h(users["priya"])).status_code == 409
    closed = client.post(f"{API}/purchase-orders/{po['id']}/close", json={"note": "rest not needed"},
                         headers=h(users["priya"])).json()
    assert closed["status"] == "CLOSED"
    assert grn(client, users["ravi"], po["id"], [{"line_no": 1, "received_qty": 1}]).status_code == 409


def test_fifty_laptops_for_five_branches(client, users, fake_extract):
    """One PO line per branch, 10 laptops each. Three branches accept all, the fourth receives 8 and
    rejects 1 as damaged, the fifth has received nothing: 37 accepted, 1 disputed, 12 pending. The
    supplier bills all 50 on one line: it waits; billed for 37 it goes through."""
    codes = ["HYD", "BLR", "CHN", "PUN", "DEL"]
    branches = [client.post(f"{API}/branches", json={"code": c, "name": c}).json()["id"] for c in codes]
    laptop = {"description": "Laptop i5 16GB", "hsn_sac": "84713010", "quantity": 10, "unit_price": 55000,
              "gst_rate": 18}
    po = new_po(client, users["priya"], number="PO-LAP-1",
                lines=[{**laptop, "branch_id": b} for b in branches])
    for line_no, (got, rejected) in enumerate([(10, 0), (10, 0), (10, 0), (8, 1)], start=1):
        r = grn(client, users["ravi"], po["id"], [{"line_no": line_no, "received_qty": got, "rejected_qty": rejected,
                                                   "reason": "screen cracked" if rejected else None}],
                branch_id=branches[line_no - 1])
        assert r.status_code == 201, r.text
    lines = client.get(f"{API}/purchase-orders/{po['id']}").json()["lines"]
    assert sum(ln["accepted"] for ln in lines) == 37 and sum(ln["rejected"] for ln in lines) == 1
    assert sum(ln["to_receive"] for ln in lines) == 13       # 12 not delivered + 1 to replace

    def laptops(n):
        return [dict(description="Laptop i5 16GB", hsn_sac="8471", quantity=n, unit_price=55000,
                     taxable_value=55000 * n, gst_rate=18)]
    d = invoice(client, users["priya"], fake_extract, 1, po_number="PO-LAP-1", lines=laptops(50))
    assert d["match_status"] == "partial" or d["match_status"] == "unmatched"
    assert "bills 50 but only 37 is received" in po_check(d)["message"]
    client.post(f"{API}/invoices/{d['invoice_id']}/reject", json={"note": "ask for an invoice for 37"},
                headers=h(users["priya"]))
    d = invoice(client, users["priya"], fake_extract, 2, po_number="PO-LAP-1", lines=laptops(37))
    assert d["match_status"] == "matched" and po_check(d)["status"] == "PASS"
    lines = client.get(f"{API}/purchase-orders/{po['id']}").json()["lines"]
    assert [ln["invoiced"] for ln in lines] == [10, 10, 10, 7, 0]


def test_matching_off_changes_nothing(client, users, fake_extract, monkeypatch):
    monkeypatch.setattr(settings, "po_matching", False)
    d = invoice(client, users["priya"], fake_extract)
    assert d["status"] in ("APPROVED", "EXPORTED") and d["match_status"] is None
    assert not any(c["name"] == "po" for c in d["checks"])


def test_invoice_branch_from_the_buyer_gstin(client, users, fake_extract):
    b = client.post(f"{API}/branches", json={"code": "HYD", "name": "HO", "gstin": BRANCH_GSTIN}).json()["id"]
    assert invoice(client, users["priya"], fake_extract, po_number=None)["branch_id"] == b
