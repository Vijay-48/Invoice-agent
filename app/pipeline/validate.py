"""Validation: GST rules, arithmetic, dates, confidence and duplicates.

No master data needed: an invoice that passes every check here (and is within the
auto-approve limit) goes straight to Excel."""
import logging
import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Invoice, Status
from app.pipeline.checks import Check, PASS, WARN, FAIL
from app.pipeline.journal import doc_type as journal_doc_type
from app.pipeline.words import parse_amount
from app.schemas import ExtractedInvoice

log = logging.getLogger("validate")
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DUPLICATE_LOCK = 2   # pg advisory lock namespace
# Invoices that can't be duplicates of anything
NOT_COMPARED = [Status.REJECTED, Status.FAILED, Status.NOT_INVOICE, Status.DUPLICATE]


def gstin_valid(gstin: str | None) -> bool:
    """Format + checksum validation (does not check whether it's active on the GST portal)."""
    if not gstin or not GSTIN_RE.match(gstin):
        return False
    total = 0
    for i, ch in enumerate(gstin[:14]):
        product = CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return CHARS[(36 - total % 36) % 36] == gstin[14]


def company_gstins() -> set[str]:
    """COMPANY_GSTIN may list several registrations, comma-separated."""
    return {re.sub(r"\s", "", g).upper() for g in settings.company_gstin.split(",") if g.strip()}


def normalize_invoice_no(no: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", no.upper())


def normalize_extracted(data: ExtractedInvoice) -> ExtractedInvoice:
    """Undo common LLM formatting slips before anything is stored or compared."""
    def gstin(g: str | None) -> str | None:
        return re.sub(r"[\s-]", "", g or "").upper() or None

    def state_code(c: str | None) -> str | None:
        """'7', '07-Delhi', '29 Karnataka' -> '07', '07', '29'"""
        digits = re.match(r"\s*(\d{1,2})(?!\d)", c or "")
        return digits.group(1).zfill(2) if digits else ((c or "").strip() or None)

    return data.model_copy(update={
        "supplier_name": data.supplier_name.strip(),
        "supplier_gstin": gstin(data.supplier_gstin),
        "buyer_gstin": gstin(data.buyer_gstin),
        "invoice_number": data.invoice_number.strip(),
        "invoice_date": data.invoice_date.strip(),
        "voucher_number": (data.voucher_number or "").strip() or None,
        "voucher_date": (data.voucher_date or "").strip() or None,
        "reference_invoice_number": (data.reference_invoice_number or "").strip() or None,
        "currency": (data.currency or "").strip().upper() or "INR",
        "po_number": (data.po_number or "").strip() or None,
        "place_of_supply_state_code": state_code(data.place_of_supply_state_code),
    })


CONFUSABLE_FROM, CONFUSABLE_TO = "ODQILSBZG", "000115826"
CONFUSABLE = str.maketrans(CONFUSABLE_FROM, CONFUSABLE_TO)


def _look_alike_numbers(session: Session, inv: Invoice, same_supplier, no_norm: str, fy: int) -> list[Invoice]:
    """Other invoices from the supplier whose number differs only by characters that are easily
    misread (0/O, 1/I, 5/S, 8/B...): possibly the same invoice read twice in different ways.
    Compared in the database (Postgres translate): a supplier's numbers usually all have the same
    length, so filtering in Python loaded every invoice of the supplier (D-95)."""
    key = no_norm[:100].translate(CONFUSABLE)
    candidates = session.scalars(select(Invoice).where(
        Invoice.id != inv.id, same_supplier, Invoice.invoice_no_norm != no_norm[:100],
        func.translate(Invoice.invoice_no_norm, CONFUSABLE_FROM, CONFUSABLE_TO) == key,
        Invoice.status.not_in(NOT_COMPARED), journal_doc_type() != "journal_entry").order_by(Invoice.id)).all()
    return [o for o in candidates if (d := parse_date(o.invoice_date)) is None or financial_year(d) == fy]


def parse_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def financial_year(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= settings.amount_tolerance


def arithmetic_checks(data: ExtractedInvoice) -> list[Check]:
    """Line qty x rate, lines vs taxable total, tax vs rates, and the grand total.
    Used for invoices and journal vouchers alike."""
    checks: list[Check] = []
    if not data.lines:
        checks.append(Check("lines", FAIL, "No line items found"))
    for idx, line in enumerate(data.lines, start=1):
        computed = line.quantity * line.unit_price
        if not _close(computed, line.taxable_value) and abs(computed - line.taxable_value) > 0.005 * abs(line.taxable_value):
            checks.append(Check(f"line_{idx}", FAIL, f"'{line.description}': {line.quantity:g} x "
                                f"{line.unit_price:.2f} = {computed:.2f}, document says {line.taxable_value:.2f}"))
    line_taxable = sum(l.taxable_value for l in data.lines)
    expected_tax = sum(l.taxable_value * l.gst_rate / 100 for l in data.lines)
    actual_tax = data.cgst + data.sgst + data.igst
    computed_total = data.taxable_total + actual_tax + data.round_off
    for name, a, b in [("line_total", line_taxable, data.taxable_total),
                       ("tax_amount", expected_tax, actual_tax),
                       ("grand_total", computed_total, data.grand_total)]:
        checks.append(Check(name, PASS if _close(a, b) else FAIL, f"expected {a:.2f}, got {b:.2f}"))
    checks += hsn_summary_checks(data, actual_tax)
    return checks


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def hsn_summary_checks(data: ExtractedInvoice, actual_tax: float) -> list[Check]:
    """The HSN-wise tax summary printed on many invoices is a second set of totals: it must agree with
    the line items (per HSN) and with the invoice's taxable total and taxes. A misread digit in either
    place shows up here. No summary printed = no check."""
    rows = data.hsn_summary
    if not rows:
        return []
    problems: list[str] = []
    summary_taxable = sum(r.taxable_value for r in rows)
    summary_tax = sum(r.tax_amount for r in rows)
    if not _close(summary_taxable, data.taxable_total):
        problems.append(f"HSN summary taxable {summary_taxable:.2f} vs invoice {data.taxable_total:.2f}")
    if not _close(summary_tax, actual_tax):
        problems.append(f"HSN summary tax {summary_tax:.2f} vs invoice {actual_tax:.2f}")
    # One HSN can have several summary rows (one per GST rate): compared as their sum
    by_code: dict[str, float] = {}
    for row in rows:
        if code := _digits(row.hsn_sac):
            by_code[code] = by_code.get(code, 0.0) + row.taxable_value
    for code, summary_taxable_code in by_code.items():
        # A summary may list 4 digits where the lines show 8: one is a prefix of the other
        matching = [l for l in data.lines if _digits(l.hsn_sac)
                    and (_digits(l.hsn_sac).startswith(code) or code.startswith(_digits(l.hsn_sac)))]
        if not matching:
            continue
        line_taxable = sum(l.taxable_value for l in matching)
        if not _close(line_taxable, summary_taxable_code):
            problems.append(f"HSN {code}: lines add up to {line_taxable:.2f}, summary says {summary_taxable_code:.2f}")
    if problems:
        return [Check("hsn_summary", FAIL, "; ".join(problems))]
    return [Check("hsn_summary", PASS, f"{len(rows)} HSN row(s) agree with the lines and totals")]


@dataclass
class Validation:
    checks: list[Check]
    duplicate_of: int | None = None   # set when an identical invoice already exists


def validate(session: Session | None, inv: Invoice, data: ExtractedInvoice) -> Validation:
    """`session` None = offline: every check except the duplicate lookup."""
    result = _validate(session, inv, data)
    for c in result.checks:
        log.debug("Invoice %s check %-15s %-4s %s", inv.id, c.name, c.status, c.message)
    failed = [c.name for c in result.checks if c.status != PASS]
    log.info("Invoice %s: %d check(s), %s%s", inv.id, len(result.checks),
             f"not passed: {', '.join(failed)}" if failed else "all passed",
             f"; duplicate of #{result.duplicate_of}" if result.duplicate_of else "")
    return result


def _validate(session: Session | None, inv: Invoice, data: ExtractedInvoice) -> Validation:
    checks: list[Check] = []

    # Required fields
    for name, value in [("supplier_name", data.supplier_name), ("invoice_number", data.invoice_number)]:
        if not value:
            checks.append(Check(name, FAIL, f"{name.replace('_', ' ').capitalize()} missing"))

    # Extraction confidence
    if data.confidence < settings.min_confidence:
        fields = ", ".join(data.low_confidence_fields) or "unspecified fields"
        checks.append(Check("confidence", FAIL, f"Low extraction confidence {data.confidence:.2f} ({fields})"))
    else:
        checks.append(Check("confidence", PASS, f"Confidence {data.confidence:.2f}"))

    # GSTINs
    checks.append(Check("supplier_gstin", PASS if gstin_valid(data.supplier_gstin) else FAIL,
                        f"Supplier GSTIN {data.supplier_gstin or 'missing'}"))
    company = company_gstins()
    if company:
        buyer = data.buyer_gstin or ""
        if buyer in company:
            checks.append(Check("buyer_gstin", PASS, f"Billed to {buyer}"))
        elif gstin_valid(buyer) and buyer[2:12] in {g[2:12] for g in company}:
            # Same PAN = same company, registered in another state
            checks.append(Check("buyer_gstin", PASS, f"Billed to {buyer} (your company's state {buyer[:2]} GSTIN)"))
        else:
            checks.append(Check("buyer_gstin", FAIL, f"Billed to {buyer or 'no GSTIN'}, not one of your "
                                                     f"GSTINs ({', '.join(sorted(company))})"))

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
    else:
        checks.append(Check("gst_type", WARN, "Place of supply unknown; GST type not checked"))

    checks += arithmetic_checks(data)
    in_words = parse_amount(data.amount_in_words)
    if in_words is not None:
        # The total in words is written separately from the figures: a misread digit shows up here
        same = abs(in_words - abs(data.grand_total)) <= settings.amount_tolerance
        checks.append(Check("amount_in_words", PASS if same else FAIL,
                            f"In words: {in_words:,.2f}" + ("" if same else f", but the figure is {data.grand_total:,.2f}")))
    if data.grand_total <= 0:
        # Adds up (0 = 0), but nothing to pay: a free-of-charge note, or the AI missed the amounts
        checks.append(Check("amount", FAIL, f"Total is {data.grand_total:.2f}: check the amounts"))

    # Date
    inv_date = parse_date(data.invoice_date)
    if inv_date is None:
        checks.append(Check("invoice_date", FAIL, f"Unreadable date '{data.invoice_date}'"))
        return Validation(checks)
    if inv_date > date.today():
        checks.append(Check("invoice_date", FAIL, "Invoice date is in the future"))
    else:
        checks.append(Check("invoice_date", PASS, inv_date.isoformat()))

    # Duplicates: same supplier + invoice number in the same financial year.
    # Same date and amount too = DUPLICATE; otherwise a person should look.
    if session is None:   # offline (scripts/evaluate.py): no database to compare with
        return Validation(checks)
    no_norm = normalize_invoice_no(data.invoice_number)
    session.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
                    {"ns": DUPLICATE_LOCK, "key": f"{data.supplier_gstin}|{no_norm}"})
    # Compared as stored (see evaluate.store): a GSTIN too long to be real is stored as none
    gstin = data.supplier_gstin if data.supplier_gstin and len(data.supplier_gstin) <= 15 else None
    # Without a GSTIN, the supplier's name tells two unregistered suppliers apart
    same_supplier = (Invoice.supplier_gstin == gstin) if gstin else (
        Invoice.supplier_gstin.is_(None) & (func.lower(Invoice.supplier_name) == data.supplier_name.lower()[:255]))
    others = session.scalars(select(Invoice).where(
        Invoice.id != inv.id,
        same_supplier,
        Invoice.invoice_no_norm == no_norm[:100],
        Invoice.status.not_in(NOT_COMPARED),
        journal_doc_type() != "journal_entry",   # journals are matched in journal.py
    ).order_by(Invoice.id)).all()
    fy = financial_year(inv_date)
    same_year = [o for o in others if (d := parse_date(o.invoice_date)) is None or financial_year(d) == fy]
    for o in same_year:
        if o.invoice_date == inv_date.isoformat() and o.grand_total is not None \
                and _close(float(o.grand_total), data.grand_total):
            checks.append(Check("duplicate", FAIL, f"Same invoice as #{o.id}"))
            return Validation(checks, duplicate_of=o.id)
    look_alike = _look_alike_numbers(session, inv, same_supplier, no_norm, fy)
    if look_alike:
        o = look_alike[0]
        checks.append(Check("similar_number", FAIL,
                            f"Invoice number {data.invoice_number} looks like {o.invoice_no} (#{o.id}) from the same "
                            f"supplier (O/0, I/1, S/5 or B/8 read differently?): check it isn't the same invoice"))
    if same_year:
        ids = ", ".join(f"#{o.id}" for o in same_year)
        checks.append(Check("duplicate", FAIL,
                            f"Invoice number already used by {ids} with a different date or amount"))
    else:
        checks.append(Check("duplicate", PASS, "No duplicate found"))
    return Validation(checks)
