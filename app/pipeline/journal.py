"""Journal posting entries (e.g. "Journal posting Entry against Invoice No. RV2606000765").

A journal is the buyer's own accounting entry for a supplier invoice, so it describes
the same purchase as that invoice. To never count a purchase twice:

  journal arrives, its invoice already here   -> journal is DUPLICATE of the invoice
  journal arrives, invoice not here yet       -> NEEDS_REVIEW (never auto-approved:
                                                  a journal has no GST details to check)
  invoice arrives, journal waiting for review -> journal becomes DUPLICATE of the invoice
  invoice arrives, journal already approved   -> invoice gets a FAIL check (review)

Journals and invoices are matched by normalised invoice number and amount; a journal
usually has no supplier GSTIN, so the supplier can't be part of the match.
"""
import logging
import re

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Invoice, Status, audit
from app.pipeline.checks import Check, PASS, WARN, FAIL
from app.schemas import ExtractedInvoice

log = logging.getLogger("journal")
JOURNAL = "journal_entry"
JOURNAL_LOCK = 4   # pg advisory lock namespace
NOT_MATCHED = [Status.REJECTED, Status.FAILED, Status.NOT_INVOICE, Status.DUPLICATE]
IN_EXCEL = [Status.APPROVED, Status.EXPORTED]


def doc_type():
    """SQL expression for the stored document type ('' when not extracted yet)."""
    return func.coalesce(Invoice.extracted["document_type"].astext, "")


def _lock(session: Session, no_norm: str) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"), {"ns": JOURNAL_LOCK, "key": no_norm})


LEGAL_WORDS = {"ltd", "limited", "pvt", "private", "llp", "inc", "co", "company", "corp", "corporation",
               "the", "ms", "m", "s", "and"}


def _name_words(name: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in LEGAL_WORDS}


def same_supplier(name: str | None, gstin: str | None, other: Invoice) -> bool:
    """Is `other` from the same supplier? GSTINs decide when both are known; otherwise the names'
    significant words ("ITI Limited" ~ "ITI Ltd. Raebareli"). Journals often name only the party."""
    if gstin and other.supplier_gstin:
        return gstin == other.supplier_gstin
    a, b = _name_words(name), _name_words(other.supplier_name)
    return bool(a and b and (a <= b or b <= a))


def _same_amount(a, b: float) -> bool:
    return a is not None and abs(float(a) - b) <= max(settings.amount_tolerance, 1.0)


def check_journal(session: Session, inv: Invoice, data: ExtractedInvoice) -> tuple[list[Check], int | None]:
    """Checks for a journal entry; returns (checks, id of the invoice it belongs to)."""
    from app.pipeline.validate import parse_date, arithmetic_checks   # avoid an import cycle
    checks: list[Check] = []
    if not data.supplier_name:
        checks.append(Check("supplier_name", FAIL, "Journal doesn't name the supplier (party credited)"))
    if not inv.invoice_no_norm:
        checks.append(Check("journal_invoice_no", FAIL, "Journal doesn't mention an invoice number"))
    if data.grand_total <= 0:
        checks.append(Check("journal_amount", FAIL, "Journal has no amount"))
    if parse_date(data.invoice_date) is None:
        checks.append(Check("invoice_date", FAIL, f"Unreadable date '{data.invoice_date}'"))
    if data.grand_total > 0:
        checks += arithmetic_checks(data)   # debits (purchase + input tax) must equal the party credit
    if not inv.invoice_no_norm or data.grand_total <= 0:
        return checks, None

    _lock(session, inv.invoice_no_norm)
    # Another copy of the same journal (e.g. sent twice) is a duplicate of the first
    for other in session.scalars(select(Invoice).where(
            Invoice.id != inv.id, Invoice.invoice_no_norm == inv.invoice_no_norm,
            doc_type() == JOURNAL, Invoice.status.not_in(NOT_MATCHED)).order_by(Invoice.id)):
        if _same_amount(other.grand_total, data.grand_total) and same_supplier(data.supplier_name, data.supplier_gstin, other):
            checks.append(Check("journal_copy", FAIL, f"Same journal entry as #{other.id}"))
            return checks, other.id
    invoices = session.scalars(select(Invoice).where(
        Invoice.id != inv.id, Invoice.invoice_no_norm == inv.invoice_no_norm,
        doc_type() != JOURNAL, Invoice.status.not_in(NOT_MATCHED),
    ).order_by(Invoice.id)).all()
    # Same invoice number from another supplier is a different invoice
    invoices = [o for o in invoices if same_supplier(data.supplier_name, data.supplier_gstin, o)]
    for o in invoices:
        if _same_amount(o.grand_total, data.grand_total):
            checks.append(Check("journal_link", PASS, f"Entry for invoice #{o.id} ({o.invoice_no})"))
            return checks, o.id
    if invoices:
        checks.append(Check("journal_link", WARN, f"Invoice {data.invoice_number} is here as "
                            f"{', '.join(f'#{o.id}' for o in invoices)} but with a different amount"))
    else:
        checks.append(Check("journal_link", WARN, f"Invoice {data.invoice_number} hasn't been received yet"))
    return checks, None


def check_invoice_against_journals(session: Session, inv: Invoice, data: ExtractedInvoice) -> list[Check]:
    """For an invoice: a journal for it that is already approved means it's in Excel already."""
    if not inv.invoice_no_norm:
        return []
    _lock(session, inv.invoice_no_norm)
    approved = session.scalars(select(Invoice).where(
        Invoice.id != inv.id, Invoice.invoice_no_norm == inv.invoice_no_norm,
        doc_type() == JOURNAL, Invoice.status.in_(IN_EXCEL),
    )).all()
    approved = [j for j in approved if _same_amount(j.grand_total, data.grand_total)
                and same_supplier(data.supplier_name, data.supplier_gstin, j)]
    if approved:
        return [Check("journal", FAIL, f"Journal entry #{approved[0].id} for this invoice is already approved; "
                                       f"approving this too would count the purchase twice")]
    return []


def link_waiting_journals(session: Session, inv: Invoice, data: ExtractedInvoice) -> None:
    """An invoice has arrived: journals for it still waiting for review are now duplicates."""
    if not inv.invoice_no_norm:
        return
    waiting = session.scalars(select(Invoice).where(
        Invoice.id != inv.id, Invoice.invoice_no_norm == inv.invoice_no_norm,
        doc_type() == JOURNAL, Invoice.status == Status.NEEDS_REVIEW,
    )).all()
    for j in waiting:
        if _same_amount(j.grand_total, data.grand_total) and same_supplier(data.supplier_name, data.supplier_gstin, j):
            j.status = Status.DUPLICATE
            j.duplicate_of_id = inv.id
            j.status_reason = f"Journal entry for invoice #{inv.id} ({inv.invoice_no})"
            audit(session, j.id, "decide", f"DUPLICATE: {j.status_reason}")
            log.info("Journal %s linked to invoice %s that just arrived", j.id, inv.id)
