"""GST e-invoice QR code: exact values signed by the government portal (NIC/IRP).

An e-invoice carries a QR code holding a signed token (JWT, RS256, issuer "NIC") with the
seller and buyer GSTIN, document number/type/date, total value and IRN. Those values are
machine-readable, so they replace what the AI read; differences are listed for the reviewer.

The QR is only looked for when the document shows an IRN (the AI read one, or the text layer
has one): decoding a dense QR needs a high-resolution render, which costs a few seconds.
The signature is checked when NIC's public key is available (EINVOICE_KEYS_DIR, *.pem files
from the e-invoice portal); otherwise the values are used and the check says "not verified".
"""
import base64
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pymupdf

from app.config import settings
from app.documents import PDF_LOCK, render_dpi
from app.logs import timed
from app.pipeline.checks import Check, FAIL, PASS
from app.schemas import ExtractedInvoice

log = logging.getLogger("einvoice")
IRN_HINT = re.compile(r"\bIRN\b|\b[0-9a-f]{64}\b", re.IGNORECASE)
DOC_TYPES = {"INV": "tax_invoice", "CRN": "credit_note", "DBN": "debit_note"}
# Where the QR usually is: (x, y, width, height) as fractions of the page
REGIONS = [(0.55, 0.0, 0.45, 0.3), (0.0, 0.0, 0.45, 0.3), (0.55, 0.7, 0.45, 0.3), (0.0, 0.7, 0.45, 0.3),
           (0.3, 0.0, 0.4, 0.3)]
RENDER_DPI = (400, 600)


def _qr_texts(gray) -> list[str]:
    """Every QR text found in a grayscale image (numpy array)."""
    try:   # zxing-cpp, if installed, reads dense QR codes far more reliably
        import zxingcpp
        return [r.text for r in zxingcpp.read_barcodes(gray) if r.text]
    except ImportError:
        pass
    import cv2
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    for img in (cv2.resize(bw, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST),
                cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_NEAREST)):
        try:
            text, _, _ = cv2.QRCodeDetectorAruco().detectAndDecode(img)
        except cv2.error:
            text = ""
        if text:
            return [text]
    return []


def _gray(page, dpi: int, clip):
    import numpy as np
    pix = page.get_pixmap(dpi=render_dpi(clip, dpi), clip=clip, colorspace=pymupdf.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)


def find_qr_token(file_path: str, passwords: list[str] | None = None) -> str | None:
    """The signed e-invoice token from the document's QR code, or None."""
    if Path(file_path).suffix.lower() not in (".pdf", ".png", ".jpg", ".jpeg", ".webp"):
        return None
    # MuPDF only under PDF_LOCK, one render at a time; the QR decoding (the slow part) runs outside it
    with PDF_LOCK:
        doc = pymupdf.open(file_path)
    try:
        with PDF_LOCK:
            if doc.needs_pass and not any(doc.authenticate(p) for p in passwords or []):
                return None
            pages = [(i, doc[i].rect) for i in sorted({0, doc.page_count - 1})]   # first and last page
        for i, r in pages:
            for x, y, w, h in REGIONS:
                clip = pymupdf.Rect(r.width * x, r.height * y, r.width * (x + w), r.height * (y + h))
                for dpi in RENDER_DPI:
                    with PDF_LOCK:
                        image = _gray(doc[i], dpi, clip)
                    for text in _qr_texts(image):
                        if _looks_like_token(text):
                            log.info("E-invoice QR found on page %d of %s", i + 1, Path(file_path).name)
                            return text
        return None
    finally:
        with PDF_LOCK:
            doc.close()


def _looks_like_token(text: str) -> bool:
    return text.count(".") == 2 and len(text) > 200


def _b64json(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def _public_keys() -> list:
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    from cryptography.x509 import load_pem_x509_certificate
    folder = Path(settings.einvoice_keys_dir) if settings.einvoice_keys_dir else None
    keys = []
    for pem in sorted(folder.glob("*.pem")) if folder and folder.is_dir() else []:
        raw = pem.read_bytes()
        try:
            keys.append(load_pem_x509_certificate(raw).public_key() if b"CERTIFICATE" in raw else load_pem_public_key(raw))
        except ValueError as e:
            log.warning("E-invoice key %s can't be read: %s", pem.name, e)
    return keys


def parse_token(token: str) -> dict:
    """The QR's values plus `verified`: True (signature checked), False (signature wrong),
    None (no key configured to check it)."""
    header, payload, _ = token.split(".")
    claims = _b64json(payload)
    data = claims.get("data")
    data = json.loads(data) if isinstance(data, str) else (data or {})
    if not isinstance(claims, dict) or not isinstance(data, dict):
        raise ValueError("the QR token has no e-invoice data")
    verified = None
    keys = _public_keys()
    if keys:
        import jwt
        verified = False
        for key in keys:
            try:
                jwt.decode(token, key=key, algorithms=["RS256"], options={"verify_aud": False, "verify_exp": False})
                verified = True
                break
            except jwt.InvalidTokenError:
                continue
    head = _b64json(header)
    # The document's own fields first, our conclusions last: a forged QR can't claim "verified" itself
    return {**{k: v for k, v in data.items() if k not in ("issuer", "kid", "verified")},
            "issuer": claims.get("iss"), "kid": head.get("kid") if isinstance(head, dict) else None,
            "verified": verified}


def _amount(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _iso_date(d: str | None) -> str | None:
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(d), fmt).date().isoformat()
        except (TypeError, ValueError):
            continue
    return None


def apply(data: ExtractedInvoice, qr: dict) -> tuple[ExtractedInvoice, Check]:
    """A QR whose signature is verified replaces the AI's reading (the check lists what changed).
    Without NIC's key the QR can't be trusted (anyone can print a QR), so it only confirms the
    reading: any difference sends the document to review with both values. A QR with a wrong
    signature is not used at all."""
    if qr.get("verified") is False:
        return data, Check("einvoice_qr", FAIL, "E-invoice QR signature is NOT valid: the QR doesn't come "
                                                "from the government portal (altered or fake e-invoice?)")
    wanted = {
        "supplier_gstin": (qr.get("SellerGstin") or "").upper() or None,
        "buyer_gstin": (qr.get("BuyerGstin") or "").upper() or None,
        "invoice_number": qr.get("DocNo"),
        "invoice_date": _iso_date(qr.get("DocDt")),
        "grand_total": _amount(qr.get("TotInvVal")),
        "irn": qr.get("Irn"),
    }
    if data.document_type in DOC_TYPES.values() and DOC_TYPES.get(qr.get("DocTyp")):
        wanted["document_type"] = DOC_TYPES[qr["DocTyp"]]
    changes, update = [], {}
    for field, value in wanted.items():
        if value in (None, ""):
            continue
        current = getattr(data, field)
        same = abs(float(current or 0) - value) <= settings.amount_tolerance if field == "grand_total" \
            else str(current or "").strip().upper() == str(value).strip().upper()
        if not same:
            update[field] = value
            changes.append(f"{field.replace('_', ' ')} {current!r} -> {value!r}")
    trust = {True: "signature verified", None: "signature not checked (no NIC key configured)",
             "recheck": "signature verified"}[qr.get("verified")]
    if not changes:
        return data, Check("einvoice_qr", PASS, f"E-invoice QR matches the document ({trust})")
    if qr.get("verified") is not True:
        return data, Check("einvoice_qr", FAIL, f"E-invoice QR differs from the document as read ({trust}): "
                                                f"{'; '.join(changes)} (read -> QR). Check which is right")
    log.info("E-invoice QR corrected the AI's reading: %s", "; ".join(changes))
    return data.model_copy(update=update), Check(
        "einvoice_qr", PASS, f"Values taken from the e-invoice QR ({trust}); the AI had read: {'; '.join(changes)}")


def recheck(data: ExtractedInvoice, qr: dict) -> Check:
    """After a person corrected the values: compare them with the QR again (never overriding the
    person). Used when re-checking a corrected invoice, so the QR check isn't lost."""
    if qr.get("verified") is False:
        return Check("einvoice_qr", FAIL, "E-invoice QR signature is NOT valid (altered or fake e-invoice?)")
    _, check = apply(data, {**qr, "verified": None if qr.get("verified") is None else "recheck"})
    return check


def _text_layer(file_path: str, passwords: list[str] | None) -> str:
    if Path(file_path).suffix.lower() != ".pdf":
        return ""
    try:
        with PDF_LOCK, pymupdf.open(file_path) as doc:
            if doc.needs_pass and not any(doc.authenticate(p) for p in passwords or []):
                return ""
            return "\n".join(doc[i].get_text() for i in sorted({0, doc.page_count - 1}))
    except Exception:
        return ""


def check_document(file_path: str, data: ExtractedInvoice, passwords: list[str] | None = None,
                   text: str | None = None) -> tuple[ExtractedInvoice, list[Check], dict | None]:
    """Look for the QR when the document shows an IRN. Returns (data, checks, QR values)."""
    if not settings.einvoice_qr:
        return data, [], None
    if text is None and not data.irn:
        text = _text_layer(file_path, passwords)
    if not (data.irn or IRN_HINT.search(text or "")):
        return data, [], None
    with timed(log, f"E-invoice QR search in {Path(file_path).name}"):
        try:
            token = find_qr_token(file_path, passwords)
        except Exception:   # a broken file must not stop the invoice; it just isn't checked
            log.exception("E-invoice QR search failed for %s", Path(file_path).name)
            return data, [], None
    if token is None:
        msg = "IRN printed but the e-invoice QR couldn't be read; values are as read by the AI"
        return data, [Check("einvoice_qr", PASS, msg)], None
    try:   # the QR comes from outside: anything in it may be malformed
        qr = parse_token(token)
        new_data, check = apply(data, qr)
    except Exception as e:
        log.warning("E-invoice QR in %s isn't a valid token: %s", Path(file_path).name, e)
        return data, [Check("einvoice_qr", FAIL, "The QR code isn't a valid e-invoice QR")], None
    return new_data, [check], qr
