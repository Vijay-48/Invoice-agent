"""Add a processed invoice to the golden set (golden/), to grow the test set from real work.

    python -m scripts.golden_add 12                  # invoice #12 -> golden/docs + golden/expected
    python -m scripts.golden_add 12 --name abc_credit_note

The expected values are pre-filled from the invoice's current data (AI output plus any
corrections made in review) with "verified": false. Open the JSON next to the document,
fix anything wrong, remove fields that are ambiguous, then set "verified": true. Only
verified documents are used by scripts/evaluate.py.
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

from app.db import SessionLocal
from app.models import Invoice, Status

GOLDEN = Path("golden")
FIELDS = ["document_type", "supplier_name", "supplier_gstin", "buyer_gstin", "invoice_number", "invoice_date",
          "voucher_number", "voucher_date", "place_of_supply_state_code", "po_number", "invoice_kind",
          "reference_invoice_number", "currency", "taxable_total", "cgst", "sgst", "igst", "round_off",
          "grand_total"]


def expected_decision(inv: Invoice) -> str | None:
    """The offline decision the rules should reach (see app/evaluation.py)."""
    doc = (inv.extracted or {}).get("document_type")
    if inv.status == Status.NOT_INVOICE:
        return Status.NOT_INVOICE
    if doc == "journal_entry":
        return Status.NEEDS_REVIEW
    if inv.approved_by == "auto":
        return Status.APPROVED
    if inv.status in (Status.NEEDS_REVIEW, Status.EXPORTED, Status.APPROVED):
        return Status.NEEDS_REVIEW
    return None   # duplicates and rejections depend on other records: not scored


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("invoice_id", type=int)
    p.add_argument("--name", help="file name in golden/ (default: from the original file name)")
    args = p.parse_args()

    with SessionLocal() as s:
        inv = s.get(Invoice, args.invoice_id)
        if inv is None or not inv.extracted:
            sys.exit(f"Invoice {args.invoice_id} not found or not extracted yet")
        name = args.name or re.sub(r"[^a-z0-9]+", "_", Path(inv.original_filename).stem.lower()).strip("_")
        src = Path(inv.file_path)
        (GOLDEN / "docs").mkdir(parents=True, exist_ok=True)
        (GOLDEN / "expected").mkdir(parents=True, exist_ok=True)
        dest = GOLDEN / "docs" / f"{name}{src.suffix.lower()}"
        exp_file = GOLDEN / "expected" / f"{name}.json"
        if exp_file.exists():
            sys.exit(f"{exp_file} already exists; choose another --name")
        shutil.copy(src, dest)
        d = inv.extracted
        expected = {k: d.get(k) for k in FIELDS if k in d}
        expected["lines"] = [{k: l[k] for k in ("description", "quantity", "unit_price", "taxable_value", "gst_rate")}
                             for l in d.get("lines", [])]
        decision = expected_decision(inv)
        if decision:
            expected["decision"] = decision
        exp_file.write_text(json.dumps({
            "file": dest.name, "verified": False,
            "notes": f"From invoice #{inv.id} ({inv.status}); pre-filled, CHECK EVERY VALUE against the document",
            "expected": expected, "alternatives": {},
        }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Added {dest} and {exp_file}.\nCheck the values against the document, then set \"verified\": true.")


if __name__ == "__main__":
    main()
