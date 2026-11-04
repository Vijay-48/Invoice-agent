# ruff: noqa: F811  (pytest fixtures imported from other test modules)
"""Goods receipts on WhatsApp (app/grn_chat.py, D-113): the conversation against the test database, and
the webhook sending GRN messages to it."""
from datetime import date

import pytest

from app import grn_chat, procurement
from app.config import settings
from app.models import Branch, GoodsReceipt, Invoice, PurchaseOrder
from tests.conftest import SUPPLIER_GSTIN, make_extracted
from tests.test_flow import client, fake_extract, run_worker, upload  # noqa: F401
from tests.test_whatsapp_log import NUMBER, priya, send, sid, twilio  # noqa: F401

LINES = [dict(description="MS Pipe 25mm", hsn_sac="7306", quantity=10, unit_price=450, gst_rate=18),
         dict(description="GI Elbow 25mm", hsn_sac="7307", quantity=20, unit_price=35, gst_rate=18)]


@pytest.fixture
def po(session, users):
    branch = Branch(code="HYD", name="Hyderabad")
    session.add(branch)
    session.flush()
    users["ravi"].branch_id = branch.id
    order = procurement.create_po(session, po_number="PO-2026-0042", supplier_name="Sri Lakshmi Steels",
                                  supplier_gstin=SUPPLIER_GSTIN, po_date=date(2026, 9, 1), kind="goods",
                                  branch_id=branch.id, notes=None, lines=[dict(ln) for ln in LINES], actor="Priya")
    session.commit()
    return order


def say(session, user, text):
    return grn_chat.answer(session, user, text)


def test_only_the_grn_words_are_taken():
    assert grn_chat.is_grn_message("GRN PO-1") and grn_chat.is_grn_message("confirm 3")
    assert grn_chat.is_grn_message("Cancel")
    assert not grn_chat.is_grn_message("ok") and not grn_chat.is_grn_message("👍")
    assert not grn_chat.is_grn_message("") and not grn_chat.is_grn_message("status")


def test_open_pos_and_a_pos_lines(session, users, po):
    text = say(session, users["ravi"], "GRN")
    assert "PO-2026-0042: Sri Lakshmi Steels (01 Sep)" in text
    text = say(session, users["ravi"], "grn po 2026 0042")            # spaces and case don't matter
    assert "1. MS Pipe 25mm: 10 to come (ordered 10, accepted 0)" in text and "2 18 2 bent" in text
    assert "can't find PO X-9" in say(session, users["ravi"], "GRN X-9")


def test_a_draft_then_confirm(session, users, po):
    text = say(session, users["ravi"], "GRN PO-2026-0042\n1 10 2 bent\n2 20\nchallan DC-551")
    grn = session.query(GoodsReceipt).one()
    assert f"Draft GRN {grn.id} for PO PO-2026-0042 at Hyderabad, challan DC-551" in text
    assert "1. MS Pipe 25mm: 10 received, 8 accepted, 2 rejected (bent)" in text
    assert "Nothing counts until you confirm" in text
    assert (grn.status, grn.source, grn.created_by_id, grn.challan_number) == ("DRAFT", "whatsapp", users["ravi"].id,
                                                                              "DC-551")

    assert f"no draft GRN {grn.id}" in say(session, users["priya"], f"CONFIRM {grn.id}")   # not hers
    text = say(session, users["ravi"], "confirm")                     # the only draft: no number needed
    assert f"GRN {grn.id} confirmed. PO PO-2026-0042 is now partially received." in text
    session.expire_all()
    assert session.get(PurchaseOrder, po.id).status == "PARTIALLY_RECEIVED"
    assert "already confirmed" in say(session, users["ravi"], f"confirm {grn.id}")


def test_mistakes_are_explained_and_nothing_saved(session, users, po):
    assert "didn't understand \"ten pipes\"" in say(session, users["ravi"], "GRN PO-2026-0042\nten pipes")
    assert "say why 2 were rejected" in say(session, users["ravi"], "GRN PO-2026-0042\n1 10 2")
    assert "11 of 10 ordered" in say(session, users["ravi"], "GRN PO-2026-0042\n1 11")
    assert "has no line 7" in say(session, users["ravi"], "GRN PO-2026-0042\n7 1")
    assert session.query(GoodsReceipt).count() == 0
    assert "no draft GRN waiting" in say(session, users["ravi"], "CONFIRM")


def test_several_drafts_need_a_number_and_cancel(session, users, po):
    say(session, users["ravi"], "GRN PO-2026-0042; 1 4")
    say(session, users["ravi"], "GRN PO-2026-0042; 1 5")
    a, b = [g.id for g in session.query(GoodsReceipt).order_by(GoodsReceipt.id)]
    assert f"CONFIRM {a}, CONFIRM {b}" in say(session, users["ravi"], "confirm")
    assert f"Draft GRN {a} discarded" in say(session, users["ravi"], f"cancel {a}")
    assert "confirmed" in say(session, users["ravi"], "confirm")
    assert "isn't a GRN number" in say(session, users["ravi"], "confirm abc")


def test_confirming_lets_the_waiting_invoice_through(client, session, users, po, fake_extract, monkeypatch):
    monkeypatch.setattr(settings, "po_matching", True)
    fake_extract.append(make_extracted())
    inv_id = upload(client, users["priya"])["invoice_id"]
    run_worker()
    assert session.get(Invoice, inv_id).status == "NEEDS_REVIEW"
    say(session, users["ravi"], "GRN PO-2026-0042\n1 10\n2 20")
    text = say(session, users["ravi"], "CONFIRM")
    assert f"1 invoice(s) waiting for these goods were checked again; approved: #{inv_id}." in text
    session.expire_all()
    assert session.get(Invoice, inv_id).status in ("APPROVED", "EXPORTED")


def test_the_webhook_sends_grn_messages_here(twilio, priya, monkeypatch):
    calls = []
    monkeypatch.setattr(grn_chat, "on_message", lambda *a: calls.append(a))
    monkeypatch.setattr(settings, "whatsapp_grn", True)
    send(twilio, sid(1), media=False, body="GRN PO-1\n1 10")
    assert calls == [(priya.id, f"whatsapp:{NUMBER}", "GRN PO-1\n1 10")]
    monkeypatch.setattr(settings, "whatsapp_grn", False)
    send(twilio, sid(2), media=False, body="GRN PO-1\n1 10")
    assert len(calls) == 1                                            # off: not taken


def test_on_message_answers_on_whatsapp(session, users, po, monkeypatch):
    from app import followup
    sent = []
    monkeypatch.setattr(followup, "reply", lambda *a: sent.append(a))
    grn_chat.on_message(users["ravi"].id, f"whatsapp:{NUMBER}", "GRN")
    (user_id, number, text, label), = sent
    assert (user_id, number, label) == (users["ravi"].id, NUMBER, "grn") and "PO-2026-0042" in text
