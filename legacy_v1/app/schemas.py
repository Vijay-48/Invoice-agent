"""Schema the Agno agent must fill. Fields have no defaults so OpenAI strict
structured outputs accepts it; nullable fields are typed `X | None`."""
from pydantic import BaseModel, Field


class ExtractedLine(BaseModel):
    description: str
    hsn_sac: str | None = Field(description="HSN or SAC code, digits only")
    quantity: float
    unit_price: float = Field(description="Price per unit before tax")
    taxable_value: float = Field(description="Line value before tax")
    gst_rate: float = Field(description="Total GST rate in percent, e.g. 18")


class ExtractedInvoice(BaseModel):
    is_invoice: bool = Field(description="False if the document is not a tax invoice")
    supplier_name: str
    supplier_gstin: str | None
    buyer_gstin: str | None
    invoice_number: str
    invoice_date: str = Field(description="ISO format YYYY-MM-DD")
    place_of_supply_state_code: str | None = Field(description="2-digit GST state code, e.g. 36")
    po_number: str | None = Field(description="Buyer's purchase order number if printed")
    irn: str | None = Field(description="e-Invoice IRN (64 chars) if present")
    lines: list[ExtractedLine]
    taxable_total: float
    cgst: float
    sgst: float
    igst: float
    round_off: float
    grand_total: float
    confidence: float = Field(description="0 to 1, how confident you are in the overall extraction")
    low_confidence_fields: list[str] = Field(description="Names of fields that were hard to read")
