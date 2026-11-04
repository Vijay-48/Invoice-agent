"""Goods receipts on WhatsApp (D-113): a branch person reports what arrived, gets a draft GRN back, and
confirms it. Only fixed words are understood (no AI reading the text), and nothing counts until the person
sends CONFIRM: "ok" or a thumbs-up never confirms.

    GRN                         open POs (of the sender's branch) still waiting for goods
    GRN PO-2026-0042            that PO's lines and how much is still to come
    GRN PO-2026-0042            a draft GRN: one line per PO line, "line received [rejected reason]"
    1 10
    2 20 2 bent
    challan DC-551              (optional) the supplier's delivery challan number
    CONFIRM 12 / CANCEL 12      confirm or discard draft GRN 12 (the number may be left out when the sender
                                has only one draft)

The sender is the user their WhatsApp number maps to (the webhook checked Twilio's signature). A draft can be
confirmed or discarded only by the person who sent it. Confirming checks again the invoices waiting for the
goods (procurement.after_po_change), exactly as confirming in the API does."""
import logging
import re
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import procurement
from app.db import SessionLocal
from app.models import Branch, GoodsReceipt, PoLine, PurchaseOrder, User, WhatsAppSession

log = logging.getLogger("grn_chat")
WORDS = {"grn", "confirm", "cancel"}
LIST_LIMIT = 5
LINE_RE = re.compile(r"^(?:line\s*)?(\d+)\s*[:=-]?\s*(\d+(?:\.\d+)?)(?:\s+(\d+(?:\.\d+)?)\s*(.*))?$", re.I)
CHALLAN_RE = re.compile(r"^(?:challan|dc)\s*[:#-]?\s*(\S.*)$", re.I)
HOW = ("Send what arrived, one PO line per line of the message:\n"
       "GRN {po}\n1 10\n2 18 2 bent\n"
       "(line number, quantity received, and if some were rejected: how many and why). "
       "Add \"challan <number>\" if there is a delivery challan.")


def is_grn_message(body: str) -> bool:
    words = body.strip().split(maxsplit=1)
    return bool(words) and words[0].lower() in WORDS


def _qty(x: float) -> str:
    return f"{x:g}"


def _open_pos(session: Session, user: User) -> str:
    q = select(PurchaseOrder).where(PurchaseOrder.status.in_(["OPEN", "PARTIALLY_RECEIVED"]))
    if user.branch_id:
        q = q.where(PurchaseOrder.branch_id == user.branch_id)
    pos = session.scalars(q.order_by(PurchaseOrder.po_date, PurchaseOrder.id).limit(LIST_LIMIT + 1)).all()
    if not pos:
        return "No purchase orders are waiting for goods" + (" at your branch." if user.branch_id else ".")
    lines = [f"{po.po_number}: {po.supplier_name} ({po.po_date:%d %b})" for po in pos[:LIST_LIMIT]]
    more = "\n…and more" if len(pos) > LIST_LIMIT else ""
    return ("Purchase orders waiting for goods:\n" + "\n".join(lines) + more
            + "\n\nSend \"GRN <PO number>\" to see what is still to come.")


def _po_lines_text(session: Session, po: PurchaseOrder) -> str:
    totals = procurement.line_totals(session, po.id)
    rows = []
    for ln in session.scalars(select(PoLine).where(PoLine.po_id == po.id).order_by(PoLine.line_no)):
        t = totals[ln.id]
        rows.append(f"{ln.line_no}. {ln.description}: {_qty(t.to_receive)} to come "
                    f"(ordered {_qty(t.ordered)}, accepted {_qty(t.accepted)})")
    return f"PO {po.po_number} ({po.supplier_name}):\n" + "\n".join(rows)


def _parse(items: list[str]) -> tuple[list[dict], str | None, list[str]]:
    lines, challan, bad = [], None, []
    for item in items:
        if m := CHALLAN_RE.match(item):
            challan = m.group(1).strip()[:50]
        elif m := LINE_RE.match(item):
            rejected = float(m.group(3)) if m.group(3) else 0.0
            lines.append({"line_no": int(m.group(1)), "received_qty": float(m.group(2)), "rejected_qty": rejected,
                          "reason": (m.group(4) or "").strip() or None})
        else:
            bad.append(item)
    return lines, challan, bad


def _draft_text(session: Session, grn: GoodsReceipt, po: PurchaseOrder) -> str:
    out = procurement.serialize_grn(session, grn)
    branch = session.get(Branch, grn.branch_id) if grn.branch_id else None
    rows = []
    for ln in out["lines"]:
        row = f"{ln['line_no']}. {ln['description']}: {_qty(ln['received'])} received"
        if ln["rejected"]:
            row += f", {_qty(ln['accepted'])} accepted, {_qty(ln['rejected'])} rejected ({ln['reason']})"
        rows.append(row)
    where = f" at {branch.name}" if branch else ""
    challan = f", challan {grn.challan_number}" if grn.challan_number else ""
    return (f"Draft GRN {grn.id} for PO {po.po_number}{where}{challan}:\n" + "\n".join(rows)
            + f"\n\nNothing counts until you confirm. Send CONFIRM {grn.id} if this is right, or CANCEL {grn.id}.")


def _new_grn(session: Session, user: User, po_text: str, items: list[str]) -> str:
    po = procurement.find_po(session, po_text)
    if po is None:
        return f"I can't find PO {po_text}. Send GRN to see the open ones."
    if not items:
        return _po_lines_text(session, po) + "\n\n" + HOW.format(po=po.po_number)
    lines, challan, bad = _parse(items)
    if bad or not lines:
        shown = "; ".join(f"\"{b[:40]}\"" for b in bad[:3])
        return (f"I didn't understand {shown or 'the quantities'}. " + HOW.format(po=po.po_number))
    try:
        grn = procurement.create_grn(session, po, branch_id=user.branch_id, received_on=date.today(),
                                     challan_number=challan, notes="Sent on WhatsApp", lines=lines,
                                     actor=user.name, actor_id=user.id, source="whatsapp")
    except procurement.ProcurementError as e:
        session.rollback()
        return f"Not saved: {e}."
    session.commit()
    log.info("Draft GRN %s for PO %s from %s on WhatsApp", grn.id, po.po_number, user.name)
    return _draft_text(session, grn, po)


def _own_draft(session: Session, user: User, number: str | None) -> GoodsReceipt | str:
    q = select(GoodsReceipt).where(GoodsReceipt.created_by_id == user.id, GoodsReceipt.status == "DRAFT")
    if number:
        if not number.isdigit():
            return f"\"{number[:20]}\" isn't a GRN number."
        grn = session.scalar(select(GoodsReceipt).where(GoodsReceipt.id == int(number)).with_for_update())
        if grn is None or grn.created_by_id != user.id:
            return f"You have no draft GRN {number}."
        return grn
    drafts = session.scalars(q.order_by(GoodsReceipt.id)).all()
    if len(drafts) == 1:
        return session.scalar(select(GoodsReceipt).where(GoodsReceipt.id == drafts[0].id).with_for_update())
    if not drafts:
        return "You have no draft GRN waiting."
    return "You have several drafts: say which, e.g. " + ", ".join(f"CONFIRM {g.id}" for g in drafts[:LIST_LIMIT])


def _confirm(session: Session, user: User, number: str | None) -> str:
    grn = _own_draft(session, user, number)
    if isinstance(grn, str):
        return grn
    if grn.status != "DRAFT":
        return f"GRN {grn.id} is already {grn.status.lower()}."
    try:
        po = procurement.confirm_grn(session, grn, user.name)
    except procurement.ProcurementError as e:
        session.rollback()
        return f"Not confirmed: {e}."
    rechecked = procurement.after_po_change(session, po, user.name)
    log.info("GRN %s confirmed by %s on WhatsApp; %d invoice(s) checked again", grn.id, user.name, len(rechecked))
    text = f"GRN {grn.id} confirmed. PO {po.po_number} is now {po.status.replace('_', ' ').lower()}."
    approved = [r["invoice_id"] for r in rechecked if r["status"] == "APPROVED"]
    if rechecked:
        text += (f" {len(rechecked)} invoice(s) waiting for these goods were checked again"
                 + (f"; approved: {', '.join(f'#{i}' for i in approved)}." if approved else "."))
    return text


def _cancel(session: Session, user: User, number: str | None) -> str:
    grn = _own_draft(session, user, number)
    if isinstance(grn, str):
        return grn
    try:
        procurement.cancel_grn(session, grn, user.name, "on WhatsApp")
    except procurement.ProcurementError as e:
        return f"Not cancelled: {e}."
    session.commit()
    return f"Draft GRN {grn.id} discarded."


def answer(session: Session, user: User, body: str) -> str:
    """The reply to a GRN / CONFIRM / CANCEL message."""
    items = [x.strip() for x in re.split(r"[\n;]+", body.strip()) if x.strip()]
    first = items[0].split(maxsplit=1)
    word, rest = first[0].lower(), (first[1].strip() if len(first) > 1 else "")
    if word == "confirm":
        return _confirm(session, user, rest or None)
    if word == "cancel":
        return _cancel(session, user, rest or None)
    if not rest:
        return _open_pos(session, user)
    return _new_grn(session, user, rest, items[1:])


def on_message(user_id: int, sender: str, body: str) -> None:
    """Background task of the WhatsApp webhook: answer, inside the 24-hour window the sender just opened."""
    from app import followup, whatsapp  # here: both import modules that import this one's callers
    number = whatsapp.normalize_number(sender)
    if number is None:
        return
    try:
        with SessionLocal() as session:
            session.execute(pg_insert(WhatsAppSession).values(number=number, last_inbound_at=followup._now())
                            .on_conflict_do_update(index_elements=["number"], set_={"last_inbound_at": followup._now()}))
            session.commit()
            user = session.get(User, user_id)
            text = answer(session, user, body)
            session.commit()
    except Exception as e:      # the class only: the text may hold anything
        log.error("Could not handle a WhatsApp GRN message: %s", type(e).__name__)
        text = "Sorry, something went wrong; nothing was saved. Please try again."
    followup.reply(user_id, number, text, "grn")
