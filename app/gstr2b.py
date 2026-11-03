"""GSTR-2B reconciliation: what suppliers filed for us against the invoices we approved (D-105).

GSTR-2B is the GST portal's monthly statement of the purchase invoices and credit/debit notes our suppliers
reported. Input tax credit (ITC) can only be claimed for what it lists, so an approved invoice that is missing
from it, or listed with other amounts, is credit at risk until the supplier files or corrects it.

Finance downloads the month's GSTR-2B as JSON from the portal and uploads it (POST /api/v1/gstr2b). Only the
supplier documents are stored; the comparison with the invoices is made each time the report is asked for, so
an invoice approved after the upload, or found in a later month's GSTR-2B (a supplier filing late), is
counted without uploading again.

Our side ("the books") for one return: invoices APPROVED or EXPORTED, billed to that return's GSTIN, not journal
vouchers. A 2B document matches the invoice with the same supplier GSTIN and invoice number (compared without
punctuation or case); failing that, one from the same supplier with the same date and total.
"""
import hashlib
import json
import logging
import re
from datetime import date, datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Gstr2bDocument, Gstr2bReturn, Invoice, Status
from app.pipeline.journal import JOURNAL
from app.pipeline.journal import doc_type as journal_doc_type
from app.pipeline.validate import company_gstins, gstin_valid, normalize_invoice_no

log = logging.getLogger("gstr2b")
RECORDED = (Status.APPROVED, Status.EXPORTED)
TOLERANCE = 1.0          # rupees: rounding differences between the invoice and what the supplier filed
STATUSES = {
    "matched": "In GSTR-2B, amounts agree",
    "mismatch": "In GSTR-2B with different amounts",
    "not_in_books": "In GSTR-2B, no approved invoice in the app",
    "missing": "Approved in the app, not in any GSTR-2B (ITC at risk)",
}


class Gstr2bError(ValueError):
    """The upload isn't a usable GSTR-2B (shown to the person as it is)."""


# ---------- reading the portal's JSON

def _num(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _date(text) -> str | None:
    """The portal writes dd-mm-yyyy."""
    try:
        return datetime.strptime(str(text).strip(), "%d-%m-%Y").date().isoformat()
    except (TypeError, ValueError):
        return None


def _period(text) -> str | None:
    """rtnprd 'MMYYYY' -> 'YYYY-MM'."""
    m = re.fullmatch(r"(\d{2})(\d{4})", str(text or "").strip())
    return f"{m.group(2)}-{m.group(1)}" if m and 1 <= int(m.group(1)) <= 12 else None


def _amounts(d: dict) -> dict:
    """Amounts of one document: given directly, or (older layouts) per tax-rate item."""
    items = d.get("items") or d.get("itms") or []
    rows = [i.get("itm_det", i) for i in items] if items else [d]
    return {k: round(sum(_num(r.get(src)) for r in rows), 2)
            for k, src in (("taxable", "txval"), ("igst", "igst"), ("cgst", "cgst"), ("sgst", "sgst"), ("cess", "cess"))}


def _document(section: str, doc_type: str, supplier: dict, d: dict, number) -> dict | None:
    gstin = str(supplier.get("ctin") or "").strip().upper()
    number = str(number or "").strip()
    if not gstin or not number:
        return None
    return {"section": section, "doc_type": doc_type, "supplier_gstin": gstin[:15],
            "supplier_name": str(supplier.get("trdnm") or "").strip()[:255] or None,
            "number": number[:100], "number_norm": normalize_invoice_no(number)[:100] or number[:100],
            "doc_date": _date(d.get("dt")), **_amounts(d), "total": _num(d.get("val")),
            "itc_available": str(d.get("itcavl") or "Y").strip().upper() != "N",
            "itc_reason": (str(d.get("rsn") or "").strip() or None),
            "reverse_charge": str(d.get("rev") or "N").strip().upper() == "Y",
            "supplier_filed_on": _date(supplier.get("supfildt"))}


def parse(content: bytes) -> tuple[dict, list[dict]]:
    """-> ({gstin, period, generated_on}, documents). Raises Gstr2bError for anything that isn't a GSTR-2B."""
    try:
        doc = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Gstr2bError("Not a JSON file: download GSTR-2B from the GST portal as JSON and upload that") from None
    data = doc.get("data", doc) if isinstance(doc, dict) else {}
    if not isinstance(data, dict):
        raise Gstr2bError("This doesn't look like a GSTR-2B")
    gstin, period = str(data.get("gstin") or "").strip().upper(), _period(data.get("rtnprd"))
    if not gstin_valid(gstin) or not period:
        raise Gstr2bError("This doesn't look like a GSTR-2B: it has no valid GSTIN or return period (rtnprd)")
    sections = data.get("docdata") or {}
    out = []
    for section in ("b2b", "b2ba"):
        for supplier in sections.get(section) or []:
            for d in supplier.get("inv") or []:
                out.append(_document(section, "invoice", supplier, d, d.get("inum")))
    for section in ("cdnr", "cdnra"):
        for supplier in sections.get(section) or []:
            for d in supplier.get("nt") or []:
                kind = "debit_note" if str(d.get("typ") or "C").strip().upper() == "D" else "credit_note"
                out.append(_document(section, kind, supplier, d, d.get("ntnum")))
    return {"gstin": gstin, "period": period, "generated_on": _date(data.get("gendt"))}, [d for d in out if d]


def store(session: Session, content: bytes, file_name: str, actor: str) -> Gstr2bReturn:
    """Save an uploaded GSTR-2B, replacing an earlier upload for the same GSTIN and month."""
    header, documents = parse(content)
    ours = company_gstins()
    if ours and header["gstin"] not in ours:
        raise Gstr2bError(f"This GSTR-2B is for {header['gstin']}, not one of your GSTINs ({', '.join(sorted(ours))})")
    replaced = session.execute(delete(Gstr2bReturn).where(Gstr2bReturn.gstin == header["gstin"],
                                                          Gstr2bReturn.period == header["period"])).rowcount
    ret = Gstr2bReturn(**header, file_name=file_name[:255], file_hash=hashlib.sha256(content).hexdigest(),
                       documents=len(documents), uploaded_by=actor)
    session.add(ret)
    session.flush()
    session.add_all(Gstr2bDocument(return_id=ret.id, **d) for d in documents)
    log.info("GSTR-2B %s %s uploaded by %s: %d document(s)%s", ret.gstin, ret.period, actor, len(documents),
             " (replaced the earlier upload)" if replaced and replaced > 0 else "")
    return ret


# ---------- comparing with the invoices

def _tax(igst, cgst, sgst) -> float:
    return round(float(igst or 0) + float(cgst or 0) + float(sgst or 0), 2)


def _book(inv: Invoice) -> dict:
    x = inv.extracted or {}
    return {"invoice_id": inv.id, "status": inv.status, "supplier_gstin": inv.supplier_gstin,
            "supplier_name": inv.supplier_name, "number": inv.invoice_no, "date": inv.invoice_date,
            "doc_type": x.get("document_type"), "taxable": _num(x.get("taxable_total")),
            "tax": _tax(x.get("igst"), x.get("cgst"), x.get("sgst")),
            "total": float(inv.grand_total or 0)}


def _books(session: Session, gstin: str, where) -> list[Invoice]:
    """Recorded invoices billed to `gstin` (the buyer GSTIN as read) that match `where`."""
    return list(session.scalars(select(Invoice).where(
        Invoice.status.in_(RECORDED), Invoice.supplier_gstin.is_not(None), journal_doc_type() != JOURNAL,
        Invoice.extracted["buyer_gstin"].astext == gstin, where)))


def _month_end(period: str) -> str:
    year, month = map(int, period.split("-"))
    return (date(year + month // 12, month % 12 + 1, 1)).isoformat()


def reconcile(session: Session, ret: Gstr2bReturn) -> dict:
    docs = list(session.scalars(select(Gstr2bDocument).where(Gstr2bDocument.return_id == ret.id)
                                .order_by(Gstr2bDocument.supplier_gstin, Gstr2bDocument.doc_date, Gstr2bDocument.id)))
    candidates = _books(session, ret.gstin, Invoice.supplier_gstin.in_({d.supplier_gstin for d in docs})) if docs else []
    by_number = {(i.supplier_gstin, i.invoice_no_norm): i for i in candidates}
    used: set[int] = set()
    rows = []
    for d in docs:
        inv, how = by_number.get((d.supplier_gstin, d.number_norm)), "number"
        if inv is None or inv.id in used:
            inv, how = next((i for i in candidates if i.id not in used and i.supplier_gstin == d.supplier_gstin
                             and i.invoice_date == d.doc_date and abs(float(i.grand_total or 0) - float(d.total)) <= TOLERANCE),
                            None), "date_and_amount"
        tax_2b = _tax(d.igst, d.cgst, d.sgst)
        row = {"section": d.section, "doc_type": d.doc_type, "supplier_gstin": d.supplier_gstin,
               "supplier_name": d.supplier_name, "number": d.number, "date": d.doc_date, "taxable": float(d.taxable),
               "tax": tax_2b, "total": float(d.total), "itc_available": d.itc_available, "itc_reason": d.itc_reason,
               "reverse_charge": d.reverse_charge, "supplier_filed_on": d.supplier_filed_on, "invoice_id": None,
               "matched_by": None, "book_taxable": None, "book_tax": None, "difference": None}
        if inv is None:
            row["status"] = "not_in_books"
        else:
            used.add(inv.id)
            book = _book(inv)
            row.update(invoice_id=inv.id, matched_by=how, book_taxable=book["taxable"], book_tax=book["tax"])
            taxable_diff, tax_diff = round(book["taxable"] - row["taxable"], 2), round(book["tax"] - tax_2b, 2)
            if abs(taxable_diff) <= TOLERANCE and abs(tax_diff) <= TOLERANCE:
                row["status"] = "matched"
            else:
                row["status"] = "mismatch"
                row["difference"] = (f"Our invoice: taxable {book['taxable']:,.2f}, tax {book['tax']:,.2f}; "
                                     f"GSTR-2B: taxable {row['taxable']:,.2f}, tax {tax_2b:,.2f}")
                row["tax_difference"] = tax_diff
        rows.append(row)

    # Invoices of this month that no GSTR-2B lists (any month: a supplier may file late)
    filed = select(Gstr2bDocument.id).where(Gstr2bDocument.supplier_gstin == Invoice.supplier_gstin,
                                            Gstr2bDocument.number_norm == Invoice.invoice_no_norm)
    in_month = (Invoice.invoice_date >= f"{ret.period}-01") & (Invoice.invoice_date < _month_end(ret.period))
    missing = [{**_book(i), "status": "missing"} for i in _books(session, ret.gstin, in_month & ~filed.exists())
               if i.id not in used]
    missing.sort(key=lambda r: (r["supplier_gstin"] or "", r["date"] or "", r["invoice_id"]))
    return {"return": serialize(ret), "summary": _summary(rows, missing), "documents": rows, "missing_from_2b": missing}


def _signed(row: dict, value: float) -> float:
    return -value if row["doc_type"] == "credit_note" else value


def _summary(rows: list[dict], missing: list[dict]) -> dict:
    count = {s: sum(1 for r in rows if r["status"] == s) for s in ("matched", "mismatch", "not_in_books")}
    itc_in_2b = sum(_signed(r, r["tax"]) for r in rows if r["itc_available"])
    not_available = sum(_signed(r, r["tax"]) for r in rows if not r["itc_available"] and r["invoice_id"])
    short = sum(max(0.0, r.get("tax_difference", 0)) for r in rows if r["status"] == "mismatch")
    missing_tax = sum(_signed(r, r["tax"]) for r in missing)
    return {**count, "missing_from_2b": len(missing), "documents": len(rows),
            "itc_in_2b": round(itc_in_2b, 2),
            "itc_at_risk": round(missing_tax + short + not_available, 2),
            "itc_at_risk_parts": {"missing_from_2b": round(missing_tax, 2), "filed_lower": round(short, 2),
                                  "itc_not_available": round(not_available, 2)},
            "suppliers_to_chase": sorted({r["supplier_gstin"] for r in missing}
                                         | {r["supplier_gstin"] for r in rows if r["status"] == "mismatch"})}


def serialize(ret: Gstr2bReturn) -> dict:
    return {"id": ret.id, "gstin": ret.gstin, "period": ret.period, "generated_on": ret.generated_on,
            "file_name": ret.file_name, "documents": ret.documents, "uploaded_by": ret.uploaded_by,
            "uploaded_at": ret.created_at}


# ---------- the workbook for the accountant

COLUMNS = [("Status", "status"), ("Supplier GSTIN", "supplier_gstin"), ("Supplier", "supplier_name"),
           ("Document", "doc_type"), ("Number", "number"), ("Date", "date"), ("Taxable (2B)", "taxable"),
           ("Tax (2B)", "tax"), ("Taxable (ours)", "book_taxable"), ("Tax (ours)", "book_tax"),
           ("Difference", "difference"), ("ITC available", "itc_available"), ("Reason", "itc_reason"),
           ("Supplier filed on", "supplier_filed_on"), ("Invoice #", "invoice_id"), ("Matched by", "matched_by")]


def workbook(result: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ret, s = result["return"], result["summary"]
    for row in [("GSTR-2B reconciliation", ""), ("GSTIN", ret["gstin"]), ("Month", ret["period"]),
                ("Uploaded", f"{ret['file_name']} by {ret['uploaded_by']}"), ("", ""),
                *[(STATUSES[k], s[k if k != "missing" else "missing_from_2b"]) for k in STATUSES],
                ("", ""), ("ITC in GSTR-2B", s["itc_in_2b"]), ("ITC at risk", s["itc_at_risk"]),
                ("  of which: invoices missing from 2B", s["itc_at_risk_parts"]["missing_from_2b"]),
                ("  of which: filed with lower tax", s["itc_at_risk_parts"]["filed_lower"]),
                ("  of which: ITC not available", s["itc_at_risk_parts"]["itc_not_available"])]:
        ws.append(row)
    ws["A1"].font = Font(bold=True, size=14)
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 22
    detail = wb.create_sheet("Documents")
    detail.append([title for title, _ in COLUMNS])
    for cell in detail[1]:
        cell.font = Font(bold=True)
    for row in result["documents"] + [{**m, "book_taxable": m["taxable"], "book_tax": m["tax"], "taxable": None,
                                       "tax": None, "itc_available": None} for m in result["missing_from_2b"]]:
        detail.append([STATUSES[row["status"]] if key == "status" else
                       ({True: "Yes", False: "No"}.get(row.get(key), row.get(key)) if key == "itc_available" else row.get(key))
                       for _, key in COLUMNS])
    for col, width in zip("ABCDEFGHIJKLMNOP", (44, 17, 30, 12, 18, 11, 13, 11, 13, 11, 60, 12, 10, 15, 9, 15),
                          strict=True):
        detail.column_dimensions[col].width = width
    detail.freeze_panes = "A2"
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
