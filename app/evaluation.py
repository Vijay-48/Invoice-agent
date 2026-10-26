"""Accuracy measurement against the golden set (golden/).

Each golden document has a JSON file with the correct values, checked by a person:

    {
      "file": "hfcl_invoice.pdf",
      "verified": true,
      "notes": "text PDF, inter-state, over the auto-approve limit",
      "expected": {"document_type": "tax_invoice", "invoice_number": "RV2606000765", ...,
                   "lines": [{"quantity": 12, "unit_price": 75276}], "decision": "NEEDS_REVIEW"},
      "alternatives": {"invoice_number": ["B025260106"]}
    }

Only the fields listed in "expected" are scored, so a document can leave out fields that
are genuinely ambiguous. "alternatives" lists other readings that also count as correct
(e.g. a typewriter O vs 0). "decision" is what the rules should decide; it's predicted
offline (no database), so duplicates and journal linking are not part of it.
"""
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.models import Invoice, Status
from app.pipeline.decide import decide
from app.pipeline.journal import JOURNAL
from app.pipeline.validate import validate, normalize_extracted, normalize_invoice_no
from app.schemas import ExtractedInvoice

MONEY = {"taxable_total", "cgst", "sgst", "igst", "round_off", "grand_total"}
NUMBERS = {"invoice_number", "voucher_number", "reference_invoice_number", "po_number"}
GSTINS = {"supplier_gstin", "buyer_gstin"}
EXACT = {"document_type", "invoice_kind", "currency", "invoice_date", "voucher_date", "irn"}
# The fields the >= 98% accuracy target is about
KEY_FIELDS = ["document_type", "supplier_gstin", "buyer_gstin", "invoice_number", "invoice_date",
              "taxable_total", "cgst", "sgst", "igst", "grand_total"]
AMOUNT_TOLERANCE = 1.0
LINE_KEYS = {"quantity": 0.001, "unit_price": AMOUNT_TOLERANCE, "taxable_value": AMOUNT_TOLERANCE,
             "gst_rate": 0.01, "hsn_sac": None, "description": None}
COMPANY_WORDS = {"ltd", "limited", "pvt", "private", "the", "co", "company", "inc", "llp"}


@dataclass
class FieldResult:
    name: str
    expected: object
    actual: object
    ok: bool
    note: str = ""


@dataclass
class DocResult:
    name: str
    fields: list[FieldResult] = field(default_factory=list)
    decision_expected: str | None = None
    decision_actual: str | None = None
    decision_reason: str = ""
    model: str = ""
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def wrong(self) -> list[FieldResult]:
        return [f for f in self.fields if not f.ok]

    @property
    def wrong_auto_approve(self) -> bool:
        """The worst outcome: approved without a person while something is wrong."""
        return self.decision_actual == Status.APPROVED and (
            self.decision_expected != Status.APPROVED or any(not f.ok for f in self.fields))


def _name_key(s: str) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()
    return " ".join(w for w in words if w not in COMPANY_WORDS)


def _same_name(expected: str, actual: str) -> bool:
    a, b = _name_key(expected), _name_key(actual)
    return bool(a and b) and (a in b or b in a or SequenceMatcher(None, a, b).ratio() >= 0.85)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def same_value(name: str, expected, actual) -> bool:
    if expected is None or expected == "":
        return actual is None or actual == ""
    if name in MONEY:
        a, e = _num(actual), _num(expected)
        return a is not None and abs(a - e) <= AMOUNT_TOLERANCE
    if name in NUMBERS:
        return normalize_invoice_no(str(actual or "")) == normalize_invoice_no(str(expected))
    if name in GSTINS:
        return re.sub(r"\s", "", str(actual or "")).upper() == str(expected).upper()
    if name == "supplier_name":
        return _same_name(str(expected), str(actual or ""))
    if name == "place_of_supply_state_code":
        return str(actual or "").zfill(2) == str(expected).zfill(2)
    if name in EXACT:
        return str(actual or "").strip().lower() == str(expected).strip().lower()
    return str(actual or "").strip() == str(expected).strip()


def _compare_lines(expected: list[dict], actual: list) -> FieldResult:
    problems = []
    if len(actual) != len(expected):
        problems.append(f"{len(actual)} line(s), expected {len(expected)}")
    for i, (e, a) in enumerate(zip(expected, actual), start=1):
        a = a.model_dump() if hasattr(a, "model_dump") else a
        for key, tol in LINE_KEYS.items():
            if key not in e:
                continue
            if tol is None:
                ok = (_same_name(e[key], a.get(key) or "") if key == "description"
                      else str(a.get(key) or "").strip() == str(e[key]).strip())
            else:
                av = _num(a.get(key))
                ok = av is not None and abs(av - float(e[key])) <= tol
            if not ok:
                problems.append(f"line {i} {key}: {a.get(key)!r}, expected {e[key]!r}")
    return FieldResult("lines", f"{len(expected)} line(s)", f"{len(actual)} line(s)", not problems,
                       "; ".join(problems))


def compare(golden: dict, data: ExtractedInvoice) -> list[FieldResult]:
    expected = {k: v for k, v in golden["expected"].items() if k != "decision"}
    alternatives = golden.get("alternatives", {})
    data = normalize_extracted(data)
    results = []
    for name, exp in expected.items():
        if name == "lines":
            results.append(_compare_lines(exp, data.lines))
            continue
        actual = getattr(data, name, None)
        ok = any(same_value(name, option, actual) for option in [exp, *alternatives.get(name, [])])
        results.append(FieldResult(name, exp, actual, ok))
    return results


def predict_decision(data: ExtractedInvoice) -> tuple[str, str]:
    """What the rules would decide, without a database (no duplicate/journal lookups)."""
    data = normalize_extracted(data)
    if data.document_type == JOURNAL:
        return Status.NEEDS_REVIEW, "Journal entry (never auto-approved; links to its invoice when one exists)"
    if not (data.is_invoice or data.looks_like_invoice):
        return decide(data, None)
    stub = Invoice(id=0)
    return decide(data, validate(None, stub, data))
