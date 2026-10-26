"""E-invoice QR (Phase 2 item 1): read the signed QR, correct the AI's reading, verify the signature."""
import json
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import settings
from app.pipeline import einvoice
from tests.conftest import make_extracted

HFCL = Path(__file__).resolve().parent.parent / "golden" / "docs" / "hfcl_invoice.pdf"
QR_DATA = {"SellerGstin": "06AAACH4041D1Z8", "BuyerGstin": "19AABCT1332L2ZA", "DocNo": "RV2606000765",
           "DocTyp": "INV", "DocDt": "03/07/2026", "TotInvVal": 1065908.16, "ItemCnt": 1, "MainHsnCode": "85176930",
           "Irn": "7445150eb473c0b483e0d79a1031405e5211b843ee7cdb6ae8ec11d7dafb4beb", "IrnDt": "2026-07-03 18:47:00"}


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(private_key, data=QR_DATA):
    return jwt.encode({"data": json.dumps(data), "iss": "NIC"}, private_key, algorithm="RS256", headers={"kid": "TEST"})


def _save_public(key, folder: Path, name="nic.pem"):
    folder.mkdir(exist_ok=True)
    (folder / name).write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))


@pytest.mark.skipif(not HFCL.exists(), reason="golden document not present")
def test_real_einvoice_qr_unverified_only_confirms(monkeypatch):
    """The real HFCL e-invoice without NIC's key: its QR is read, but an unverifiable QR must not
    override the reading (anyone can print a QR): the difference goes to review with both values."""
    monkeypatch.setattr(settings, "einvoice_keys_dir", "")
    misread = make_extracted(supplier_gstin="06AAACH4041D1Z8", buyer_gstin="19AABCT1332L2ZA",
                             invoice_number="RV26O6000765", invoice_date="2026-07-03", irn="7445150e...")
    misread = misread.model_copy(update={"grand_total": 1065980.16})
    data, checks, qr = einvoice.check_document(str(HFCL), misread)
    assert qr["SellerGstin"] == "06AAACH4041D1Z8" and qr["verified"] is None
    assert data.invoice_number == "RV26O6000765" and data.grand_total == 1065980.16      # not overridden
    [check] = checks
    assert check.status == "FAIL" and "not checked" in check.message and "RV2606000765" in check.message


def test_verified_qr_corrects_the_reading(monkeypatch, tmp_path):
    nic = _key()
    _save_public(nic, tmp_path / "keys")
    monkeypatch.setattr(settings, "einvoice_keys_dir", str(tmp_path / "keys"))
    qr = einvoice.parse_token(_token(nic))
    misread = make_extracted(supplier_gstin="06AAACH4041D1Z8", buyer_gstin="19AABCT1332L2ZA",
                             invoice_number="RV26O6000765", invoice_date="2026-07-03", irn=QR_DATA["Irn"])
    data, check = einvoice.apply(misread.model_copy(update={"grand_total": 1065980.16}), qr)
    assert qr["verified"] is True and check.status == "PASS" and "signature verified" in check.message
    assert data.invoice_number == "RV2606000765" and data.grand_total == 1065908.16


def test_forged_qr_cannot_claim_to_be_verified(monkeypatch):
    """A QR payload with its own "verified": true must not override the real signature result."""
    monkeypatch.setattr(settings, "einvoice_keys_dir", "")
    qr = einvoice.parse_token(_token(_key(), {**QR_DATA, "verified": True, "TotInvVal": 10.0}))
    assert qr["verified"] is None
    data, check = einvoice.apply(make_extracted(), qr)
    assert check.status == "FAIL" and data.grand_total == 6136


@pytest.mark.parametrize("payload", [
    {"data": "[1, 2]", "iss": "NIC"},                 # data isn't an object
    {"data": json.dumps({**QR_DATA, "TotInvVal": "lots"}), "iss": "NIC"},
    {"data": "not json", "iss": "NIC"},
])
def test_malformed_qr_is_a_failed_check_not_a_crash(monkeypatch, payload):
    monkeypatch.setattr(settings, "einvoice_keys_dir", "")
    token = jwt.encode(payload, _key(), algorithm="RS256")
    monkeypatch.setattr(einvoice, "find_qr_token", lambda *a: token)
    data, checks, _ = einvoice.check_document("x.pdf", make_extracted(irn="abc"))
    assert data.grand_total == 6136 and checks and checks[0].name == "einvoice_qr"


def test_no_irn_means_no_qr_search(monkeypatch, tmp_path):
    monkeypatch.setattr(einvoice, "find_qr_token", lambda *a: pytest.fail("searched without an IRN"))
    pdf = tmp_path / "plain.pdf"
    import pymupdf
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "TAX INVOICE  Total 6,136.00")
    doc.save(pdf)
    doc.close()
    data, checks, qr = einvoice.check_document(str(pdf), make_extracted(irn=None))
    assert checks == [] and qr is None


def test_signature_checked_with_the_configured_key(monkeypatch, tmp_path):
    nic, forger = _key(), _key()
    _save_public(nic, tmp_path / "keys")
    monkeypatch.setattr(settings, "einvoice_keys_dir", str(tmp_path / "keys"))
    assert einvoice.parse_token(_token(nic))["verified"] is True
    fake = einvoice.parse_token(_token(forger, {**QR_DATA, "TotInvVal": 10.0}))
    assert fake["verified"] is False
    data, check = einvoice.apply(make_extracted(), fake)
    assert check.status == "FAIL" and "NOT valid" in check.message and data.grand_total == 6136   # not used


def test_qr_document_type_and_matching_values(monkeypatch):
    monkeypatch.setattr(settings, "einvoice_keys_dir", "")
    qr = einvoice.parse_token(_token(_key(), {**QR_DATA, "DocTyp": "CRN", "SellerGstin": "36AAACS1234A1Z3",
                                             "BuyerGstin": "36AABCT1234F1ZR", "DocNo": "SLS/26-27/118",
                                             "DocDt": "15/09/2026", "TotInvVal": 6136.0, "Irn": "abc"}))
    data, check = einvoice.apply(make_extracted(irn="abc"), qr)
    # unverified: the type isn't changed, the difference is shown for review
    assert data.document_type == "tax_invoice" and check.status == "FAIL" and "document type" in check.message
    same, check = einvoice.apply(make_extracted(irn="abc", document_type="credit_note"), qr)
    assert check.message.startswith("E-invoice QR matches the document")


def test_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "einvoice_qr", False)
    assert einvoice.check_document("x.pdf", make_extracted(irn="abc")) == (make_extracted(irn="abc"), [], None)


def test_correction_keeps_the_qr_check(monkeypatch):
    """A reviewer's PATCH re-runs the checks: the e-invoice QR comparison must survive (re-done against
    the corrected values), not silently disappear."""
    from fastapi.testclient import TestClient
    from app import worker
    from app.api import app
    from app.db import SessionLocal
    from app.models import Invoice, User
    monkeypatch.setattr(settings, "einvoice_keys_dir", "")
    qr = einvoice.parse_token(_token(_key(), {**QR_DATA, "SellerGstin": "36AAACS1234A1Z3",
                                             "BuyerGstin": "36AABCT1234F1ZR", "DocNo": "SLS/26-27/118",
                                             "DocDt": "15/09/2026", "TotInvVal": 6136.0, "Irn": "abc"}))
    monkeypatch.setattr(einvoice, "find_qr_token", lambda *a: "token")
    monkeypatch.setattr(einvoice, "parse_token", lambda t: qr)
    monkeypatch.setattr(worker, "extract_invoice", lambda *a, **k: make_extracted(irn="abc", grand_total=6163))
    monkeypatch.setattr(worker, "page_count", lambda p: 1)
    with TestClient(app) as c, SessionLocal() as s:
        u = User(name="Q", email="q@example.com")
        s.add(u)
        s.commit()
        h = {"X-User-Id": str(u.id)}
        inv_id = c.post("/api/v1/invoices/upload", headers=h,
                        files={"file": ("q.pdf", b"%PDF-1.4 q", "application/pdf")}).json()["invoice_id"]
        while (i := worker.claim_next()) is not None:
            worker.process(i)
        first = {ch["name"]: ch for ch in c.get(f"/api/v1/invoices/{inv_id}").json()["checks"]}
        assert first["einvoice_qr"]["status"] == "FAIL" and "6163" in first["einvoice_qr"]["message"]
        assert c.patch(f"/api/v1/invoices/{inv_id}", headers=h, json={"grand_total": 6136}).status_code == 200
        after = {ch["name"]: ch for ch in c.get(f"/api/v1/invoices/{inv_id}").json()["checks"]}
        assert after["einvoice_qr"]["status"] == "PASS"            # corrected to the QR's value: now matches
        assert c.patch(f"/api/v1/invoices/{inv_id}", headers=h, json={"grand_total": 7000}).status_code == 200
        again = {ch["name"]: ch for ch in c.get(f"/api/v1/invoices/{inv_id}").json()["checks"]}
        assert again["einvoice_qr"]["status"] == "FAIL"            # differs from the QR: still flagged
        assert s.get(Invoice, inv_id) is not None
