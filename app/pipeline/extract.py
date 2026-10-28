"""Document Agent: the only LLM step. Classifies the document and extracts the invoice."""
import logging
import os
import re
from pathlib import Path

import pymupdf
from agno.agent import Agent
from agno.media import Image
from agno.models.openai import OpenAIChat

from app.config import settings
from app.documents import PDF_LOCK, TEXT_EXT, document_text, render_dpi
from app.logs import timed
from app.schemas import ExtractedInvoice

log = logging.getLogger("extract")

# Kept short: these instructions and the schema are sent with every document.
INSTRUCTIONS = [
    "Extract data from Indian purchase documents (PDF, scan or photo) for accounts payable.",

    "document_type:\n"
    "- tax_invoice: a supplier's bill with GST under any name (Invoice, Bill, e-Invoice, Invoice cum "
    "Challan; interim, final, retainer, timesheet, recurring, consolidated or commercial invoice; an "
    "overdue reminder restating an invoice).\n"
    "- bill_of_supply: a supplier's bill without GST.\n"
    "- credit_note / debit_note: a supplier's note reducing / adding to an earlier invoice (amounts "
    "positive, as printed).\n"
    "- journal_entry: the buyer's journal voucher recording a supplier invoice (ledgers Dr, supplier "
    "Cr, narration naming the invoice).\n"
    "- Not bills to pay: proforma_invoice, quotation, purchase_order, delivery_challan, receipt, "
    "statement, expense_report, other. For these return '' / 0 / [] and stop.",

    "Invoices and notes:\n"
    "- supplier = the issuer (letterhead, Seller); buyer = Bill To. invoice_number = this document's "
    "number exactly as printed.\n"
    "- lines: one per item: quantity, rate before tax, taxable value, total GST % (timesheet: hours x "
    "hourly rate).\n"
    "- taxable_total; cgst, sgst, igst (0 if not used); round_off; grand_total = the document's total, "
    "not the balance due.\n"
    "- invoice_kind: payment_reminder, consolidated, final, interim, retainer, timesheet, recurring, "
    "import, else regular. reference_invoice_number: the invoice a note or reminder refers to. "
    "currency: ISO code of the amounts as printed ('$' or US address = USD, '€' = EUR, '₹'/Rs = INR); "
    "INR only if no other currency is shown. irn if printed. voucher_number, voucher_date: null.\n"
    "- hsn_summary: copy the printed HSN/SAC-wise summary table, one row per HSN (taxable value, total "
    "tax); [] if the document has none.",

    "Journal vouchers (e.g. Tally): extract the invoice they record.\n"
    "- supplier_name = the party credited ('To X ... Cr'); the company at the top is the buyer. "
    "GSTINs only if printed.\n"
    "- invoice_number, invoice_date from the narration ('Inv.No. X, dt. D'), never the voucher's own "
    "No.; voucher_number, voucher_date = the voucher's No. and Dated.\n"
    "- taxable_total = purchase/expense ledger debits; cgst/sgst/igst = Input tax ledger debits (never "
    "lines); grand_total = party credit + any TDS credited.\n"
    "- lines from the narration's 'qty @ rate + GST x%'; else one line per purchase ledger, quantity 1.\n"
    "- place_of_supply_state_code from a state in the ledger names (e.g. Nagaland 13, West Bengal 19).\n"
    "- invoice_kind regular, currency INR, reference_invoice_number and irn null, hsn_summary [].\n"
    "Example: 'Purchases 4,77,755.00 Dr; Input IGST 85,996.00 Dr; To ITI Limited 5,63,751.00 Cr; "
    "Inv.No.BO25260106, dt.07.02.26 ... 10.058 KM @ Rs.47500 + GST 18%', voucher No. 075 dated 1-Mar-26 "
    "-> supplier ITI Limited, invoice BO25260106 of 2026-02-07, voucher 075 of 2026-03-01, one line "
    "10.058 x 47500 = 477755 at 18%, taxable 477755, igst 85996, total 563751.",

    "Formats: amounts as plain numbers ('4,77,755.00' = 477755); dates YYYY-MM-DD, day first "
    "('07.02.26' = 2026-02-07, '1-Mar-26' = 2026-03-01); GSTIN 15 characters, uppercase. Take exact "
    "numbers from the text layer when one is given.",

    "Before answering: lines must add up to taxable_total, and taxable_total + taxes + round_off must "
    "equal grand_total; if not, re-read. Never invent values: missing = null or 0. Set confidence "
    "below 0.8 and list the fields if the document is blurry or cut off or a value was inferred "
    "(field names as in the schema; a line as lines[N].field, N counting from 1).",
]



def _agent(model_id: str) -> Agent:
    return Agent(
        name="Invoice Extractor",
        model=OpenAIChat(id=model_id, temperature=0,
                         api_key=settings.openai_api_key or None),   # None -> OPENAI_API_KEY env var
        instructions=INSTRUCTIONS,
        output_schema=ExtractedInvoice,   # Agno 1.x used `response_model=` instead
    )


extractor = _agent(settings.openai_model)
# Scans and photos have no text layer to fall back on, so they get the stronger model
scan_extractor = extractor if settings.openai_scan_model == settings.openai_model \
    else _agent(settings.openai_scan_model)
SCAN_TEXT_CHARS = 50   # less real text than this in the PDF = scanned
_other_agents: dict[str, Agent] = {}   # models chosen explicitly (scripts/evaluate.py --model)


def _agent_for(model: str) -> Agent:
    for a in (extractor, scan_extractor):
        if a.model.id == model:
            return a
    if model not in _other_agents:
        _other_agents[model] = _agent(model)
    return _other_agents[model]


class PasswordRequired(Exception):
    """A protected PDF that none of the known passwords open."""


def _open_pdf(path: Path, passwords: list[str]) -> "pymupdf.Document":
    doc = pymupdf.open(path)
    if doc.needs_pass:
        for pw in passwords:
            if pw and doc.authenticate(pw):
                log.info("Opened protected PDF %s with a known password", path.name)
                return doc
        doc.close()
        raise PasswordRequired("The PDF is password-protected and no known password opens it")
    return doc


def prepare_pages(file_path: str, passwords: list[str] | None = None) -> tuple[list[Image], str, int]:
    """-> (page images, text, total pages).
    PDFs: pages rendered to PNG (for vision) plus the text layer (for exact digits).
    Images: the image itself. Excel/CSV/Word: their text only (no images)."""
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext in TEXT_EXT:
        return [], document_text(str(path)), 1
    if ext != ".pdf":
        return [Image(filepath=str(path))], "", 1

    images, texts = [], []
    with PDF_LOCK, _open_pdf(path, passwords or []) as doc:   # MuPDF: one thread at a time
        total = doc.page_count
        page_texts = [p.get_text() for p in doc] if total > settings.max_pages else []
        for i in select_pages(page_texts, total, settings.max_pages):
            page = doc[i]
            png = path.with_name(f"{path.stem}_p{i + 1}.png")
            if not png.exists():   # written under another name first: a crash never leaves half a PNG
                part = png.with_name(png.stem + ".part.png")
                page.get_pixmap(dpi=render_dpi(page.rect, 200)).save(part)
                os.replace(part, png)
            images.append(Image(filepath=str(png)))
            texts.append(f"--- page {i + 1} ---\n{page_texts[i] if page_texts else page.get_text()}")
    return images, "\n".join(texts).strip(), total


BOILERPLATE = re.compile(r"terms\s*(and|&)\s*conditions|conditions of sale|general terms|jurisdiction|"
                         r"declaration|bank details|authori[sz]ed signator", re.IGNORECASE)
AMOUNT = re.compile(r"\d[\d,]*\.\d{2}(?!\d)")


def select_pages(page_texts: list[str], total: int, limit: int) -> list[int]:
    """Which pages to read (0-based, in order). Up to `limit` pages: all of them. A longer document:
    the first page (supplier, buyer, number) and the last (totals) always, then the pages with the most
    amounts, skipping terms-and-conditions pages. Amounts per page is a cheap stand-in for "has line items"."""
    if total <= limit:
        return list(range(total))
    chosen = {0, total - 1}

    def score(i: int) -> int:
        text = page_texts[i]
        amounts = len(AMOUNT.findall(text))
        return amounts - (1000 if BOILERPLATE.search(text) and amounts < 5 else 0)

    for i in sorted(range(1, total - 1), key=lambda i: (-score(i), i)):
        if len(chosen) >= limit:
            break
        chosen.add(i)
    return sorted(chosen)


class AIServiceUnavailable(Exception):
    """OpenAI unreachable, timing out, rate-limited or overloaded: try again later."""


class AIAccountProblem(AIServiceUnavailable):
    """The OpenAI account can't be used (no credits, invalid key): nothing a document can fix.
    Documents wait in the queue until someone fixes the account."""


AI_ACCOUNT_ERRORS = re.compile(
    r"no credits remaining|insufficient_quota|exceeded your current quota|check your plan and billing|"
    r"billing_hard_limit|invalid_api_key|incorrect api key provided|api key has been (invalidated|revoked)|"
    r"account (is )?(deactivated|disabled)", re.IGNORECASE)


def is_ai_account_error(message: str) -> bool:
    return bool(AI_ACCOUNT_ERRORS.search(message or ""))


TRANSIENT_AI_ERRORS = re.compile(
    r"connection error|timed out|timeout|rate limit|rate_limit|too many requests|\b(429|500|502|503|504)\b|"
    r"overloaded|service unavailable|internal server error|bad gateway|temporarily", re.IGNORECASE)


def is_transient_ai_error(message: str) -> bool:
    return bool(TRANSIENT_AI_ERRORS.search(message or ""))


def page_count(file_path: str) -> int:
    if Path(file_path).suffix.lower() != ".pdf":
        return 1
    with PDF_LOCK, pymupdf.open(file_path) as doc:
        return doc.page_count


HSN_HEADER = re.compile(r"\b(?:HSN|SAC)(?:\s*/\s*(?:HSN|SAC))?\b", re.IGNORECASE)   # "HSN/SAC" = one column
HSN_SUMMARY_TITLE = re.compile(r"\b(HSN|SAC)[\s/-]*(SAC[\s/-]*)?(wise|summary)|tax\s+summary", re.IGNORECASE)


def hsn_summary_printed(text: str, lines_have_hsn: bool) -> bool:
    """Is there an HSN-wise summary table in the document's text? A titled summary, or an HSN column a
    second time (the first is the line items'); with no HSN in the line items, one HSN column is the summary."""
    if HSN_SUMMARY_TITLE.search(text):
        return True
    return len(HSN_HEADER.findall(text)) >= (2 if lines_have_hsn else 1)


def extract_invoice(file_path: str, name: str | None = None, model: str | None = None,
                    hints: str | None = None, passwords: list[str] | None = None,
                    feedback: str | None = None) -> ExtractedInvoice:
    """`model` forces one model for every document (evaluation); normally text PDFs and text
    documents use OPENAI_MODEL and scans/photos OPENAI_SCAN_MODEL. `hints`: email context
    (subject, sender, body). `passwords`: tried on protected PDFs. `feedback`: the checks a first
    reading failed (second reading, app/pipeline/reading.py)."""
    name = name or Path(file_path).name
    images, text, pages = prepare_pages(file_path, passwords)
    real_text = len(re.sub(r"--- page \d+ ---|\s", "", text))
    scanned = bool(images) and real_text < SCAN_TEXT_CHARS
    agent = _agent_for(model) if model else (scan_extractor if scanned else extractor)
    log.info("Reading '%s': %d page image(s), %d chars of text%s%s, model %s", name, len(images), real_text,
             " (scan/photo)" if scanned else "", " + email hints" if hints else "", agent.model.id)
    if images:
        prompt = "Extract the invoice data from the attached page images."
        if not scanned:
            prompt += f"\n\nText layer of the PDF:\n{text[:20000]}"
    else:
        prompt = f"Extract the invoice data from this document ({Path(file_path).suffix} file):\n{text}"
    if feedback:
        prompt += f"\n\n{feedback}"
    if hints:
        prompt += ("\n\nEmail it arrived with (context only; when it disagrees with the document, "
                   f"the document is right):\n{hints[:1500]}")
    with timed(log, f"AI extraction of '{name}'", logging.INFO):
        result = agent.run(prompt, images=images or None)
    if not isinstance(result.content, ExtractedInvoice):
        # Agno reports API failures as text ("Connection error.") instead of raising
        message = str(result.content)[:300]
        # Only a failed run is a service problem: text the model wrote (unparseable output)
        # can contain "500" or "timeout" too, and must not be retried as if OpenAI were down
        run_failed = str(getattr(result, "status", "") or "").lower().endswith("error")
        if run_failed and is_ai_account_error(message):
            log.error("OpenAI account problem while reading '%s': %s", name, message)
            raise AIAccountProblem(f"OpenAI account problem: {message}")
        if run_failed and is_transient_ai_error(message):
            log.warning("AI service unavailable for '%s': %s", name, message)
            raise AIServiceUnavailable(f"AI service unavailable: {message}")
        log.error("AI returned no invoice data: %s", message)
        raise ValueError(f"Extractor returned unexpected output: {result.content!r}")
    d = result.content
    if d.hsn_summary and not hsn_summary_printed(text, any(l.hsn_sac for l in d.lines)):
        # Asked to copy the table, the AI also builds one from the lines when none is printed; such a
        # summary only agrees with itself (D-95). Scans have no text to confirm the table with.
        log.info("'%s': HSN summary not found in the document's text; ignored", name)
        d.hsn_summary = []
    d._model = agent.model.id
    m = getattr(result, "metrics", None)   # token counts, when the model reports them
    d._usage = {"input_tokens": getattr(m, "input_tokens", 0) or 0,
                "output_tokens": getattr(m, "output_tokens", 0) or 0, "scanned": scanned, "pages": pages,
                "read_pages": [int(m) for m in re.findall(r"--- page (\d+) ---", text)] or None}
    log.info("AI result: %s, supplier '%s' (%s), invoice '%s' dated %s, total %.2f, %d line(s), "
             "confidence %.2f%s", d.document_type, d.supplier_name, d.supplier_gstin, d.invoice_number,
             d.invoice_date, d.grand_total, len(d.lines), d.confidence,
             f", unsure about {d.low_confidence_fields}" if d.low_confidence_fields else "")
    return d
