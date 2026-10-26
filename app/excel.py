"""Excel output: APPROVED invoices are appended to a workbook, then marked EXPORTED.

    Invoices     one row per invoice
    Line Items   one row per item, linked by Invoice ID

One workbook per financial year (April to March) of the invoice date, e.g. "invoices FY2026-27.xlsx"
next to EXCEL_PATH (D-96): every export loads and saves the whole workbook, so a single file growing
forever made each export slower (37 s at 3,000 invoices, D-95). EXCEL_PER_FINANCIAL_YEAR=false keeps
the single EXCEL_PATH workbook.

The workbook is written to a temp file and swapped in, so a crash never leaves a
half-written file. If Excel has the file open, Windows refuses the swap: the invoices
stay APPROVED and the next attempt (worker loop or next approval) writes them.
Rows already in the sheet (by Invoice ID) are never written twice.
"""
import logging
import os
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Invoice, User, Status, audit

log = logging.getLogger("excel")
EXCEL_LOCK = 3   # pg advisory lock namespace: one writer at a time across processes

MONEY, DATE, DATETIME = "#,##0.00", "yyyy-mm-dd", "yyyy-mm-dd hh:mm"
INVOICE_COLUMNS = [  # (header, width, number format)
    ("Invoice ID", 10, None), ("Supplier", 32, None), ("Supplier GSTIN", 18, None),
    ("Invoice No", 18, None), ("Invoice Date", 12, DATE), ("Buyer GSTIN", 18, None),
    ("Place of Supply", 8, None), ("PO No", 16, None), ("Taxable Value", 14, MONEY),
    ("CGST", 12, MONEY), ("SGST", 12, MONEY), ("IGST", 12, MONEY), ("Round Off", 10, MONEY),
    ("Grand Total", 14, MONEY), ("Approved By", 14, None), ("Approved At", 17, DATETIME),
    ("Source", 8, None), ("From", 28, None), ("User", 16, None), ("File", 28, None),
    ("Document", 14, None),   # new columns go at the end so existing workbooks stay aligned
    ("Voucher No", 12, None), ("Voucher Date", 12, DATE),
    ("Invoice Type", 16, None), ("Ref Invoice", 18, None), ("Currency", 8, None), ("IRN", 30, None),
]
LINE_COLUMNS = [
    ("Invoice ID", 10, None), ("Invoice No", 18, None), ("Supplier", 32, None), ("#", 5, None),
    ("Description", 40, None), ("HSN/SAC", 10, None), ("Qty", 10, "#,##0.###"),
    ("Rate", 12, MONEY), ("Taxable Value", 14, MONEY), ("GST %", 7, None), ("GST Amount", 12, MONEY),
]
SHEETS = {"Invoices": INVOICE_COLUMNS, "Line Items": LINE_COLUMNS}


class ExcelLocked(Exception):
    pass


FY_RE = re.compile(r"^(\d{4})-(\d{2})$")   # "2026-27"


def fy_label(d: date) -> str:
    """Indian financial year of a date: 2026-04-01 .. 2027-03-31 -> "2026-27"."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def workbook_path(fy: str | None = None) -> Path:
    """The workbook of financial year `fy` ("2026-27"); the single EXCEL_PATH workbook when the
    split is off or `fy` is None."""
    base = Path(settings.excel_path)
    if fy is None or not settings.excel_per_financial_year:
        return base
    return base.with_name(f"{base.stem} FY{fy}{base.suffix}")


def workbook_for(inv: Invoice) -> Path:
    """By invoice date, the date accountants file it under; no readable date: the approval date."""
    try:
        d = date.fromisoformat(inv.invoice_date) if inv.invoice_date else None
    except ValueError:
        d = None
    d = d or (_local(inv.approved_at) or datetime.now()).date()
    return workbook_path(fy_label(d))


def list_workbooks() -> list[dict]:
    """Workbooks on disk, newest financial year first; the single workbook (from before the split, or
    with the split off) last, as year None."""
    base = Path(settings.excel_path)
    found = []
    if base.parent.exists():
        pattern = re.compile(re.escape(base.stem) + r" FY(\d{4}-\d{2})" + re.escape(base.suffix) + "$")
        found = sorted(((m.group(1), p) for p in base.parent.iterdir() if (m := pattern.match(p.name))),
                       reverse=True)
    items = [{"year": fy, "file": p.name, "path": p} for fy, p in found]
    if base.exists():
        items.append({"year": None, "file": base.name, "path": base})
    return items


def find_workbook(year: str | None = None) -> Path | None:
    """`year` "2026-27" -> that workbook; empty -> the current financial year's, else the newest there is."""
    if year:
        if not FY_RE.match(year):
            raise ValueError(f"Financial year like 2026-27 expected, got '{year}'")
        path = workbook_path(year)
        return path if path.exists() else None
    if not settings.excel_per_financial_year:
        return workbook_path() if workbook_path().exists() else None
    current = workbook_path(fy_label(date.today()))
    if current.exists():
        return current
    books = list_workbooks()
    return books[0]["path"] if books else None


def _open_workbook(path: Path) -> Workbook:
    if path.exists():
        wb = load_workbook(path)
    else:
        wb = Workbook()
        wb.remove(wb.active)
    for name, columns in SHEETS.items():
        if name not in wb.sheetnames:
            ws = wb.create_sheet(name)
            ws.freeze_panes = "A2"
        ws = wb[name]
        # Write any missing headers (new sheet, or a column added in a newer version)
        for i, (header, width, _fmt) in enumerate(columns, start=1):
            cell = ws.cell(row=1, column=i)
            if cell.value != header:
                cell.value = header
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="305496")
                ws.column_dimensions[get_column_letter(i)].width = width
    return wb


def _key(v) -> str:
    """A cell value as it compares to the database (the formula guard's apostrophe removed)."""
    s = "" if v is None else str(v)
    return s[1:] if s.startswith("'") else s


def _safe(v):
    """Text from documents and emails: control characters removed (openpyxl refuses them and
    the whole export would stop), and = + - @ after optional whitespace kept as text."""
    if not isinstance(v, str):
        return v
    v = ILLEGAL_CHARACTERS_RE.sub("", v)
    # Spreadsheet importers can ignore leading whitespace before a formula.
    return "'" + v if v.lstrip()[:1] in ("=", "+", "-", "@") else v


def _append(ws, columns, values):
    ws.append(values)
    # The row append() just wrote. Not ws.max_row: that scans every cell of the sheet, so each export got
    # slower with the workbook's size (1,000 invoices of 30 lines took 5 minutes, D-95)
    row = ws._current_row
    for i, (_, _w, fmt) in enumerate(columns, start=1):
        if fmt:
            ws.cell(row=row, column=i).number_format = fmt


def _local(dt: datetime | None) -> datetime | None:
    return dt.astimezone().replace(tzinfo=None) if dt else None   # Excel has no time zones


def _as_date(s: str | None):
    """ISO date string -> date (a real Excel date); anything else stays text."""
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return s


DOCUMENT_LABELS = {"journal_entry": "Journal entry", "credit_note": "Credit note", "debit_note": "Debit note"}


def _rows(inv: Invoice, user: User | None):
    d = inv.extracted
    # A credit note reduces what is owed: negative amounts (and quantities, so qty x rate
    # still equals the taxable value) make the sheet's column totals the net payable
    credit = d["document_type"] == "credit_note"

    def amt(x):   # negative for a credit note, whichever sign the AI gave it
        return -abs(x) if credit and x is not None else x
    inv_date = date.fromisoformat(inv.invoice_date) if inv.invoice_date else d["invoice_date"]
    invoice_row = [
        inv.id, d["supplier_name"], d["supplier_gstin"], d["invoice_number"], inv_date,
        d["buyer_gstin"], d["place_of_supply_state_code"], d["po_number"], amt(d["taxable_total"]),
        amt(d["cgst"]), amt(d["sgst"]), amt(d["igst"]), -d["round_off"] if credit else d["round_off"],
        amt(d["grand_total"]),
        inv.approved_by, _local(inv.approved_at), inv.source, inv.sender, user.name if user else None,
        inv.original_filename, DOCUMENT_LABELS.get(d["document_type"], "Invoice"),
        d.get("voucher_number"), _as_date(d.get("voucher_date")),
        (d.get("invoice_kind") or "regular").replace("_", " "), d.get("reference_invoice_number"),
        d.get("currency") or "INR", d.get("irn"),
    ]
    line_rows = [
        [inv.id, d["invoice_number"], d["supplier_name"], i, l["description"], l["hsn_sac"],
         amt(l["quantity"]), l["unit_price"], amt(l["taxable_value"]), l["gst_rate"],
         amt(round(l["taxable_value"] * l["gst_rate"] / 100, 2))]
        for i, l in enumerate(d["lines"], start=1)
    ]
    return [_safe(v) for v in invoice_row], [[_safe(v) for v in r] for r in line_rows]


def export_pending(session: Session, wait: bool = True) -> int:
    """Write every APPROVED invoice to the workbook and mark it EXPORTED.
    Returns how many were exported. Raises ExcelLocked if the file is open in Excel.
    `wait=False`: when another export is running, return 0 at once; it (or the next one) writes these
    too. Loading and saving the whole workbook takes seconds to minutes as it grows (D-95), and worker
    threads or a reviewer's Approve click must not queue behind it."""
    if wait:
        session.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": EXCEL_LOCK})
    elif not session.scalar(text("SELECT pg_try_advisory_xact_lock(:ns, 0)"), {"ns": EXCEL_LOCK}):
        session.commit()
        return 0
    pending = session.scalars(select(Invoice).where(Invoice.status == Status.APPROVED)
                              .order_by(Invoice.approved_at, Invoice.id)).all()
    if not pending:
        session.commit()
        return 0
    users = {u.id: u for u in session.scalars(select(User).where(User.id.in_({i.user_id for i in pending})))}
    groups: dict[Path, list[Invoice]] = defaultdict(list)
    for inv in pending:
        groups[workbook_for(inv)].append(inv)

    written: list[tuple[Invoice, Path]] = []
    unwritable: list[tuple[Invoice, str]] = []
    locked: list[str] = []
    for path, invoices in groups.items():
        # A workbook open in Excel holds back only its own year's invoices
        try:
            problems = _write_workbook(path, invoices, users)
        except ExcelLocked as e:
            locked.append(str(e))
            continue
        bad = {inv.id for inv, _ in problems}
        unwritable += problems
        written += [(inv, path) for inv in invoices if inv.id not in bad]
    if locked and not written and not unwritable:
        session.rollback()
        raise ExcelLocked("; ".join(locked))

    now = datetime.now(timezone.utc)
    for inv, problem in unwritable:
        inv.status = Status.NEEDS_REVIEW
        inv.status_reason = f"Couldn't be written to Excel: {problem}"[:1000]
        audit(session, inv.id, "export", inv.status_reason)
    for inv, path in written:
        inv.status = Status.EXPORTED
        inv.exported_at = now
        inv.error = None
        audit(session, inv.id, "export", f"Written to {path.name}")
    session.commit()
    log.info("Exported %d invoice(s) to %s", len(written), ", ".join(sorted({p.name for _, p in written})) or "-")
    if locked:   # the others are written; these wait, and try_export shows why
        raise ExcelLocked("; ".join(locked))
    return len(written)


def _write_workbook(path: Path, pending: list[Invoice], users: dict[int, User]) -> list[tuple[Invoice, str]]:
    """Append `pending` to the workbook at `path` (created if missing). Returns the invoices that can't
    be written, with the reason. Raises ExcelLocked if the file is open in another program."""
    log.info("Writing %d approved invoice(s) to %s: %s", len(pending), path, [i.id for i in pending])
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        wb = _open_workbook(path)
    except PermissionError as e:
        raise ExcelLocked(f"{path} is open in another program") from e
    inv_ws, line_ws = wb["Invoices"], wb["Line Items"]
    # (Invoice ID, GSTIN, Invoice No) of the rows already written. The id alone isn't enough: after a
    # database reset ids start again at 1, and a new invoice must not be skipped for an old one's row
    already = {(row[0], _key(row[2]), _key(row[3])) for row in inv_ws.iter_rows(min_row=2, max_col=4, values_only=True)}
    ids_in_sheet = {k[0] for k in already}

    unwritable = []
    for inv in pending:
        if (inv.id, _key(inv.supplier_gstin), _key(inv.invoice_no)) in already:
            log.info("Invoice %s is already in the workbook; not adding it again", inv.id)
            continue
        if inv.id in ids_in_sheet:
            log.warning("Invoice ID %s is already in the workbook for a different invoice (database reset?); "
                        "adding this one too", inv.id)
        try:
            invoice_row, line_rows = _rows(inv, users.get(inv.user_id))
        except Exception as e:   # one bad record must not stop every other export
            log.exception("Invoice %s can't be written to Excel; back to review", inv.id)
            unwritable.append((inv, f"{type(e).__name__}: {e}"))
            continue
        _append(inv_ws, INVOICE_COLUMNS, invoice_row)
        for r in line_rows:
            _append(line_ws, LINE_COLUMNS, r)
        log.debug("Invoice %s: 1 invoice row + %d line row(s)", inv.id, len(line_rows))
    for ws in (inv_ws, line_ws):
        ws.auto_filter.ref = ws.dimensions

    tmp = path.with_name(f"~{path.stem}.{os.getpid()}.tmp.xlsx")
    try:
        wb.save(tmp)
        os.replace(tmp, path)
    except PermissionError as e:
        tmp.unlink(missing_ok=True)
        raise ExcelLocked(f"{path} is open in another program; close it and it will be updated") from e
    except Exception:
        tmp.unlink(missing_ok=True)   # e.g. disk full: no half-written temp file left behind
        raise
    return unwritable


_last_problem: str | None = None   # logged once, not on every retry


def try_export(session: Session, wait: bool = True) -> str | None:
    """export_pending() that never raises; returns an error message or None.

    The worker retries every few seconds, so the same problem (typically the workbook
    left open in Excel) is logged once, and shown on the waiting invoices' `error`."""
    global _last_problem
    try:
        export_pending(session, wait)
        if _last_problem:
            log.info("Excel export working again")
            _last_problem = None
        return None
    except ExcelLocked as e:
        problem = str(e)
        if problem != _last_problem:
            log.warning("%s; approved invoices will be written when it is closed", problem)
    except Exception as e:  # noqa: BLE001 - keep the caller (worker/API) alive
        session.rollback()
        problem = f"Excel export failed: {e}"
        if problem != _last_problem:
            log.exception("Excel export failed (will keep retrying)")
    _last_problem = problem
    try:
        session.execute(update(Invoice).where(Invoice.status == Status.APPROVED,
                                              Invoice.error.is_distinct_from(problem)).values(error=problem))
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
    return problem
