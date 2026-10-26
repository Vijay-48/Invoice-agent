"""Reading a document: AI extraction, then the e-invoice QR, then (when checks fail) a second,
guided reading by the stronger model. Used by the worker and by the evaluation script, so what is
measured is what runs.

Second reading (Phase 2 items 2-3): when a check that a misreading can explain fails (totals that
don't add up, a GSTIN with a wrong check digit, an unreadable date, low confidence...), the document
is read once more, by AI_SECOND_PASS_MODEL (gpt-4o), told exactly which checks failed. The reading
with fewer such problems is kept; if both have them, the problem is probably in the document itself
and a person looks at it as before.
"""
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.models import Invoice
from app.pipeline import einvoice, extract
from app.pipeline.checks import Check, FAIL, PASS, WARN
from app.pipeline.journal import JOURNAL
from app.pipeline.validate import normalize_extracted, validate
from app.schemas import ExtractedInvoice

log = logging.getLogger("reading")
# Checks a misreading can cause (not: duplicates, pages, the business rules)
FIXABLE = {"lines", "line_total", "tax_amount", "grand_total", "gst_type", "supplier_gstin", "buyer_gstin",
           "invoice_date", "confidence", "amount", "supplier_name", "invoice_number", "amount_in_words", "hsn_summary"}


@dataclass
class Reading:
    data: ExtractedInvoice
    checks: list[Check] = field(default_factory=list)   # extra checks to store with the invoice
    qr: dict | None = None                                # e-invoice QR values, if read
    second_pass: dict | None = None                       # what the second reading changed


def fixable_problems(data: ExtractedInvoice) -> list[Check]:
    """Failed checks that a better reading could fix (offline: no database)."""
    data = normalize_extracted(data)
    if data.document_type == JOURNAL or not (data.is_invoice or data.looks_like_invoice):
        return []
    if data.currency.upper() != "INR":
        return []   # a foreign supplier: no GSTINs or Indian GST to get right
    # A GSTIN that isn't on the document can't be read better; one read with a wrong check digit can
    missing = {"supplier_gstin": not data.supplier_gstin, "buyer_gstin": not data.buyer_gstin}
    checks = validate(None, Invoice(id=0), data).checks
    return [c for c in checks if c.status == FAIL and not missing.get(c.name)
            and (c.name in FIXABLE or c.name.startswith("line_"))]


def _feedback(problems: list[Check]) -> str:
    listed = "\n".join(f"- {c.message}" for c in problems)
    return ("A first reading of this document failed these checks:\n" + listed + "\n"
            "Read the document again carefully and correct any value that was misread (digits, decimal points, "
            "which column a number belongs to, CGST/SGST vs IGST, GSTIN characters, the date). If the document "
            "itself really shows these values, keep them exactly as printed: never change a number just to make "
            "the totals add up.")


def read_document(file_path: str, name: str, *, hints: str | None = None, passwords: list[str] | None = None,
                  model: str | None = None, extract_fn=None, supplier_notes=None) -> Reading:
    """`model` forces the first reading's model (evaluation). `extract_fn` replaces the AI (tests).
    `supplier_notes(gstin)`: reviewers' earlier corrections for the supplier the first reading found,
    added to the second reading's instructions."""
    run = extract_fn or extract.extract_invoice
    data = run(file_path, name, model=model, hints=hints, passwords=passwords)
    usage, first_model = dict(data._usage), data._model
    data, checks, qr = einvoice.check_document(file_path, data, passwords)
    reading = Reading(data, checks, qr)
    reading.data._usage, reading.data._model = usage, first_model

    problems = fixable_problems(reading.data) if settings.ai_second_pass else []
    if not problems:
        return reading
    second_model = settings.ai_second_pass_model or first_model
    log.info("'%s': %d check(s) failed after the first reading (%s); reading again with %s", name, len(problems),
             ", ".join(c.name for c in problems), second_model)
    feedback = _feedback(problems)
    notes = supplier_notes(reading.data.supplier_gstin) if supplier_notes and reading.data.supplier_gstin else None
    if notes and (not hints or notes not in hints):
        feedback += "\n\n" + notes
    try:
        second = run(file_path, name, model=second_model, hints=hints, passwords=passwords, feedback=feedback)
    except extract.PasswordRequired:
        raise
    except Exception as e:   # the first reading stands; a person reviews it as before
        log.warning("Second reading of '%s' failed (%s); keeping the first", name, e)
        reading.checks.append(Check("second_reading", PASS, f"A second reading was tried but failed: {e}"[:300]))
        return reading
    second_usage = dict(second._usage)
    if qr:
        second, _ = einvoice.apply(second, qr)
    remaining = fixable_problems(second)
    total_usage = {k: (usage.get(k) or 0) + (second_usage.get(k) or 0) for k in ("input_tokens", "output_tokens")}
    fixed = [c.name for c in problems if c.name not in {r.name for r in remaining}]
    swapped = _escapes_checks(reading.data, second, problems)
    if swapped:
        log.warning("'%s': the second reading changed %s; keeping the first", name, swapped)
        reading.second_pass = {"model": second_model, "fixed": [], "changed": []}
        reading.checks.append(Check("second_reading", PASS, f"Read again by {second_model}, but that reading "
                                    f"changed {swapped}; the first reading is kept"))
    elif len(remaining) < len(problems):
        changed = _differences(reading.data, second)
        log.info("'%s': the second reading fixed %s (%d problem(s) left)", name, ", ".join(fixed), len(remaining))
        reading.data = second
        reading.second_pass = {"model": second_model, "fixed": fixed, "changed": changed}
        message = (f"Read again by {second_model} because checks failed; fixed: {', '.join(fixed)}"
                   + (f" (changed {', '.join(changed)})" if changed else ""))
        money_changed = any(c.split(" ")[0] in MONEY_FIELDS for c in changed) or "line items" in changed
        if "hsn_summary" in fixed and changed == ["HSN summary"]:
            # Only the summary moved to agree with the lines: a copy of them, not a better reading of it
            reading.checks.append(Check("second_reading", WARN, message + ". Only the HSN summary changed, so "
                                        "please confirm it against the document"))
        elif money_changed and not _total_in_text(second.grand_total, file_path, passwords):
            # The AI changed amounts and the document's own text can't confirm the new total: a model
            # could have made the numbers add up instead of reading them. A person confirms.
            reading.checks.append(Check("second_reading", WARN, message + ". The corrected total isn't in the "
                                        "document's text, so please confirm the amounts"))
        else:
            reading.checks.append(Check("second_reading", PASS, message))
    else:
        log.info("'%s': the second reading didn't fix the problems; keeping the first", name)
        reading.second_pass = {"model": second_model, "fixed": [], "changed": []}
        reading.checks.append(Check("second_reading", PASS, f"Read again by {second_model}: same problems, so they "
                                                            "are probably in the document itself"))
    reading.data._usage = {**usage, **total_usage, "second_pass": True}
    reading.data._model = f"{first_model} + {second_model}"
    return reading


MONEY_FIELDS = {"taxable", "cgst", "sgst", "igst", "round", "grand"}   # first word of _differences' names


def _escapes_checks(first: ExtractedInvoice, second: ExtractedInvoice, problems: list[Check]) -> str | None:
    """What the second reading changed that it mustn't: becoming another kind of document, another
    currency, or another supplier/buyer (unless that GSTIN was the misread problem) would just move the
    document out of reach of the failed checks."""
    named = {c.name for c in problems}
    if second.document_type != first.document_type:
        return f"the document type ({first.document_type} -> {second.document_type})"
    if (second.currency or "INR").upper() != (first.currency or "INR").upper():
        return f"the currency ({first.currency} -> {second.currency})"
    if first.hsn_summary and not second.hsn_summary:
        return "the HSN summary (dropped, so it can't be checked)"
    for name in ("supplier_gstin", "buyer_gstin"):
        a, b = getattr(first, name), getattr(second, name)
        if name not in named and (a or "") != (b or ""):
            return f"the {name.replace('_', ' ')} ({a} -> {b})"
    return None


def _total_in_text(total: float, file_path: str, passwords: list[str] | None) -> bool:
    """Is this total printed in the document's text layer (PDF text, Excel/CSV/Word)?"""
    from app.documents import PDF_LOCK, TEXT_EXT, document_text
    ext = Path(file_path).suffix.lower()
    try:
        if ext in TEXT_EXT:
            text = document_text(file_path)
        elif ext == ".pdf":
            import pymupdf
            with PDF_LOCK, pymupdf.open(file_path) as doc:
                if doc.needs_pass and not any(doc.authenticate(p) for p in passwords or []):
                    return False
                text = "\n".join(page.get_text() for page in doc)
        else:
            return False   # a photo has no text to check against
    except Exception:
        return False
    digits = re.sub(r"(?<=\d)[,\s](?=\d)", "", text)     # 10,65,908.16 -> 1065908.16
    target = f"{abs(total):.2f}"
    forms = [re.escape(target)] + ([re.escape(target[:-3]) + r"(?![.\d])"] if target.endswith(".00") else [])
    return any(re.search(rf"(?<![\d.]){form}(?!\d)", digits) for form in forms)


def _differences(a: ExtractedInvoice, b: ExtractedInvoice) -> list[str]:
    out = []
    for f in ("supplier_gstin", "buyer_gstin", "invoice_number", "invoice_date", "taxable_total", "cgst", "sgst",
              "igst", "round_off", "grand_total"):
        if getattr(a, f) != getattr(b, f):
            out.append(f"{f.replace('_', ' ')} {getattr(a, f)!r} -> {getattr(b, f)!r}")
    if [l.model_dump() for l in a.lines] != [l.model_dump() for l in b.lines]:
        out.append("line items")
    if [r.model_dump() for r in a.hsn_summary] != [r.model_dump() for r in b.hsn_summary]:
        out.append("HSN summary")
    return out
