"""Suppliers: their history from approved invoices, and the supplier master (table suppliers).

A supplier (by GSTIN) is "known" once it has approved invoices. Later invoices are compared with
that history: a new supplier, a different name on a known GSTIN (a misread GSTIN, or someone
borrowing it) and an amount far above the supplier's usual are all shown to the reviewer.

Bank details (D-104): the supplier master keeps the account payments go to. The first approved invoice
that shows one sets it ("trust on first use"). An invoice showing a different account fails the
`bank_details` check and can't be approved until a person records the new account with a note on how
they confirmed it with the supplier: a changed account on an invoice is the most common invoice fraud."""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Invoice, Status, Supplier, SupplierEvent, audit
from app.pipeline.checks import FAIL, PASS, WARN, Check
from app.pipeline.journal import JOURNAL
from app.pipeline.journal import doc_type as journal_doc_type
from app.pipeline.validate import gstin_valid
from app.schemas import ExtractedInvoice

IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")      # 4 letters (bank), 0, 6 characters (branch)
ACCOUNT_RE = re.compile(r"^[A-Z0-9]{6,20}$")

RECORDED = [Status.APPROVED, Status.EXPORTED]


@dataclass
class SupplierProfile:
    gstin: str
    invoices: int
    names: list[str] = field(default_factory=list)   # most used first
    max_total: float = 0.0
    avg_total: float = 0.0
    last_invoice_date: str | None = None


def _name_key(name: str | None) -> str:
    """Names compared without case, punctuation and company suffixes ('Pvt. Ltd.' = 'Private Limited')."""
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(r"\b(private|pvt|limited|ltd|llp|inc|the|and)\b", " ", s)
    return " ".join(s.split())


def profile(session: Session, gstin: str | None, exclude_id: int | None = None) -> SupplierProfile | None:
    """What the approved invoices say about a supplier; None when there are none."""
    if not gstin or len(gstin) > 15:   # stored as none when too long (evaluate.store)
        return None
    # Aggregated in the database, one row per name: a supplier with thousands of invoices costs the
    # same as one with three (loading the rows took ~5 s at 20,000 invoices, D-95)
    rows = session.execute(select(
        Invoice.supplier_name, func.count(), func.max(Invoice.grand_total), func.sum(Invoice.grand_total),
        func.count(Invoice.grand_total), func.max(Invoice.invoice_date))
        .where(Invoice.supplier_gstin == gstin, Invoice.status.in_(RECORDED),
               Invoice.id != (exclude_id or 0), journal_doc_type() != "journal_entry")
        .group_by(Invoice.supplier_name).order_by(func.count().desc())).all()
    if not rows:
        return None
    totals_counted = sum(r[4] for r in rows)
    dates = [r[5] for r in rows if r[5]]
    return SupplierProfile(
        gstin=gstin, invoices=sum(r[1] for r in rows), names=[r[0] or "" for r in rows],
        max_total=max((float(r[2]) for r in rows if r[2] is not None), default=0.0),
        avg_total=sum(float(r[3] or 0) for r in rows) / totals_counted if totals_counted else 0.0,
        last_invoice_date=max(dates, default=None))


def check_supplier(session: Session, inv: Invoice, data: ExtractedInvoice) -> list[Check]:
    """Compare a new invoice with its supplier's history. Credit notes and the like are compared too:
    the name and the usual items still apply, but the amount rule is for invoices only."""
    known = profile(session, data.supplier_gstin, exclude_id=inv.id)
    if known is None:
        if not data.supplier_gstin:
            return []
        # Information only: warning here would send every first invoice to review (D-02)
        return [Check("supplier_history", PASS,
                      f"New supplier {data.supplier_name} ({data.supplier_gstin}): no approved invoice from it yet")]
    checks: list[Check] = []
    if _name_key(data.supplier_name) not in {_name_key(n) for n in known.names}:
        checks.append(Check("supplier_name", WARN,
                            f"GSTIN {data.supplier_gstin} was approved before as '{known.names[0]}', "
                            f"this invoice says '{data.supplier_name}'"))
    if (known.invoices >= settings.supplier_min_history and data.is_invoice
            and data.document_type != "credit_note" and known.max_total > 0
            and data.grand_total > known.max_total * settings.supplier_amount_factor):
        checks.append(Check("supplier_amount", WARN,
                            f"Total {data.grand_total:,.2f} is far above this supplier's usual "
                            f"(highest so far {known.max_total:,.2f}, average {known.avg_total:,.2f}, "
                            f"{known.invoices} invoices)"))
    if not checks:
        checks.append(Check("supplier_history", PASS, f"Known supplier ({known.invoices} approved invoice(s))"))
    return checks


def list_suppliers(session: Session) -> list[dict]:
    """One row per supplier GSTIN with approved invoices, biggest spend first."""
    rows = session.execute(select(
        Invoice.supplier_gstin, func.count(), func.max(Invoice.grand_total), func.avg(Invoice.grand_total),
        func.sum(Invoice.grand_total), func.max(Invoice.invoice_date), func.max(Invoice.supplier_name))
        .where(Invoice.supplier_gstin.is_not(None), Invoice.status.in_(RECORDED), journal_doc_type() != "journal_entry")
        .group_by(Invoice.supplier_gstin).order_by(func.sum(Invoice.grand_total).desc())).all()
    return [{"gstin": g, "name": n, "invoices": c, "highest_total": float(mx or 0), "average_total": float(av or 0),
             "total_spend": float(sm or 0), "last_invoice_date": ld} for g, c, mx, av, sm, ld, n in rows]


# ---------- the supplier master and its bank details

def get(session: Session, gstin: str | None) -> Supplier | None:
    return session.scalar(select(Supplier).where(Supplier.gstin == gstin)) if gstin else None


def masked(account: str | None) -> str:
    """Only the last four characters: messages, check results and logs never hold a whole account number."""
    return f"XXXX{account[-4:]}" if account else "none"


def bank_text(account: str | None, ifsc: str | None) -> str:
    return f"A/c {masked(account)}, IFSC {ifsc or 'not shown'}"


def _same_bank(sup: Supplier, account: str, ifsc: str | None) -> bool:
    """The same account; the IFSC must match too when both are known."""
    return sup.bank_account == account and (not ifsc or not sup.bank_ifsc or sup.bank_ifsc == ifsc)


def _changed_message(sup: Supplier, account: str, ifsc: str | None) -> str:
    return (f"Bank details differ from the supplier's recorded account (recorded {bank_text(sup.bank_account, sup.bank_ifsc)}; "
            f"this invoice {bank_text(account, ifsc)}). Confirm with the supplier on a phone number you already "
            "have, never one printed on this invoice, then record the new details for the supplier; or correct "
            "them here if they were misread")


def check_bank(session: Session, data: ExtractedInvoice) -> list[Check]:
    """The invoice's bank details against the supplier master."""
    account, ifsc = data.supplier_bank_account, data.supplier_ifsc
    checks: list[Check] = []
    if ifsc and not IFSC_RE.match(ifsc):
        checks.append(Check("bank_ifsc", WARN, f"IFSC '{ifsc}' is not in the bank format (4 letters, 0, 6 "
                                               "characters): check it was read correctly"))
    if not account or not data.supplier_gstin:
        return checks
    known = get(session, data.supplier_gstin)
    if known is None or not known.bank_account:
        checks.append(Check("bank_details", PASS, f"First bank details for this supplier ({bank_text(account, ifsc)}): "
                                                  "recorded as its account when this invoice is approved"))
    elif _same_bank(known, account, ifsc):
        checks.append(Check("bank_details", PASS, f"Bank details match the supplier's recorded account ({masked(account)})"))
    else:
        checks.append(Check("bank_details", FAIL, _changed_message(known, account, ifsc)))
    return checks


def bank_hold(session: Session, inv: Invoice) -> str | None:
    """Why this invoice can't be approved yet (its bank details differ from the recorded ones), or None.
    Asked again at approval: the details may have been confirmed since the checks ran."""
    data = inv.extracted or {}
    account = data.get("supplier_bank_account")
    known = get(session, data.get("supplier_gstin")) if account else None
    if known is None or not known.bank_account or _same_bank(known, account, data.get("supplier_ifsc")):
        return None
    return _changed_message(known, account, data.get("supplier_ifsc"))


def _event(session: Session, sup: Supplier, kind: str, message: str, *, actor: str, invoice_id: int | None = None,
           data: dict | None = None) -> None:
    session.add(SupplierEvent(supplier_id=sup.id, kind=kind, message=message, actor=actor, invoice_id=invoice_id,
                              data=data))


def record_approved(session: Session, inv: Invoice, data: ExtractedInvoice, actor: str) -> Supplier | None:
    """An invoice was approved: make sure its supplier is in the master, and record its bank details if the
    supplier has none yet. Never changes recorded bank details (that is confirm_bank)."""
    gstin = data.supplier_gstin
    if data.document_type == JOURNAL or not gstin_valid(gstin):
        return None
    created = session.scalar(pg_insert(Supplier).values(gstin=gstin, name=data.supplier_name[:255] or gstin,
                                                        pan=gstin[2:12])
                             .on_conflict_do_nothing(index_elements=["gstin"]).returning(Supplier.id)) is not None
    sup = session.scalar(select(Supplier).where(Supplier.gstin == gstin).with_for_update())
    if created:
        _event(session, sup, "created", f"Added from invoice #{inv.id}", actor=actor, invoice_id=inv.id)
    account, ifsc = data.supplier_bank_account, data.supplier_ifsc
    if account and ACCOUNT_RE.match(account) and not sup.bank_account:
        sup.bank_account, sup.bank_ifsc = account, ifsc if ifsc and IFSC_RE.match(ifsc) else None
        sup.bank_set_by, sup.bank_set_at, sup.bank_source_invoice_id = actor, datetime.now(timezone.utc), inv.id
        text = f"Bank details recorded from invoice #{inv.id}: {bank_text(account, sup.bank_ifsc)}"
        _event(session, sup, "bank_recorded", text, actor=actor, invoice_id=inv.id,
               data={"account": account, "ifsc": sup.bank_ifsc})
        audit(session, inv.id, "supplier", text, actor=actor)
    return sup


def confirm_bank(session: Session, gstin: str, account: str, ifsc: str, note: str, actor: str,
                 invoice_id: int | None = None) -> Supplier:
    """A person confirmed the supplier's (new) bank details with the supplier: they become the recorded ones."""
    account = re.sub(r"[\s.-]", "", account or "").upper()
    ifsc = re.sub(r"[\s-]", "", ifsc or "").upper()
    if not ACCOUNT_RE.match(account):
        raise ValueError("The account number should be 6 to 20 letters or digits")
    if not IFSC_RE.match(ifsc):
        raise ValueError("The IFSC should be 11 characters: 4 letters, 0, then 6 letters or digits")
    if len(note.strip()) < 10:
        raise ValueError("Say how the change was confirmed (who you called, on which known number)")
    sup = session.scalar(select(Supplier).where(Supplier.gstin == gstin).with_for_update())
    if sup is None:
        raise LookupError(f"No supplier with GSTIN {gstin}")
    old = {"account": sup.bank_account, "ifsc": sup.bank_ifsc}
    sup.bank_account, sup.bank_ifsc = account, ifsc
    sup.bank_set_by, sup.bank_set_at, sup.bank_source_invoice_id = actor, datetime.now(timezone.utc), invoice_id
    text = (f"Bank details confirmed by {actor}: {bank_text(account, ifsc)} (before: "
            f"{bank_text(old['account'], old['ifsc'])}). {note.strip()}")
    _event(session, sup, "bank_confirmed", text, actor=actor, invoice_id=invoice_id,
           data={"before": old, "after": {"account": account, "ifsc": ifsc}, "note": note.strip()})
    if invoice_id:
        audit(session, invoice_id, "supplier", text, actor=actor)
    return sup


def serialize_master(sup: Supplier | None) -> dict:
    if sup is None:
        return {"in_master": False, "pan": None, "bank_account": None, "bank_ifsc": None, "bank_set_by": None,
                "bank_set_at": None}
    return {"in_master": True, "pan": sup.pan, "bank_account": sup.bank_account, "bank_ifsc": sup.bank_ifsc,
            "bank_set_by": sup.bank_set_by, "bank_set_at": sup.bank_set_at}


def serialize_event(e: SupplierEvent) -> dict:
    return {"id": e.id, "kind": e.kind, "invoice_id": e.invoice_id, "actor": e.actor, "message": e.message,
            "created_at": e.created_at}
