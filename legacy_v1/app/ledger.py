"""Markdown ledger books, a file-based stand-in for Tally/ERP posting.

Every posted invoice becomes one purchase voucher (double entry), appended to:

    ledgers/daybook.md                 every voucher, in posting order
    ledgers/suppliers/<GSTIN>.md       party ledger, one file per supplier
    ledgers/users/<user>.md            vouchers posted by that user ("system" = auto-post)

Each voucher carries a hidden `<!-- voucher:<id> -->` marker, so writing the same
invoice twice is a no-op. Pure file logic: no database access here.
"""
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import settings

_lock = threading.Lock()   # one process at a time per file; files are append-only


@dataclass
class VoucherLine:
    description: str
    hsn_sac: str | None
    quantity: float
    unit_price: float
    taxable_value: float
    gst_rate: float


@dataclass
class Voucher:
    invoice_id: int
    invoice_no: str
    invoice_date: str
    supplier_name: str
    supplier_gstin: str | None
    po_number: str | None
    taxable_total: float
    cgst: float
    sgst: float
    igst: float
    grand_total: float
    lines: list[VoucherLine]
    posted_by: str
    posted_at: datetime
    how: str                 # "auto-post" | "approved by <reviewer>" | ...

    @property
    def round_off(self) -> float:
        # Derived, not extracted, so the voucher always balances.
        return round(self.grand_total - self.taxable_total - self.cgst - self.sgst - self.igst, 2)


def _amt(x: float) -> str:
    return f"{x:,.2f}"


def _cell(s: str | None) -> str:
    return (s or "").replace("|", "/").replace("\n", " ").strip()


def safe_name(name: str | None) -> str:
    """Turn a user name or GSTIN into a safe file name."""
    cleaned = re.sub(r"[^A-Za-z0-9._@-]+", "_", (name or "").strip()).strip("._")
    return cleaned.lower()[:80] or "unknown"


def render(v: Voucher) -> str:
    rows: list[tuple[str, float | None, float | None]] = [("Purchase Account", v.taxable_total, None)]
    for ledger, amount in [("Input CGST", v.cgst), ("Input SGST", v.sgst), ("Input IGST", v.igst)]:
        if amount:
            rows.append((ledger, amount, None))
    if v.round_off > 0:
        rows.append(("Round Off", v.round_off, None))
    elif v.round_off < 0:
        rows.append(("Round Off", None, -v.round_off))
    rows.append((f"{_cell(v.supplier_name)} (Sundry Creditor)", None, v.grand_total))
    dr = sum(r[1] or 0 for r in rows)
    cr = sum(r[2] or 0 for r in rows)

    out = [
        f"<!-- voucher:{v.invoice_id} -->",
        f"### Purchase · {_cell(v.invoice_no)} · {v.invoice_date}",
        "",
        f"- **Supplier:** {_cell(v.supplier_name)} ({v.supplier_gstin or 'no GSTIN'})",
        f"- **PO:** {v.po_number or 'non-PO'}",
        f"- **Posted:** {v.posted_at:%Y-%m-%d %H:%M} by `{v.posted_by}` ({v.how}) · AP invoice #{v.invoice_id}",
        "",
        "| Ledger | Dr | Cr |",
        "|---|---:|---:|",
    ]
    out += [f"| {name} | {_amt(d) if d is not None else ''} | {_amt(c) if c is not None else ''} |"
            for name, d, c in rows]
    out += [f"| **Total** | **{_amt(dr)}** | **{_amt(cr)}** |", ""]

    if v.lines:
        out += ["| # | Item | HSN/SAC | Qty | Rate | Taxable | GST % |",
                "|---:|---|---|---:|---:|---:|---:|"]
        out += [f"| {i} | {_cell(l.description)} | {l.hsn_sac or ''} | {l.quantity:g} | "
                f"{_amt(l.unit_price)} | {_amt(l.taxable_value)} | {l.gst_rate:g} |"
                for i, l in enumerate(v.lines, start=1)]
        out.append("")
    out += ["---", ""]
    return "\n".join(out)


def _append_once(path: Path, header: str, marker: str, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if marker in path.read_text(encoding="utf-8"):
            return False
        prefix = ""
    else:
        prefix = header
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(prefix + text)
    return True


def write_voucher(v: Voucher) -> list[str]:
    """Append the voucher to the day book, supplier ledger and user book.
    Returns the file paths. Safe to call again for the same invoice."""
    root = Path(settings.ledger_dir)
    supplier_key = safe_name(v.supplier_gstin or v.supplier_name)
    user_key = safe_name(v.posted_by)
    targets = [
        (root / "daybook.md", "# Day Book: Purchases\n\n"),
        (root / "suppliers" / f"{supplier_key}.md",
         f"# Party Ledger: {_cell(v.supplier_name)}\n\nGSTIN: {v.supplier_gstin or 'n/a'}\n\n"),
        (root / "users" / f"{user_key}.md", f"# Postings by `{v.posted_by}`\n\n"),
    ]
    marker = f"<!-- voucher:{v.invoice_id} -->"
    text = render(v)
    with _lock:
        for path, header in targets:
            _append_once(path, header, marker, text)
    return [str(p) for p, _ in targets]
