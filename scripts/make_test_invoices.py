"""Generate realistic test invoices (PDF) with a known expected outcome.

    python -m scripts.make_test_invoices                      # one of each scenario -> test_invoices/<time>/
    python -m scripts.make_test_invoices --repeat 3 --out my_folder
    python -m scripts.make_test_invoices --only clean_intra,over_limit

Unlike random fake invoices, these follow the rules a real Indian GST invoice follows: valid GSTIN check
digits, billed to your COMPANY_GSTIN, place of supply, CGST+SGST (same state) or IGST (other state),
lines that add up, and the total in words. So the system's decisions can be tested: each file has the
outcome the rules should produce (manifest.json next to the PDFs).

Scenarios (expected status):
    clean_intra    same-state invoice under the auto-approve limit       -> EXPORTED (approved automatically)
    clean_inter    other-state invoice (IGST) under the limit            -> EXPORTED
    over_limit     correct, but above AUTO_APPROVE_LIMIT                 -> NEEDS_REVIEW
    wrong_total    printed grand total doesn't add up                    -> NEEDS_REVIEW
    bad_gstin      supplier GSTIN with a wrong check digit               -> NEEDS_REVIEW
    other_buyer    billed to another company's GSTIN                     -> NEEDS_REVIEW
    credit_note    a credit note against an earlier invoice              -> NEEDS_REVIEW
    duplicate      the same invoice as clean_intra, sent again (other file) -> DUPLICATE
    not_invoice    the supplier's terms & conditions letter (no lines/total) -> NOT_INVOICE
                   (a quotation with GSTIN, lines and a total "looks like an invoice" and goes
                   to review on purpose, so it isn't used here)

The same generator logic is used by scripts/simulate_users.py. Use it as the reference for an outside
generator too (e.g. an n8n workflow): see make_gstin() for the check digit.
"""
import argparse
import html
import io
import json
import random
import string
from datetime import date, datetime, timedelta
from pathlib import Path

import pymupdf

from app.config import settings

CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
STATES = {"19": "West Bengal", "27": "Maharashtra", "29": "Karnataka", "33": "Tamil Nadu", "36": "Telangana",
          "07": "Delhi", "09": "Uttar Pradesh", "24": "Gujarat", "06": "Haryana", "21": "Odisha"}
SUPPLIERS = ["Sri Lakshmi Steels", "Bharat Cable Works", "Kaveri Electricals", "Deccan Fasteners",
             "Ganga Packaging", "Shree Ganesh Traders", "Konark Industrial Supplies", "Nilgiri Tools",
             "Vindhya Polymers", "Sahyadri Hardware"]
ITEMS = [("MS Pipe 25mm", "7306", 450, 18), ("GI Elbow 25mm", "7307", 35, 18), ("Copper wire 2.5 sq mm", "8544", 95, 18),
         ("Cable tray 300mm", "7308", 820, 18), ("Packing box 5-ply", "4819", 42, 12), ("Hex bolt M12", "7318", 6, 18),
         ("Safety helmet", "6506", 260, 18), ("PVC conduit 20mm", "3917", 55, 18), ("Office chair", "9401", 3400, 18),
         ("Printer paper A4 (ream)", "4802", 240, 12)]
SCENARIOS = {
    "clean_intra": "EXPORTED", "clean_inter": "EXPORTED", "over_limit": "NEEDS_REVIEW",
    "wrong_total": "NEEDS_REVIEW", "bad_gstin": "NEEDS_REVIEW", "other_buyer": "NEEDS_REVIEW",
    "credit_note": "NEEDS_REVIEW", "duplicate": "DUPLICATE", "not_invoice": "NOT_INVOICE",
}


# ---------- GSTIN

def check_digit(first14: str) -> str:
    """The GSTIN's 15th character (same algorithm as app/pipeline/validate.gstin_valid)."""
    total = 0
    for i, ch in enumerate(first14):
        product = CHARS.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return CHARS[(36 - total % 36) % 36]


def make_pan(rnd: random.Random) -> str:
    letters = string.ascii_uppercase
    return ("".join(rnd.choices(letters, k=3)) + "C" + rnd.choice(letters)
            + "".join(rnd.choices(string.digits, k=4)) + rnd.choice(letters))


def make_gstin(state: str, rnd: random.Random, pan: str | None = None) -> str:
    """A GSTIN with a valid check digit: state code + PAN + entity number + 'Z' + check digit."""
    first14 = f"{state}{pan or make_pan(rnd)}1Z"
    return first14 + check_digit(first14)


def company_gstin(rnd: random.Random) -> str:
    configured = [g.strip().upper() for g in settings.company_gstin.split(",") if g.strip()]
    return configured[0] if configured else make_gstin("19", rnd)


# ---------- amounts in words (Indian system)

_ONES = ("Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen Fourteen Fifteen "
         "Sixteen Seventeen Eighteen Nineteen").split()
_TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def _below_hundred(n: int) -> str:
    return _ONES[n] if n < 20 else _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])


def _below_thousand(n: int) -> str:
    h, rest = divmod(n, 100)
    return " ".join(p for p in ((_ONES[h] + " Hundred") if h else "", _below_hundred(rest) if rest else "") if p)


def words(amount: float) -> str:
    """6136.5 -> 'Rupees Six Thousand One Hundred Thirty Six and Fifty Paise Only'"""
    rupees, paise = divmod(round(abs(amount) * 100), 100)
    parts = []
    for name, size in (("Crore", 10 ** 7), ("Lakh", 10 ** 5), ("Thousand", 1000)):
        n, rupees = divmod(rupees, size)
        if n:
            parts.append(f"{_below_thousand(n)} {name}")
    if rupees:
        parts.append(_below_thousand(rupees))
    text = "Rupees " + (" ".join(parts) or "Zero")
    return text + (f" and {_below_hundred(paise)} Paise" if paise else "") + " Only"


def inr(x: float) -> str:
    """Indian digit grouping: 1065908.16 -> 10,65,908.16"""
    sign, x = ("-" if x < 0 else ""), abs(x)
    whole, frac = f"{x:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups) + "," + tail
    return f"{sign}{whole}.{frac}"


# ---------- building one document

def build(scenario: str, rnd: random.Random, tag: str = "", base: dict | None = None) -> dict:
    """The invoice data for a scenario. `base`: the invoice a 'duplicate' repeats."""
    if scenario == "duplicate":
        assert base is not None, "a duplicate needs the invoice it repeats"
        return {**base, "scenario": "duplicate", "expected": SCENARIOS["duplicate"],
                "footer": "Copy sent again for your records"}
    buyer = company_gstin(rnd)
    buyer_state = buyer[:2]
    if scenario == "clean_inter":
        state = rnd.choice([s for s in STATES if s != buyer_state])
    else:
        state = buyer_state
    supplier = rnd.choice(SUPPLIERS)
    gstin = make_gstin(state, rnd)
    if scenario == "bad_gstin":
        wrong = CHARS[(CHARS.index(gstin[14]) + 7) % 36]
        gstin = gstin[:14] + wrong
    if scenario == "other_buyer":
        buyer = make_gstin(rnd.choice([s for s in STATES if s != buyer_state]), rnd)
    limit = settings.auto_approve_limit
    target = rnd.uniform(limit * 1.3, limit * 4) if scenario == "over_limit" else rnd.uniform(limit * 0.1, limit * 0.75)
    lines, taxable = [], 0.0
    for desc, hsn, price, rate in rnd.sample(ITEMS, rnd.randint(1, 4)):
        lines.append({"description": desc, "hsn": hsn, "rate": rate, "price": float(price)})
    # quantities so the taxable total lands near the target
    share = target / 1.18 / len(lines)
    for line in lines:
        line["qty"] = max(1, round(share / line["price"]))
        line["taxable"] = round(line["qty"] * line["price"], 2)
        taxable += line["taxable"]
    tax = round(sum(line["taxable"] * line["rate"] / 100 for line in lines), 2)
    intra = state == buyer[:2]
    cgst = sgst = round(tax / 2, 2) if intra else 0.0
    igst = 0.0 if intra else tax
    raw_total = taxable + cgst + sgst + igst
    total = round(raw_total)
    round_off = round(total - raw_total, 2)
    printed_total = total + (100 if scenario == "wrong_total" else 0)
    number = f"{'CN' if scenario == 'credit_note' else 'INV'}/{date.today():%y%m}/{tag}{rnd.randint(1000, 9999)}"
    return {
        "scenario": scenario, "expected": SCENARIOS[scenario],
        "kind": {"credit_note": "CREDIT NOTE", "not_invoice": "TERMS AND CONDITIONS OF SUPPLY"}.get(scenario, "TAX INVOICE"),
        "supplier": supplier + (" Pvt Ltd" if rnd.random() < 0.5 else ""), "supplier_gstin": gstin,
        "supplier_state": STATES.get(state, state), "buyer_gstin": buyer, "buyer_name": "Tera Software Ltd",
        "place_of_supply": f"{buyer[:2]} - {STATES.get(buyer[:2], '')}", "number": number,
        "date": (date.today() - timedelta(days=rnd.randint(0, 20))).strftime("%d/%m/%Y"),
        "reference": f"INV/{date.today():%y%m}/{rnd.randint(1000, 9999)}" if scenario == "credit_note" else None,
        "lines": lines, "taxable": round(taxable, 2), "cgst": cgst, "sgst": sgst, "igst": igst,
        "round_off": round_off, "total": printed_total, "words": words(printed_total if scenario != "wrong_total" else total),
        "footer": "Thank you for your business",
    }


def render(inv: dict) -> bytes:
    """A one-page PDF with a real text layer (like invoices made by billing software)."""
    e = html.escape
    if inv["scenario"] == "not_invoice":
        return _pdf(f"""<h2>{e(inv['supplier'])}</h2><p>GSTIN: {e(inv['supplier_gstin'])}</p>
<h3>{e(inv['kind'])}</h3>
<p>Dear {e(inv['buyer_name'])},</p>
<p>1. Payment is due within 30 days of the invoice date.</p>
<p>2. Goods once sold will not be taken back. Interest at 18% a year applies to late payments.</p>
<p>3. All disputes are subject to local jurisdiction.</p>
<p>This letter is for your records; it is not an invoice.</p>""")
    rows = "".join(
        f"<tr><td>{i}</td><td>{e(line['description'])}</td><td>{line['hsn']}</td><td align='right'>{line['qty']}</td>"
        f"<td align='right'>{inr(line['price'])}</td><td align='right'>{line['rate']}%</td>"
        f"<td align='right'>{inr(line['taxable'])}</td></tr>"
        for i, line in enumerate(inv["lines"], start=1))
    tax_rows = (f"<tr><td>CGST</td><td align='right'>{inr(inv['cgst'])}</td></tr>"
                f"<tr><td>SGST</td><td align='right'>{inr(inv['sgst'])}</td></tr>" if inv["cgst"] else
                f"<tr><td>IGST</td><td align='right'>{inr(inv['igst'])}</td></tr>")
    ref = f"<p>Against invoice: {e(inv['reference'])}</p>" if inv.get("reference") else ""
    content = f"""
<h2>{e(inv['supplier'])}</h2>
<p>{e(inv['supplier_state'])} &nbsp; GSTIN: {e(inv['supplier_gstin'])}</p>
<h3>{e(inv['kind'])}</h3>
<p>No.: {e(inv['number'])} &nbsp; Date: {e(inv['date'])} &nbsp; Place of supply: {e(inv['place_of_supply'])}</p>
{ref}
<p><b>Bill to:</b> {e(inv['buyer_name'])} &nbsp; GSTIN: {e(inv['buyer_gstin'])}</p>
<table border="1" cellpadding="4" cellspacing="0" width="100%">
<tr><th>#</th><th>Description</th><th>HSN/SAC</th><th>Qty</th><th>Rate</th><th>GST</th><th>Taxable value</th></tr>
{rows}
</table>
<table cellpadding="3" width="50%">
<tr><td>Taxable value</td><td align='right'>{inr(inv['taxable'])}</td></tr>
{tax_rows}
<tr><td>Round off</td><td align='right'>{inr(inv['round_off'])}</td></tr>
<tr><td><b>Grand total (INR)</b></td><td align='right'><b>{inr(inv['total'])}</b></td></tr>
</table>
<p>Amount in words: {e(inv['words'])}</p>
<p>{e(inv['footer'])}</p>"""
    return _pdf(content)


def _pdf(content: str) -> bytes:
    story = pymupdf.Story(html=content)
    writer = pymupdf.DocumentWriter(buf := io.BytesIO())
    more = True
    while more:
        dev = writer.begin_page(pymupdf.paper_rect("a4"))
        more, _ = story.place(pymupdf.Rect(40, 40, 555, 800))
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()


def make_set(rnd: random.Random, scenarios: list[str], tag: str = "") -> list[dict]:
    """Documents for the scenarios (a 'duplicate' repeats the last clean_intra, made if needed)."""
    out, last_clean = [], None
    for sc in scenarios:
        if sc == "duplicate" and last_clean is None:
            last_clean = build("clean_intra", rnd, tag)
            out.append(last_clean)
        inv = build(sc, rnd, tag, base=last_clean)
        if sc == "clean_intra":
            last_clean = inv
        out.append(inv)
    for i, inv in enumerate(out, start=1):
        inv["file"] = f"{tag}{i:02d}-{inv['scenario']}-{inv['number'].replace('/', '-')}.pdf"
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=None, help="folder (default test_invoices/<time>)")
    p.add_argument("--repeat", type=int, default=1, help="how many of each scenario")
    p.add_argument("--only", default="", help="comma-separated scenarios")
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args()
    rnd = random.Random(args.seed)
    chosen = [s.strip() for s in args.only.split(",") if s.strip()] or list(SCENARIOS)
    unknown = set(chosen) - set(SCENARIOS)
    if unknown:
        p.error(f"unknown scenario(s): {', '.join(sorted(unknown))}; choose from {', '.join(SCENARIOS)}")
    out = Path(args.out or f"test_invoices/{datetime.now():%Y%m%d-%H%M%S}")
    out.mkdir(parents=True, exist_ok=True)
    docs = make_set(rnd, chosen * args.repeat)
    for inv in docs:
        (out / inv["file"]).write_bytes(render(inv))
    (out / "manifest.json").write_text(json.dumps(docs, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(docs)} invoice(s) in {out} (billed to {docs[0]['buyer_gstin'] if docs else '-'}):")
    for inv in docs:
        print(f"  {inv['file']:60} expected {inv['expected']}")


if __name__ == "__main__":
    main()
