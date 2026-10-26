"""Decision Engine: plain rules, so every outcome is explainable."""
from app.config import settings
from app.models import Status
from app.pipeline.checks import WARN, FAIL
from app.pipeline.validate import Validation
from app.schemas import ExtractedInvoice

RULES_VERSION = "2026-10-06.1"   # stored with each decision for audit


# Kinds of bill that can repeat amounts already recorded
REVIEW_KINDS = {
    "payment_reminder": "Payment reminder for an earlier invoice{ref}: check it isn't already recorded "
                        "and whether late fees are added",
    "consolidated": "Consolidated invoice covering several invoices: check they aren't already recorded "
                    "one by one",
    "final": "Final project invoice: check it doesn't repeat interim invoices already recorded",
}


def always_review(data: ExtractedInvoice) -> list[str]:
    """Reasons a person must look, however clean the numbers are."""
    ref = f" {data.reference_invoice_number}" if data.reference_invoice_number else ""
    reasons = []
    if data.document_type == "credit_note":
        reasons.append(f"Credit note{' against invoice' + ref if ref else ''}: reduces what you owe")
    elif data.document_type == "debit_note":
        reasons.append(f"Debit note{' against invoice' + ref if ref else ''}: adds to what you owe")
    if data.invoice_kind in REVIEW_KINDS:
        reasons.append(REVIEW_KINDS[data.invoice_kind].format(ref=ref))
    if data.currency.upper() != "INR":
        reasons.append(f"Amounts are in {data.currency.upper()}, not INR")
    return reasons


def decide(data: ExtractedInvoice, result: Validation | None) -> tuple[str, str]:
    """`result` is None for documents that weren't validated (clearly not invoices)."""
    doc_type = data.document_type.replace("_", " ")
    if result is None:
        return Status.NOT_INVOICE, f"Not an invoice (looks like: {doc_type})"
    if result.duplicate_of:
        return Status.DUPLICATE, f"Same invoice as #{result.duplicate_of}"
    problems: list[str] = [c.message for c in result.checks if c.status in (FAIL, WARN)]
    if not data.is_invoice:
        # The AI hesitated, but it has a supplier GSTIN, number, items and total
        problems.insert(0, f"AI classified it as '{doc_type}' but it looks like an invoice; please check")
    problems = always_review(data) + problems
    if problems:
        return Status.NEEDS_REVIEW, "; ".join(problems)
    if data.grand_total > settings.auto_approve_limit:
        return Status.NEEDS_REVIEW, (f"Amount {data.grand_total:,.2f} above auto-approve limit "
                                     f"{settings.auto_approve_limit:,.2f}")
    return Status.APPROVED, "All checks passed"
