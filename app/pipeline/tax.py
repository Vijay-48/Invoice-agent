"""Tax treatment the checks can work out from the invoice (D-109): the TDS section and amount the company
deducts when it pays, GST it pays itself under reverse charge (RCM), and input tax credit (ITC) that GST
section 17(5) blocks. Plain rules on the HSN/SAC codes of the lines: the AI decides none of it.

The rules, rates and thresholds below are as understood on 2026-11-03 (Finance Act 2025). They are a
starting point for the CA to confirm and edit; RULES_VERSION goes into the audit with each result.
Not covered: 194Q (purchase of goods: depends on the company's turnover), lower-deduction certificates,
payments to non-residents (195), the higher rate for non-filers (206AB)."""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Invoice, Status
from app.pipeline.checks import PASS, WARN, Check
from app.pipeline.validate import gstin_valid, parse_date
from app.schemas import ExtractedInvoice

RULES_VERSION = "2026-11-03.1 (to be confirmed by the CA)"
NO_PAN_RATE = 20.0          # section 206AA: no valid PAN
RECORDED = [Status.APPROVED, Status.EXPORTED]


@dataclass(frozen=True)
class TdsRule:
    section: str
    prefixes: tuple[str, ...]   # SAC codes (lines whose HSN/SAC starts with one of these)
    what: str
    rate_individual: float      # supplier's PAN is an individual's or HUF's (4th character P or H)
    rate_other: float
    single_limit: float | None  # deduct when one invoice is above this...
    year_limit: float | None    # ...or the financial year's total for the supplier is
    note: str = ""


TDS_RULES = [
    TdsRule("194C", ("9954", "9965", "9966", "9967", "9985", "9987", "99836"),
            "contract work (works contract, transport, manpower, security, repairs, advertising)",
            1, 2, 30_000, 100_000),
    TdsRule("194J", ("9982", "9983"), "professional or technical fees", 10, 10, None, 50_000,
            "2% instead if it is a technical service rather than a professional one"),
    TdsRule("194I", ("99721",), "rent of land or building", 10, 10, 50_000, None,
            "the threshold is per month: an invoice for several months may be below it"),
    TdsRule("194I", ("9973",), "rent of plant or machinery", 2, 2, 50_000, None,
            "the threshold is per month: an invoice for several months may be below it"),
    TdsRule("194H", ("9961", "9962"), "commission or brokerage", 2, 2, None, 20_000),
]

# (SAC prefix, service, GST rate the company pays) when the supplier charged no GST on the line
RCM_RULES = [
    ("9965", "goods transport by a transport agency (GTA)", 5),
    ("99821", "legal services by an advocate or firm of advocates", 18),
    ("998525", "security services by a supplier other than a company", 18),
    ("997211", "renting of a residential dwelling", 18),
]

# (HSN/SAC prefix, what it is) where GST section 17(5) blocks the credit
BLOCKED_ITC = [
    ("8703", "a motor car"),
    ("8711", "a motorcycle"),
    ("99633", "food and beverages"),
    ("9993", "a health service"),
    ("99972", "a beauty or wellness treatment"),
    ("9954", "construction work (blocked when it becomes a building or other immovable property)"),
]


def _match(code: str | None, prefixes) -> str | None:
    """The longest prefix the code starts with."""
    code = code or ""
    found = [p for p in prefixes if code.startswith(p)]
    return max(found, key=len) if found else None


def tds_rule(code: str | None) -> TdsRule | None:
    best = None
    for rule in TDS_RULES:
        p = _match(code, rule.prefixes)
        if p and (best is None or len(p) > len(best[0])):
            best = (p, rule)
    return best[1] if best else None


def pan_of(gstin: str | None) -> str | None:
    return gstin[2:12] if gstin and gstin_valid(gstin) else None


def financial_year(d: date) -> tuple[str, str]:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-04-01", f"{start + 1}-03-31"


def _year_so_far(session: Session, inv: Invoice, gstin: str, section: str, when: date) -> float:
    start, end = financial_year(when)
    total = session.scalar(select(func.coalesce(func.sum(Invoice.tds_base), 0)).where(
        Invoice.id != inv.id, Invoice.supplier_gstin == gstin, Invoice.tds_section == section,
        Invoice.status.in_(RECORDED), Invoice.invoice_date >= start, Invoice.invoice_date <= end))
    return float(total or 0)


def _rupees(x: float) -> str:
    return f"₹{x:,.2f}"


def check_tds(session: Session, inv: Invoice, data: ExtractedInvoice) -> list[Check]:
    """Sets inv.tds_*. Information (PASS) unless a person has something to do."""
    inv.tds_section = inv.tds_base = inv.tds_rate = inv.tds_amount = None
    if not settings.tds_checks or data.document_type == "credit_note":
        return []
    bases: dict[str, float] = {}
    rules: dict[str, TdsRule] = {}
    for ln in data.lines:
        rule = tds_rule(ln.hsn_sac)
        if rule:
            key = f"{rule.section}:{rule.what}"
            bases[key] = bases.get(key, 0) + ln.taxable_value
            rules[key] = rule
    if not bases:
        return []
    key = max(bases, key=bases.get)
    rule, base = rules[key], round(bases[key], 2)
    others = [f"{rules[k].section} on {_rupees(v)}" for k, v in bases.items() if k != key]
    inv.tds_section, inv.tds_base = rule.section, base
    head = f"TDS {rule.section} ({rule.what})"
    also = f". Other lines may need {', '.join(others)}" if others else ""
    note = f" ({rule.note})" if rule.note else ""

    pan = pan_of(data.supplier_gstin)
    when = parse_date(data.invoice_date) or date.today()
    prior = _year_so_far(session, inv, data.supplier_gstin, rule.section, when) if pan else 0.0
    over_single = rule.single_limit is not None and base > rule.single_limit
    over_year = rule.year_limit is not None and prior + base > rule.year_limit
    if not (over_single or over_year):
        limit = rule.single_limit if rule.year_limit is None else rule.year_limit
        so_far = "this invoice" if rule.year_limit is None else f"{_rupees(prior + base)} this financial year"
        return [Check("tds", PASS, f"{head}: not due yet, {so_far} is within {_rupees(limit)}{also}")]

    if pan is None:
        inv.tds_rate = NO_PAN_RATE
        inv.tds_amount = round(base * NO_PAN_RATE / 100)
        return [Check("tds", WARN, f"{head}: the supplier's PAN isn't known (no valid GSTIN), so TDS is "
                                   f"{NO_PAN_RATE:g}% = {_rupees(inv.tds_amount)}; get the PAN{also}")]
    rate = rule.rate_individual if pan[3] in "PH" else rule.rate_other
    inv.tds_rate, inv.tds_amount = rate, round(base * rate / 100)
    text = f"{head}: deduct {rate:g}% of {_rupees(base)} = {_rupees(inv.tds_amount)}{note}{also}"
    if over_year and 0 < prior <= rule.year_limit:
        # Crossing the year's threshold makes the earlier invoices of the year liable too
        return [Check("tds", WARN, f"{text}. This invoice takes the year's total over "
                                   f"{_rupees(rule.year_limit)}: TDS is due on the earlier {_rupees(prior)} too")]
    return [Check("tds", PASS, text)]


def check_rcm(data: ExtractedInvoice) -> list[Check]:
    found = []
    for ln in data.lines:
        for prefix, what, rate in RCM_RULES:
            if (ln.hsn_sac or "").startswith(prefix) and not ln.gst_rate:
                tax = round(ln.taxable_value * rate / 100, 2)
                found.append(f"'{ln.description}' is {what}: the company pays {rate}% GST = {_rupees(tax)} itself")
                break
    if not found:
        return []
    return [Check("rcm", WARN, "Reverse charge may apply (no GST charged): " + "; ".join(found)
                  + ". It can be claimed back as input tax credit if eligible")]


def check_blocked_itc(data: ExtractedInvoice) -> list[Check]:
    found = []
    for ln in data.lines:
        prefix = _match(ln.hsn_sac, [p for p, _ in BLOCKED_ITC])
        if prefix and ln.gst_rate:
            what = dict(BLOCKED_ITC)[prefix]
            tax = round(ln.taxable_value * ln.gst_rate / 100, 2)
            found.append(f"'{ln.description}' is {what} (GST {_rupees(tax)})")
    if not found:
        return []
    return [Check("itc_blocked", WARN, "Input tax credit may be blocked (GST section 17(5)): " + "; ".join(found)
                  + ". Book that GST as cost unless an exception applies")]


def check_tax(session: Session, inv: Invoice, data: ExtractedInvoice) -> list[Check]:
    return check_tds(session, inv, data) + check_rcm(data) + check_blocked_itc(data)
