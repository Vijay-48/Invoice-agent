# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""GSTR-2B reconciliation (app/gstr2b.py, D-105)."""
import io
import json

from openpyxl import load_workbook

from app import gstr2b
from tests.conftest import OTHER_GSTIN, SUPPLIER_GSTIN, make_extracted
from tests.test_flow import client, fake_extract, get, h, run_worker, upload  # noqa: F401 (fixtures)

OURS = "36AABCT1234F1ZR"          # COMPANY_GSTIN in tests, the buyer on make_extracted's invoices


def approve(client, users, fake_extract, number, date, **kw):
    """An invoice through the pipeline; with make_extracted's lines it is auto-approved (6,136.00, tax 936)."""
    fake_extract.append(make_extracted(invoice_number=number, invoice_date=date, **kw))
    inv = upload(client, users["priya"], f"%PDF {number} {date}".encode())["invoice_id"]
    run_worker()
    assert get(client, inv)["status"] == "EXPORTED", get(client, inv)["status_reason"]
    return inv


def doc(number, date, taxable=5200, cgst=468, sgst=468, igst=0, val=6136, itcavl="Y", rsn=None):
    return {"inum": number, "typ": "R", "dt": date, "val": val, "pos": "36", "rev": "N", "itcavl": itcavl,
            "rsn": rsn, "txval": taxable, "igst": igst, "cgst": cgst, "sgst": sgst, "cess": 0}


def gstr2b_file(period, b2b, cdnr=(), gstin=OURS) -> bytes:
    return json.dumps({"chksum": "x", "data": {
        "gstin": gstin, "rtnprd": period, "version": "1.0", "gendt": "14-10-2026",
        "docdata": {"b2b": [{"ctin": SUPPLIER_GSTIN, "trdnm": "SRI LAKSHMI STEELS", "supfildt": "11-10-2026",
                             "supprd": period, "inv": list(b2b)}],
                    "cdnr": [{"ctin": SUPPLIER_GSTIN, "trdnm": "SRI LAKSHMI STEELS", "nt": list(cdnr)}] if cdnr else []},
    }}).encode()


def send(client, users, content, name="returns_R2B.json"):
    return client.post("/api/v1/gstr2b", files={"file": (name, content, "application/json")}, headers=h(users["ravi"]))


def by_number(result):
    return {d["number"]: d for d in result["documents"]}


def test_a_month_reconciled(client, users, fake_extract):
    a = approve(client, users, fake_extract, "SLS/26-27/118", "2026-09-05")
    b = approve(client, users, fake_extract, "SLS/26-27/119", "2026-09-10")
    c = approve(client, users, fake_extract, "SLS/26-27/120", "2026-09-15")     # the supplier doesn't file it
    d = approve(client, users, fake_extract, "SLS/26-27/121", "2026-09-20")

    r = send(client, users, gstr2b_file("092026", [
        doc("SLS-26-27-118", "05-09-2026"),                        # same number, other punctuation
        doc("118/B", "10-09-2026"),                                # number differs: same date and total
        doc("SLS/26-27/121", "20-09-2026", cgst=400, sgst=400),    # filed with lower tax
        doc("SLS/26-27/150", "25-09-2026"),                        # we never got this one
    ]))
    assert r.status_code == 201, r.text
    result = r.json()
    rows = by_number(result)
    assert (rows["SLS-26-27-118"]["status"], rows["SLS-26-27-118"]["invoice_id"], rows["SLS-26-27-118"]["matched_by"]) == (
        "matched", a, "number")
    assert (rows["118/B"]["status"], rows["118/B"]["invoice_id"], rows["118/B"]["matched_by"]) == ("matched", b, "date_and_amount")
    assert rows["SLS/26-27/121"]["status"] == "mismatch" and rows["SLS/26-27/121"]["invoice_id"] == d
    assert "tax 936.00" in rows["SLS/26-27/121"]["difference"] and "tax 800.00" in rows["SLS/26-27/121"]["difference"]
    assert rows["SLS/26-27/150"]["status"] == "not_in_books" and rows["SLS/26-27/150"]["invoice_id"] is None
    assert [m["invoice_id"] for m in result["missing_from_2b"]] == [c]

    s = result["summary"]
    assert (s["matched"], s["mismatch"], s["not_in_books"], s["missing_from_2b"]) == (2, 1, 1, 1)
    assert s["itc_in_2b"] == 936 * 3 + 800
    assert s["itc_at_risk"] == 936 + 136 and s["itc_at_risk_parts"] == {
        "missing_from_2b": 936, "filed_lower": 136, "itc_not_available": 0}
    assert s["suppliers_to_chase"] == [SUPPLIER_GSTIN]
    assert result["return"]["period"] == "2026-09" and result["return"]["documents"] == 4

    # the list, a filter, and the workbook for the accountant
    listed, = client.get("/api/v1/gstr2b").json()
    assert listed["summary"]["itc_at_risk"] == 1072
    only = client.get(f"/api/v1/gstr2b/{listed['id']}?status=mismatch").json()["documents"]
    assert [x["number"] for x in only] == ["SLS/26-27/121"]
    xl = client.get(f"/api/v1/gstr2b/{listed['id']}/excel")
    assert xl.status_code == 200 and "GSTR-2B reconciliation" in xl.headers["content-disposition"]
    wb = load_workbook(io.BytesIO(xl.content))
    assert wb.sheetnames == ["Summary", "Documents"] and wb["Documents"].max_row == 1 + 4 + 1


def test_invoices_billed_to_another_gstin_belong_to_that_gstins_return(client, users, fake_extract):
    fake_extract.append(make_extracted(invoice_number="SLS/26-27/170", invoice_date="2026-09-08", buyer_gstin=OTHER_GSTIN))
    inv = upload(client, users["priya"], b"%PDF other buyer")["invoice_id"]
    run_worker()
    assert client.post(f"/api/v1/invoices/{inv}/approve", headers=h(users["ravi"])).status_code == 200
    result = send(client, users, gstr2b_file("092026", [])).json()
    assert result["missing_from_2b"] == []


def test_a_late_filing_clears_the_earlier_month_without_uploading_it_again(client, users, fake_extract):
    late = approve(client, users, fake_extract, "SLS/26-27/130", "2026-09-28")
    sept = send(client, users, gstr2b_file("092026", [])).json()
    assert [m["invoice_id"] for m in sept["missing_from_2b"]] == [late]
    send(client, users, gstr2b_file("102026", [doc("SLS/26-27/130", "28-09-2026")]))
    again = client.get(f"/api/v1/gstr2b/{sept['return']['id']}").json()
    assert again["missing_from_2b"] == [] and again["summary"]["itc_at_risk"] == 0


def test_an_invoice_approved_after_the_upload_is_matched_when_asked_again(client, users, fake_extract):
    first = send(client, users, gstr2b_file("092026", [doc("SLS/26-27/140", "12-09-2026")])).json()
    assert by_number(first)["SLS/26-27/140"]["status"] == "not_in_books"
    inv = approve(client, users, fake_extract, "SLS/26-27/140", "2026-09-12")
    again = client.get(f"/api/v1/gstr2b/{first['return']['id']}").json()
    assert by_number(again)["SLS/26-27/140"]["invoice_id"] == inv


def test_itc_not_available_and_credit_notes(client, users, fake_extract):
    blocked = approve(client, users, fake_extract, "SLS/26-27/160", "2026-09-03")
    result = send(client, users, gstr2b_file(
        "092026", [doc("SLS/26-27/160", "03-09-2026", itcavl="N", rsn="P")],
        cdnr=[{"ntnum": "CN-7", "typ": "C", "dt": "18-09-2026", "val": 1180, "txval": 1000, "igst": 0,
               "cgst": 90, "sgst": 90, "cess": 0, "itcavl": "Y"}])).json()
    rows = by_number(result)
    assert rows["SLS/26-27/160"]["invoice_id"] == blocked and rows["SLS/26-27/160"]["itc_available"] is False
    assert rows["CN-7"]["doc_type"] == "credit_note" and rows["CN-7"]["status"] == "not_in_books"
    s = result["summary"]
    assert s["itc_in_2b"] == -180                         # the credit note reduces the credit; the blocked one isn't counted
    assert s["itc_at_risk_parts"]["itc_not_available"] == 936


def test_uploading_the_same_month_again_replaces_it(client, users):
    send(client, users, gstr2b_file("092026", [doc("A1", "01-09-2026"), doc("A2", "02-09-2026")]))
    r = send(client, users, gstr2b_file("092026", [doc("A1", "01-09-2026")]))
    assert r.json()["return"]["documents"] == 1
    assert len(client.get("/api/v1/gstr2b").json()) == 1


def test_bad_uploads_are_refused_with_a_reason(client, users):
    r = send(client, users, b"%PDF-1.4 not json", "r2b.pdf")
    assert r.status_code == 422 and "JSON" in r.json()["detail"]
    r = send(client, users, json.dumps({"data": {"docdata": {}}}).encode())
    assert r.status_code == 422 and "return period" in r.json()["detail"]
    r = send(client, users, gstr2b_file("092026", [], gstin=SUPPLIER_GSTIN))
    assert r.status_code == 422 and "not one of your GSTINs" in r.json()["detail"]
    assert client.post("/api/v1/gstr2b", files={"file": ("r.json", gstr2b_file("092026", []))}).status_code == 422


def test_delete(client, users):
    ret = send(client, users, gstr2b_file("092026", [doc("A1", "01-09-2026")])).json()["return"]
    assert client.delete(f"/api/v1/gstr2b/{ret['id']}", headers=h(users["ravi"])).status_code == 204
    assert client.get(f"/api/v1/gstr2b/{ret['id']}").status_code == 404


def test_older_portal_layout_with_items():
    content = json.dumps({"data": {"gstin": OURS, "rtnprd": "092026", "docdata": {"b2b": [{
        "ctin": SUPPLIER_GSTIN, "inv": [{"inum": "X1", "dt": "01-09-2026", "val": 1180,
                                         "items": [{"txval": 600, "cgst": 54, "sgst": 54}, {"txval": 400, "cgst": 36, "sgst": 36}]}]}]}}})
    header, docs = gstr2b.parse(content.encode())
    assert header["period"] == "2026-09"
    assert (docs[0]["taxable"], docs[0]["cgst"], docs[0]["sgst"], docs[0]["doc_date"]) == (1000, 90, 90, "2026-09-01")
