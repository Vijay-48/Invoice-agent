"""Schema the Agno agent must fill, plus the correction schema used in review.

Extraction fields have no defaults so OpenAI strict structured outputs accepts
them; nullable fields are typed `X | None`."""
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr

DocumentType = Literal["tax_invoice", "bill_of_supply", "credit_note", "debit_note", "journal_entry",
                       "proforma_invoice", "quotation", "purchase_order", "delivery_challan", "receipt",
                       "statement", "expense_report", "other"]
# Documents that change what the company owes a supplier: checked and recorded
INVOICE_TYPES = {"tax_invoice", "bill_of_supply", "credit_note", "debit_note"}

# What kind of bill it is. Some kinds can repeat amounts already recorded, so they
# always go to review (see decide.py).
InvoiceKind = Literal["regular", "recurring", "retainer", "interim", "final", "timesheet",
                      "consolidated", "payment_reminder", "import"]


class ExtractedLine(BaseModel):
    description: str
    hsn_sac: str | None = Field(description="digits only")
    quantity: float
    unit_price: float = Field(description="before tax")
    taxable_value: float
    gst_rate: float = Field(description="total GST %, e.g. 18")


class HsnSummaryRow(BaseModel):
    hsn_sac: str = Field(description="digits only")
    taxable_value: float
    tax_amount: float = Field(description="CGST+SGST or IGST for this HSN, total")


class ExtractedInvoice(BaseModel):
    document_type: DocumentType
    supplier_name: str
    supplier_gstin: str | None
    buyer_gstin: str | None
    invoice_number: str
    invoice_date: str = Field(description="YYYY-MM-DD")
    voucher_number: str | None
    voucher_date: str | None = Field(description="YYYY-MM-DD")
    invoice_kind: InvoiceKind
    reference_invoice_number: str | None
    currency: str = Field(description="ISO code, e.g. INR")
    place_of_supply_state_code: str | None = Field(description="2-digit GST state code")
    po_number: str | None
    irn: str | None
    lines: list[ExtractedLine]
    hsn_summary: list[HsnSummaryRow] = Field(description="the HSN-wise tax summary table if printed, else []")
    taxable_total: float
    cgst: float
    sgst: float
    igst: float
    round_off: float
    grand_total: float
    amount_in_words: str | None = Field(description="the total as written in words, exactly as printed; null if none")
    confidence: float = Field(description="0 to 1")
    low_confidence_fields: list[str]
    _model: str = PrivateAttr(default="")   # which AI model read it (not part of the AI schema)
    _usage: dict = PrivateAttr(default_factory=dict)   # tokens used, for evaluation

    @property
    def is_invoice(self) -> bool:
        return self.document_type in INVOICE_TYPES

    @property
    def looks_like_invoice(self) -> bool:
        """Has what a supplier invoice has, whatever the AI called it. Such documents
        go to review instead of being dropped as NOT_INVOICE."""
        return bool(self.supplier_gstin and self.invoice_number and self.lines and self.grand_total > 0)


def from_stored(data: dict) -> ExtractedInvoice:
    """Rebuild a stored extraction; fills fields added after it was stored."""
    return ExtractedInvoice(**{"voucher_number": None, "voucher_date": None, "invoice_kind": "regular",
                               "reference_invoice_number": None, "currency": "INR", "amount_in_words": None,
                               "hsn_summary": [],
                               **data})


class InvoiceCorrection(BaseModel):
    """PATCH body for review: send only the fields to change. `lines` replaces all lines."""
    supplier_name: str | None = None
    supplier_gstin: str | None = None
    buyer_gstin: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    voucher_number: str | None = None
    voucher_date: str | None = None
    invoice_kind: InvoiceKind | None = None
    reference_invoice_number: str | None = None
    currency: str | None = None
    place_of_supply_state_code: str | None = None
    po_number: str | None = None
    lines: list[ExtractedLine] | None = None
    taxable_total: float | None = None
    cgst: float | None = None
    sgst: float | None = None
    igst: float | None = None
    round_off: float | None = None
    grand_total: float | None = None
    note: str = ""
