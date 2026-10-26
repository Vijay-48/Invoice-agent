"""Document Agent: the only LLM step in the pipeline."""
from pathlib import Path

import pymupdf
from agno.agent import Agent
from agno.media import Image
from agno.models.openai import OpenAIChat

from app.config import settings
from app.schemas import ExtractedInvoice

INSTRUCTIONS = [
    "You extract data from Indian GST supplier invoices for an accounts-payable system.",
    "Copy values exactly as printed. Never guess: if a value is not present, return null.",
    "GSTINs are 15 characters. Return them uppercase without spaces.",
    "The supplier is the party issuing the invoice; the buyer is the party billed.",
    "Amounts are plain numbers in INR without commas or currency symbols.",
    "If the invoice uses IGST, set cgst and sgst to 0, and vice versa.",
    "Convert dates to YYYY-MM-DD (Indian invoices are usually DD/MM/YYYY).",
    "If a page text layer is provided, prefer it for exact numbers and use the images for layout.",
    "Set confidence below 0.8 if the document is blurry, handwritten or partially cut off.",
]

extractor = Agent(
    name="Invoice Extractor",
    model=OpenAIChat(id=settings.openai_model, temperature=0,
                     api_key=settings.openai_api_key or None),   # None -> OPENAI_API_KEY env var
    instructions=INSTRUCTIONS,
    output_schema=ExtractedInvoice,   # Agno 1.x used `response_model=` instead
)


def prepare_pages(file_path: str) -> tuple[list[Image], str]:
    """PDFs: render pages to PNG (for vision) and pull the text layer (for exact digits)."""
    path = Path(file_path)
    if path.suffix.lower() != ".pdf":
        return [Image(filepath=str(path))], ""

    images, texts = [], []
    with pymupdf.open(path) as doc:
        for i, page in enumerate(doc):
            if i >= settings.max_pages:
                break
            png = path.with_name(f"{path.stem}_p{i + 1}.png")
            if not png.exists():
                page.get_pixmap(dpi=200).save(png)
            images.append(Image(filepath=str(png)))
            texts.append(f"--- page {i + 1} ---\n{page.get_text()}")
    return images, "\n".join(texts).strip()


def page_count(file_path: str) -> int:
    if Path(file_path).suffix.lower() != ".pdf":
        return 1
    with pymupdf.open(file_path) as doc:
        return doc.page_count


def extract_invoice(file_path: str) -> ExtractedInvoice:
    images, text = prepare_pages(file_path)
    prompt = "Extract the invoice data from the attached page images."
    if text:
        prompt += f"\n\nText layer of the PDF:\n{text[:20000]}"
    result = extractor.run(prompt, images=images)
    if not isinstance(result.content, ExtractedInvoice):
        raise ValueError(f"Extractor returned unexpected output: {result.content!r}")
    return result.content
