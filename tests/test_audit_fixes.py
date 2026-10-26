# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""Regression tests for the security / breakage audit of 2026-10-16."""
import io
import json
import zipfile
from email.message import EmailMessage as Mail

from openpyxl import Workbook

from app import assistant_tools as T
from app.config import settings
from app.documents import MAX_FILES_PER_UPLOAD, expand
from app.models import Status
from tests.conftest import make_extracted
from tests.test_flow import client, excel_rows, fake_extract, get, h, run_worker, upload   # noqa: F401

MB = 1024 * 1024
BODY_INVOICE = ("TAX INVOICE No. INV-77 from Sri Lakshmi Steels GSTIN 36AAACS1234A1Z3. "
                "Taxable 5,200.00 CGST 468.00 SGST 468.00 Total Rs. 6,136.00 payable in 30 days.")


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


# ---------- containers (zip bomb, too many files)

def test_zip_bomb_is_bounded():
    """10 entries of 15 MB zeros compress to ~150 KB; unpacking all would need 150 MB. The total
    unpacked per upload is capped at 3 x MAX_UPLOAD_MB."""
    limit = settings.max_upload_mb * MB
    bomb = _zip({f"inv{i}.pdf": b"\0" * limit for i in range(10)})
    assert len(bomb) < MB
    docs, skipped = expand("bomb.zip", bomb, limit)
    assert sum(len(d) for _, d in docs) <= 3 * limit
    assert any("too large" in s for s in skipped)


def test_nested_zips_share_one_budget():
    limit = 2 * MB
    inner = _zip({f"a{i}.pdf": b"%PDF" + b"\0" * (limit - 10) for i in range(3)})
    outer = _zip({f"inner{i}.zip": inner for i in range(4)})
    docs, _ = expand("outer.zip", outer, limit)
    assert sum(len(d) for _, d in docs) <= 3 * limit


def test_too_many_files_in_a_zip():
    many = _zip({f"doc{i}.pdf": f"%PDF {i}".encode() for i in range(MAX_FILES_PER_UPLOAD + 30)})
    docs, skipped = expand("many.zip", many, 15 * MB)
    assert len(docs) == MAX_FILES_PER_UPLOAD and any("more than" in s for s in skipped)


# ---------- email body invoice with a signature logo

def test_signature_logo_does_not_hide_a_body_invoice():
    msg = Mail()
    msg["Subject"], msg["From"] = "Invoice INV-77", "billing@sls.example"
    msg.set_content(BODY_INVOICE)
    msg.add_attachment(b"\x89PNG small logo", maintype="image", subtype="png", filename="logo.png",
                       disposition="inline")
    docs, _ = expand("fwd.eml", msg.as_bytes(), 15 * MB)
    assert [name for name, _ in docs] == ["fwd.eml/fwd - body.pdf"]


# ---------- journals only link to the same supplier

def test_journal_is_not_linked_to_another_suppliers_invoice(client, users, fake_extract):
    fake_extract += [make_extracted(invoice_number="1001", supplier_name="Bangalore Fittings",
                                    supplier_gstin="29AABCB1234C1Z5"),
                     make_extracted(document_type="journal_entry", invoice_number="1001", supplier_gstin=None,
                                    supplier_name="Sri Lakshmi Steels Pvt Ltd", voucher_number="JV-9",
                                    voucher_date="2026-09-16")]
    upload(client, users["priya"], b"%PDF other supplier")
    journal = upload(client, users["priya"], b"%PDF journal")["invoice_id"]
    run_worker()
    j = get(client, journal)
    assert j["status"] == Status.NEEDS_REVIEW and "hasn't been received yet" in j["status_reason"]


# ---------- stale assistant proposals

def test_proposal_refused_if_the_invoice_changed_since(client, users, fake_extract):
    fake_extract.append(make_extracted(grand_total=6163))           # to review
    inv_id = upload(client, users["priya"])["invoice_id"]
    run_worker()
    proposal = json.loads(T.propose_action(inv_id, "approve", "looks fine"))["proposal"]
    # someone corrects the invoice after the suggestion was made
    assert client.patch(f"/api/v1/invoices/{inv_id}", headers=h(users["ravi"]),
                        json={"grand_total": 9999}).status_code == 200
    r = client.post(f"/api/v1/invoices/{inv_id}/approve", headers=h(users["priya"]),
                    json={"note": "via assistant", "if_unchanged_since": proposal["version"]})
    assert r.status_code == 409 and "changed after this was suggested" in r.json()["detail"]
    fresh = json.loads(T.propose_action(inv_id, "approve", "checked again"))["proposal"]
    assert client.post(f"/api/v1/invoices/{inv_id}/approve", headers=h(users["priya"]),
                       json={"if_unchanged_since": fresh["version"]}).status_code == 200


# ---------- Excel after a database reset

def test_old_workbook_rows_with_the_same_id_do_not_hide_new_invoices(client, users, fake_extract):
    """The workbook still has a row "Invoice ID 1" from before a database reset: the new invoice 1
    (another supplier and number) must still be written, not marked EXPORTED with no row."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoices"
    ws.append(["Invoice ID", "Supplier", "Supplier GSTIN", "Invoice No"])
    ws.append([1, "Old Supplier", "29AABCB1234C1Z5", "OLD-1"])
    wb.create_sheet("Line Items").append(["Invoice ID"])
    from pathlib import Path
    Path(settings.excel_path).parent.mkdir(parents=True, exist_ok=True)
    from app import excel
    wb.save(excel.workbook_path("2026-27"))
    fake_extract.append(make_extracted())
    inv_id = upload(client, users["priya"])["invoice_id"]
    assert inv_id == 1
    run_worker()
    assert get(client, inv_id)["status"] == Status.EXPORTED
    assert [r[3] for r in excel_rows()] == ["OLD-1", "SLS/26-27/118"]
