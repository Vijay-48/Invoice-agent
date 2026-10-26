"""Validation: GST rules, arithmetic, duplicates, extraction confidence."""
import re
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Invoice, Status
from app.pipeline.checks import Check, PASS, WARN, FAIL
from app.schemas import ExtractedInvoice

GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DUPLICATE_LOCK = 2   # pg advisory lock namespace


def gstin_valid(gstin: str | None) -> bool:
    """Format + checksum validation (does not check whether it's active on the GST portal)."""
    if not gstin or not GSTIN_RE.match(gstin):
        return False
    total = 0
    for i, ch in enumerate(gstin[:14]):
        product = CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return CHARS[(36 - total % 36) % 36] == gstin[14]


def normalize_invoice_no(no: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", no.upper())


def normalize_extracted(data: ExtractedInvoice) -> ExtractedInvoice:
    """Undo common LLM formatting slips before anything is stored or compared."""
    def gstin(g: str | None) -> str | None:
        return re.sub(r"[\s-]", "", g or "").upper() or None

    return data.model_copy(update={
        "supplier_gstin": gstin(data.supplier_gstin),
        "buyer_gstin": gstin(data.buyer_gstin),
        "invoice_number": data.invoice_number.strip(),
        "invoice_date": data.invoice_date.strip(),
        "po_number": (data.po_number or "").strip() or None,
    })


def parse_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def financial_year(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= settings.amount_tolerance


def validate(session: Session, inv: Invoice, data: ExtractedInvoice) -> list[Check]:
    checks: list[Check] = []

    if not data.is_invoice:
        return [Check("document_type", FAIL, "Document does not look like a tax invoice")]

    # Extraction confidence
    if data.confidence < settings.min_confidence:
        checks.append(Check("confidence", FAIL,
                            f"Low extraction confidence {data.confidence:.2f}: {data.low_confidence_fields}"))
    else:
        checks.append(Check("confidence", PASS, f"Confidence {data.confidence:.2f}"))

    # GSTINs
    checks.append(Check("supplier_gstin", PASS if gstin_valid(data.supplier_gstin) else FAIL,
                        f"Supplier GSTIN {data.supplier_gstin}"))
    if settings.company_gstin and data.buyer_gstin != settings.company_gstin:
        checks.append(Check("buyer_gstin", FAIL,
                            f"Billed to {data.buyer_gstin}, expected {settings.company_gstin}"))

    # Intra-state => CGST+SGST, inter-state => IGST
    pos = data.place_of_supply_state_code or (data.buyer_gstin or "")[:2]
    if data.supplier_gstin and pos:
        intra = data.supplier_gstin[:2] == pos
        if intra and (data.igst > 0 or not _close(data.cgst, data.sgst)):
            checks.append(Check("gst_type", FAIL, "Intra-state supply should have equal CGST+SGST and no IGST"))
        elif not intra and (data.cgst > 0 or data.sgst > 0):
            checks.append(Check("gst_type", FAIL, "Inter-state supply should have IGST only"))
        else:
            checks.append(Check("gst_type", PASS, "Intra-state" if intra else "Inter-state"))

    # Arithmetic
    line_taxable = sum(l.taxable_value for l in data.lines)
    expected_tax = sum(l.taxable_value * l.gst_rate / 100 for l in data.lines)
    actual_tax = data.cgst + data.sgst + data.igst
    computed_total = data.taxable_total + actual_tax + data.round_off
    for name, a, b in [("line_total", line_taxable, data.taxable_total),
                       ("tax_amount", expected_tax, actual_tax),
                       ("grand_total", computed_total, data.grand_total)]:
        checks.append(Check(name, PASS if _close(a, b) else FAIL, f"expected {a:.2f}, got {b:.2f}"))

    # Duplicate: same supplier + normalised invoice number + financial year
    inv_date = parse_date(data.invoice_date)
    if inv_date is None:
        checks.append(Check("invoice_date", FAIL, f"Unreadable date {data.invoice_date}"))
        return checks
    if inv_date > date.today():
        checks.append(Check("invoice_date", FAIL, "Invoice date is in the future"))

    fy = financial_year(inv_date)
    # Serialise checks for the same supplier + number until commit, so two workers
    # processing copies of one invoice can't both pass
    session.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
                    {"ns": DUPLICATE_LOCK, "key": f"{data.supplier_gstin}|{normalize_invoice_no(data.invoice_number)}"})
    others = session.scalars(select(Invoice).where(
        Invoice.id != inv.id,
        Invoice.supplier_gstin == data.supplier_gstin,
        Invoice.invoice_no_norm == normalize_invoice_no(data.invoice_number),
        Invoice.status.not_in([Status.REJECTED, Status.FAILED]),
    )).all()
    # An unreadable stored date can't be placed in a year: treat it as a possible duplicate
    dupes = [o for o in others
             if (d := parse_date(o.invoice_date)) is None or financial_year(d) == fy]
    if dupes:
        checks.append(Check("duplicate", FAIL, f"Duplicate of invoice id(s) {[d.id for d in dupes]}"))
    else:
        checks.append(Check("duplicate", PASS, "No duplicate found"))

    return checks
