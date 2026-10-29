# ruff: noqa: F811  (pytest fixtures imported from test_flow)
"""Follow-up WhatsApp messages and calls (app/followup.py, app/outbound.py). Twilio is faked: nothing is sent."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import followup, outbound
from app.config import settings
from app.models import FollowUp, FollowUpEvent, Invoice, Status, VoiceCall, WhatsAppSession
from tests.test_flow import client, h   # noqa: F401 (fixtures)

NOW = datetime(2026, 10, 28, 12, 0).astimezone()      # inside the sending hours (09:00-19:00)
NUMBER = "+919876543210"
OTHER_NUMBER = "+919811122233"
MIN = timedelta(minutes=1)


@pytest.fixture
def clock(monkeypatch):
    c = SimpleNamespace(now=NOW)
    monkeypatch.setattr(followup, "_now", lambda: c.now)
    return c


@pytest.fixture
def sent(monkeypatch):
    """Records what would go to Twilio; set `.fail` to make sends fail."""
    log = SimpleNamespace(messages=[], calls=[], emails=[], fail=None)

    def send_whatsapp(number, body, *, template=False):
        if log.fail:
            return outbound.Result("failed", detail=log.fail)
        log.messages.append({"number": number, "body": body, "template": template})
        return outbound.Result("sent", sid=f"SM{len(log.messages):032d}")

    def place_call(number, url, status_callback, twiml=None):
        if log.fail:
            return outbound.Result("failed", detail=log.fail)
        log.calls.append({"number": number, "url": url, "status_callback": status_callback, "twiml": twiml})
        return outbound.Result("sent", sid=f"CA{len(log.calls):032d}")

    def send_email(sender, to, subject, html):
        if log.fail:
            return outbound.Result("failed", detail=log.fail)
        log.emails.append({"sender": sender and sender.email, "to": to, "subject": subject, "html": html})
        return outbound.Result("sent")

    monkeypatch.setattr(outbound, "send_whatsapp", send_whatsapp)
    monkeypatch.setattr(outbound, "place_call", place_call)
    monkeypatch.setattr(outbound, "send_email", send_email)
    return log


@pytest.fixture
def on(monkeypatch, users, clock):
    """Follow-ups switched on and live (Twilio faked), with a short ladder: WhatsApp at 60 and 120 minutes,
    then a call at 180. Priya can be reached at NUMBER, Ravi at OTHER_NUMBER."""
    for name, value in dict(
            followup_enabled=True, followup_dry_run=False, followup_hours="09:00-19:00",
            followup_review_ladder="whatsapp@60,whatsapp@120,voice@180", followup_export_ladder="whatsapp@30",
            followup_result_notices=True, followup_ack_hours=24, followup_snooze_hours=2,
            followup_max_per_day=6, followup_max_calls_per_day=2, followup_escalation_user_id=0,
            twilio_whatsapp_template_sid="", public_base_url="https://example.test",
            followup_email_to="", followup_email_sender_user_id=0, report_sender_user_id=0, report_app_url="",
            whatsapp_senders=f"{NUMBER}:{users['priya'].id},{OTHER_NUMBER}:{users['ravi'].id}").items():
        monkeypatch.setattr(settings, name, value)
    return users


def open_window(session, number=NUMBER, ago=timedelta(hours=1)):
    """The person wrote to us recently: free-form WhatsApp messages are allowed."""
    session.merge(WhatsAppSession(number=number, last_inbound_at=NOW - ago))
    session.commit()


def make_invoice(session, user, status=Status.NEEDS_REVIEW, *, source="gmail", since=None, name="Sri Lakshmi Steels",
                 total=6136, reason="Amount above the auto-approve limit", approved_at=None, created_at=None):
    n = session.query(Invoice).count() + 1
    inv = Invoice(user_id=user.id, source=source, original_filename=f"inv{n}.pdf", file_path="x", file_hash=f"{n:064d}",
                  status=status, status_reason=reason, supplier_name=name, grand_total=total,
                  updated_at=since or NOW, created_at=created_at or NOW, approved_at=approved_at)
    session.add(inv)
    session.commit()
    return inv


def rows(session, model):
    session.expire_all()
    return session.query(model).order_by(model.id).all()


# ---------- settings

def test_ladder_steps_are_sorted_and_bad_ones_skipped(monkeypatch):
    monkeypatch.setattr(settings, "followup_review_ladder", "voice@2880, whatsapp@240,sms@5,whatsapp@x,whatsapp@1440,")
    assert followup.ladder("review") == (("whatsapp", 240), ("whatsapp", 1440), ("voice", 2880))
    assert followup.ladder("result") == (("whatsapp", 0),)


def test_sending_hours(monkeypatch):
    monkeypatch.setattr(settings, "followup_hours", "09:00-19:00")
    at = lambda hh, mm=0: datetime(2026, 10, 28, hh, mm).astimezone()   # noqa: E731
    assert followup.in_hours(at(9)) and followup.in_hours(at(18, 59)) and not followup.in_hours(at(19)) \
        and not followup.in_hours(at(8, 59))
    assert followup.next_open(at(21)) == at(9) + timedelta(days=1)
    assert followup.next_open(at(7)) == at(9) and followup.next_open(at(12)) == at(12)
    monkeypatch.setattr(settings, "followup_hours", "garbage")
    assert followup.in_hours(at(12))          # falls back to 09:00-19:00


# ---------- the review ladder

def test_review_ladder_whatsapp_whatsapp_then_call(on, sent, clock, session):
    inv = make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    open_window(session)
    followup.run_once()
    assert len(sent.messages) == 1
    msg = sent.messages[0]
    assert msg["number"] == NUMBER and not msg["template"]
    assert f"#{inv.id} Sri Lakshmi Steels ₹6,136.00" in msg["body"] and "waiting for review" in msg["body"]
    assert "Reply 1" in msg["body"] and "Amount above the auto-approve limit" in msg["body"]
    fu, = rows(session, FollowUp)
    assert (fu.kind, fu.step, fu.state) == ("review", 1, "active") and fu.awaiting_reply_since is not None
    assert fu.next_due_at == inv.updated_at + 120 * MIN
    assert [e.status for e in rows(session, FollowUpEvent)] == ["sent"]

    clock.now = NOW + 20 * MIN                  # not due yet
    followup.run_once()
    assert len(sent.messages) == 1

    clock.now = NOW + 40 * MIN                  # step 2 (WhatsApp) due at 120 minutes after it began waiting
    followup.run_once()
    assert len(sent.messages) == 2 and rows(session, FollowUp)[0].step == 2

    clock.now = NOW + 100 * MIN                 # step 3: the call
    followup.run_once()
    assert len(sent.messages) == 2 and len(sent.calls) == 1
    call, = rows(session, VoiceCall)
    assert sent.calls[0]["number"] == NUMBER and sent.calls[0]["url"] == f"https://example.test/api/v1/webhook/voice?call={call.id}"
    # Twilio gets what to do with the call up front, so it never has to fetch it (unsigned on a trial account)
    assert "<ConversationRelay" in sent.calls[0]["twiml"]
    assert f"wss://example.test/api/v1/webhook/voice/stream?call={call.id}&amp;token=" in sent.calls[0]["twiml"]
    assert call.status == "ringing" and call.follow_up_ids == [fu.id]
    assert rows(session, FollowUp)[0].step == 2         # moves on when Twilio reports the call's end
    followup.finish_call(session, call.id, "completed")
    fu = rows(session, FollowUp)[0]
    assert (fu.step, fu.state, fu.done_reason) == (3, "done", "finished")
    assert rows(session, VoiceCall)[0].outcome == "none"


def test_a_person_gets_one_message_for_all_their_invoices(on, sent, session):
    ids = [make_invoice(session, on["priya"], since=NOW - 90 * MIN, name=f"Supplier {i}").id for i in range(7)]
    other = make_invoice(session, on["ravi"], since=NOW - 90 * MIN, name="Ravi's supplier")
    open_window(session)
    open_window(session, OTHER_NUMBER)
    followup.run_once()
    assert len(sent.messages) == 2
    mine = next(m for m in sent.messages if m["number"] == NUMBER)["body"]
    assert "7 invoices are waiting for review" in mine and "…and 2 more" in mine
    assert sum(f"#{i} " in mine for i in ids) == 5 and f"#{other.id}" not in mine
    assert len(rows(session, FollowUp)) == 8


def test_nothing_before_the_first_step_and_no_row_for_quick_invoices(on, sent, session):
    make_invoice(session, on["priya"], since=NOW - 30 * MIN)
    open_window(session)
    followup.run_once()
    assert not sent.messages and not rows(session, FollowUp)


def test_the_ladder_stops_when_the_invoice_is_dealt_with(on, sent, clock, session):
    inv = make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    open_window(session)
    followup.run_once()
    inv.status = Status.APPROVED
    session.commit()
    clock.now = NOW + 40 * MIN
    followup.run_once()
    assert len(sent.messages) == 1
    fu = rows(session, FollowUp)[0]
    assert (fu.state, fu.done_reason, fu.awaiting_reply_since) == ("done", "resolved", None)


def test_people_without_a_number_are_not_followed_up(on, sent, monkeypatch, session):
    monkeypatch.setattr(settings, "whatsapp_senders", "")
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    followup.run_once()
    assert not sent.messages and not rows(session, FollowUp)


# ---------- WhatsApp's 24-hour window, hours, limits, failures

def test_window_closed_without_template_skips_the_step_and_moves_on(on, sent, session):
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)      # Priya has not written to us
    followup.run_once()
    assert not sent.messages
    event, = rows(session, FollowUpEvent)
    assert event.status == "skipped" and "24-hour" in event.detail
    assert rows(session, FollowUp)[0].step == 1


def test_window_closed_uses_the_template_when_there_is_one(on, sent, monkeypatch, session):
    monkeypatch.setattr(settings, "twilio_whatsapp_template_sid", "HX" + "a" * 32)
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    followup.run_once()
    assert len(sent.messages) == 1 and sent.messages[0]["template"] is True


def test_the_window_is_open_for_24_hours_after_they_wrote(on, sent, session):
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    open_window(session, ago=timedelta(hours=23, minutes=55))     # inside the 10-minute safety margin: closed
    followup.run_once()
    assert not sent.messages


def test_reminders_wait_for_the_sending_hours_but_file_receipts_do_not(on, sent, clock, session):
    clock.now = datetime(2026, 10, 28, 21, 0).astimezone()
    make_invoice(session, on["priya"], since=clock.now - 90 * MIN)
    make_invoice(session, on["priya"], Status.APPROVED, source="whatsapp", created_at=clock.now - 5 * MIN)
    session.merge(WhatsAppSession(number=NUMBER, last_inbound_at=clock.now - 5 * MIN))
    session.commit()
    followup.run_once()
    assert len(sent.messages) == 1 and "About the files you sent" in sent.messages[0]["body"]
    assert "waiting for review" not in sent.messages[0]["body"]
    clock.now = datetime(2026, 10, 29, 9, 30).astimezone()
    session.merge(WhatsAppSession(number=NUMBER, last_inbound_at=clock.now - 5 * MIN))
    session.commit()
    followup.run_once()
    assert len(sent.messages) == 2 and "waiting for review" in sent.messages[1]["body"]


def test_daily_limit_per_person(on, sent, clock, monkeypatch, session):
    monkeypatch.setattr(settings, "followup_max_per_day", 1)
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    open_window(session)
    followup.run_once()
    assert len(sent.messages) == 1
    later = make_invoice(session, on["priya"], since=NOW - 80 * MIN)
    clock.now = NOW + 10 * MIN
    followup.run_once()
    assert len(sent.messages) == 1
    fu = next(f for f in rows(session, FollowUp) if f.invoice_id == later.id)
    assert fu.step == 0 and fu.next_due_at == datetime(2026, 10, 29, 9, 0).astimezone()
    assert "daily limit" in rows(session, FollowUpEvent)[-1].detail


def test_a_failed_send_is_retried_then_given_up(on, sent, clock, session):
    sent.fail = "Twilio error 63016"
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    open_window(session)
    for minutes in (0, 31, 62):
        clock.now = NOW + minutes * MIN
        followup.run_once()
    fu = rows(session, FollowUp)[0]
    assert (fu.state, fu.done_reason, fu.step) == ("done", "unreachable", 0)
    assert [e.status for e in rows(session, FollowUpEvent)] == ["failed"] * 3
    assert "63016" in rows(session, FollowUpEvent)[0].detail


def test_dry_run_records_every_step_and_sends_nothing(on, monkeypatch, session):
    monkeypatch.setattr(settings, "followup_dry_run", True)

    def refuse():
        raise AssertionError("a dry run must not create a Twilio client")
    monkeypatch.setattr(outbound, "client", refuse)
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    open_window(session)
    followup.run_once()
    event, = rows(session, FollowUpEvent)
    assert (event.channel, event.direction, event.status) == ("whatsapp", "out", "dry_run")
    assert rows(session, FollowUp)[0].step == 1


def test_nothing_happens_when_follow_ups_are_off(on, sent, monkeypatch, session):
    monkeypatch.setattr(settings, "followup_enabled", False)
    followup.on_incoming(on["priya"].id, f"whatsapp:{NUMBER}", "1", False)
    followup.acknowledge_file(on["priya"].id, f"whatsapp:{NUMBER}", True)
    assert not sent.messages and session.query(WhatsAppSession).count() == 0


# ---------- other kinds

def test_approved_but_not_in_excel(on, sent, session):
    inv = make_invoice(session, on["priya"], Status.APPROVED, approved_at=NOW - 40 * MIN, reason=None)
    open_window(session)
    followup.run_once()
    assert len(sent.messages) == 1 and f"#{inv.id}" in sent.messages[0]["body"]
    assert "not yet in the Excel file" in sent.messages[0]["body"] and "Reply 1" not in sent.messages[0]["body"]
    fu, = rows(session, FollowUp)
    assert (fu.kind, fu.state, fu.done_reason) == ("export", "done", "finished")


def test_approved_a_moment_ago_is_left_alone(on, sent, session):
    make_invoice(session, on["priya"], Status.APPROVED, approved_at=NOW - 5 * MIN)
    open_window(session)
    followup.run_once()
    assert not sent.messages


def test_the_sender_of_a_whatsapp_file_hears_what_became_of_it(on, sent, session):
    ok = make_invoice(session, on["priya"], Status.EXPORTED, source="whatsapp", created_at=NOW - 3 * MIN)
    held = make_invoice(session, on["priya"], Status.NEEDS_REVIEW, source="whatsapp", created_at=NOW - 3 * MIN,
                        reason="GSTIN checksum fails")
    make_invoice(session, on["priya"], Status.APPROVED, source="gmail", created_at=NOW - 3 * MIN)
    make_invoice(session, on["priya"], Status.PROCESSING, source="whatsapp", created_at=NOW - 3 * MIN)   # not decided yet
    open_window(session)
    followup.run_once()
    assert len(sent.messages) == 1
    body = sent.messages[0]["body"]
    assert f"#{ok.id} Sri Lakshmi Steels ₹6,136.00: approved and in Excel" in body
    assert f"#{held.id}" in body and "needs a person to review it (GSTIN checksum fails)" in body
    assert [f.kind for f in rows(session, FollowUp)] == ["result", "result"]
    followup.run_once()                      # told once only
    assert len(sent.messages) == 1


def test_results_can_be_switched_off(on, sent, monkeypatch, session):
    monkeypatch.setattr(settings, "followup_result_notices", False)
    make_invoice(session, on["priya"], Status.APPROVED, source="whatsapp", created_at=NOW - 3 * MIN)
    open_window(session)
    followup.run_once()
    assert not sent.messages


def test_the_call_goes_to_the_escalation_user_when_one_is_set(on, sent, clock, monkeypatch, session):
    monkeypatch.setattr(settings, "followup_review_ladder", "voice@60")
    monkeypatch.setattr(settings, "followup_escalation_user_id", on["ravi"].id)
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    followup.run_once()
    assert [c["number"] for c in sent.calls] == [OTHER_NUMBER]
    assert rows(session, VoiceCall)[0].user_id == on["ravi"].id


def test_call_limit_per_person(on, sent, clock, monkeypatch, session):
    monkeypatch.setattr(settings, "followup_review_ladder", "voice@60")
    monkeypatch.setattr(settings, "followup_max_calls_per_day", 1)
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    followup.run_once()
    second = make_invoice(session, on["priya"], since=NOW - 80 * MIN)
    clock.now = NOW + 10 * MIN
    followup.run_once()
    assert len(sent.calls) == 1
    assert next(f for f in rows(session, FollowUp) if f.invoice_id == second.id).next_due_at > clock.now + 10 * 60 * MIN


# ---------- calls that nobody answers

@pytest.mark.parametrize("status", ["no-answer", "busy", "failed", "canceled"])
def test_an_unanswered_call_is_tried_once_more_then_the_ladder_ends(on, sent, clock, monkeypatch, session, status):
    monkeypatch.setattr(settings, "followup_review_ladder", "voice@60")
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    followup.run_once()
    call = rows(session, VoiceCall)[0]
    followup.finish_call(session, call.id, status)
    fu = rows(session, FollowUp)[0]
    assert (fu.step, fu.state) == (0, "active") and fu.next_due_at == NOW + 60 * MIN
    clock.now = NOW + 61 * MIN
    followup.run_once()
    assert len(sent.calls) == 2
    followup.finish_call(session, rows(session, VoiceCall)[1].id, status)
    fu = rows(session, FollowUp)[0]
    assert (fu.step, fu.state, fu.done_reason) == (1, "done", "finished")


# ---------- what people answer

def waiting_ladder(on, sent, session):
    inv = make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    open_window(session)
    followup.run_once()
    sent.messages.clear()
    return inv


def reply(on, text, media=False):
    followup.on_incoming(on["priya"].id, f"whatsapp:{NUMBER}", text, media)


def test_reply_1_pauses_reminders(on, sent, session):
    waiting_ladder(on, sent, session)
    reply(on, " 1 ")
    assert "I won't remind you about 1 invoice for 24 hours" in sent.messages[0]["body"]
    fu = rows(session, FollowUp)[0]
    assert fu.awaiting_reply_since is None and fu.next_due_at == NOW + timedelta(hours=24) and fu.state == "active"
    assert rows(session, FollowUpEvent)[-2].status == "reply"


def test_reply_2_snoozes_and_stop_ends_the_ladder(on, sent, session):
    waiting_ladder(on, sent, session)
    reply(on, "2")
    assert rows(session, FollowUp)[0].next_due_at == NOW + timedelta(hours=2) and "in 2 hours" in sent.messages[0]["body"]
    session.query(FollowUp).update({"awaiting_reply_since": NOW})
    session.commit()
    reply(on, "STOP")
    fu = rows(session, FollowUp)[0]
    assert (fu.state, fu.done_reason) == ("done", "stopped")


def test_a_reply_never_changes_the_invoice(on, sent, session):
    inv = waiting_ladder(on, sent, session)
    for text in ("1", "approve", "approved", "yes approve it"):
        reply(on, text)
    session.refresh(inv)
    assert inv.status == Status.NEEDS_REVIEW and inv.approved_by is None


def test_other_text_gets_the_options_once_in_a_while(on, sent, session):
    waiting_ladder(on, sent, session)
    reply(on, "hello?")
    assert sent.messages[0]["body"] == followup.HELP_WITH_WAITING
    reply(on, "anyone there")
    assert len(sent.messages) == 1


def test_text_when_nothing_is_waiting(on, sent, session):
    reply(on, "hi")
    assert sent.messages[0]["body"] == followup.HELP_NOTHING
    sent.messages.clear()
    followup.on_incoming(on["priya"].id, f"whatsapp:{NUMBER}", "1", False)
    assert sent.messages[0]["body"] == "Nothing is waiting for a reply right now."
    sent.messages.clear()
    followup.on_incoming(on["priya"].id, f"whatsapp:{NUMBER}", "", True)       # a file with no text: no chatter
    assert not sent.messages


def test_receiving_a_message_opens_the_24_hour_window(on, sent, session):
    reply(on, "hi")
    row = session.get(WhatsAppSession, NUMBER)
    assert row is not None and row.last_inbound_at == NOW


def test_receipt_and_unreadable_messages(on, sent, session):
    followup.acknowledge_file(on["priya"].id, f"whatsapp:{NUMBER}", True)
    followup.acknowledge_file(on["priya"].id, f"whatsapp:{NUMBER}", False)
    followup.notify_unreadable(on["priya"].id, f"whatsapp:{NUMBER}")
    bodies = [m["body"] for m in sent.messages]
    assert "queued it for reading" in bodies[0] and "already received" in bodies[1] and "unsupported type" in bodies[2]
    assert all("Twilio" not in b and "http" not in b for b in bodies)


def test_the_phone_number_is_never_written_to_the_event_log(on, sent, session):
    waiting_ladder(on, sent, session)
    reply(on, "1")
    text = " ".join(f"{e.detail} {e.provider_sid}" for e in rows(session, FollowUpEvent))
    assert NUMBER not in text and NUMBER[3:] not in text


# ---------- email (through a connected Gmail, no Twilio)

def email_ladder(monkeypatch, ladder="email@60,email@120", to="priya@example.com"):
    monkeypatch.setattr(settings, "followup_review_ladder", ladder)
    monkeypatch.setattr(settings, "followup_email_to", to)


def test_email_reminders_go_from_the_connected_gmail(on, sent, monkeypatch, clock, session):
    email_ladder(monkeypatch)
    monkeypatch.setattr(settings, "report_app_url", "https://app.example.test")
    make_invoice(session, on["priya"], since=NOW - 90 * MIN, name="Steel <&> Co")
    followup.run_once()
    mail, = sent.emails
    assert mail["to"] == "priya@example.com" and mail["sender"] == "priya@example.com"   # the only connected Gmail
    assert mail["subject"] == "Invoice app reminder: 1 waiting for review"
    assert "Steel &lt;&amp;&gt; Co" in mail["html"] and 'href="https://app.example.test"' in mail["html"]
    assert "Reply 1" not in mail["html"] and outbound.email_marker() in mail["html"]
    # it says what it is, why it came, how long the invoice has waited and what to do
    assert "automatic reminder" in mail["html"] and "the oldest for 1 hour" in mail["html"]
    assert "What to do:" in mail["html"] and "Why you got this" in mail["html"] and "Excel invoice workbook" not in mail["html"]
    event, = rows(session, FollowUpEvent)
    assert (event.channel, event.direction, event.status) == ("email", "out", "sent") and event.detail is None
    fu = rows(session, FollowUp)[0]
    assert fu.step == 1 and fu.awaiting_reply_since is None          # emails take no replies
    clock.now = NOW + 31 * MIN
    followup.run_once()
    assert len(sent.emails) == 2 and rows(session, FollowUp)[0].state == "done"


def test_only_listed_addresses_get_emails(on, sent, monkeypatch, session):
    email_ladder(monkeypatch, to="someone-else@example.com")
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    followup.run_once()
    assert not sent.emails and not rows(session, FollowUp)


def test_a_step_on_a_channel_the_person_lacks_is_skipped(on, sent, monkeypatch, clock, session):
    """Ravi has no WhatsApp number but his email is listed: the WhatsApp step is skipped, the email goes."""
    monkeypatch.setattr(settings, "whatsapp_senders", f"{NUMBER}:{on['priya'].id}")
    email_ladder(monkeypatch, ladder="whatsapp@60,email@120", to="RAVI@example.com")
    make_invoice(session, on["ravi"], since=NOW - 90 * MIN)
    followup.run_once()
    event, = rows(session, FollowUpEvent)
    assert (event.channel, event.status, event.detail) == ("whatsapp", "skipped", "no number in WHATSAPP_SENDERS")
    assert (rows(session, FollowUp)[0].state, rows(session, FollowUp)[0].step) == ("active", 1)
    clock.now = NOW + 31 * MIN
    followup.run_once()
    assert [m["to"] for m in sent.emails] == ["ravi@example.com"]


def test_email_and_whatsapp_reminders_are_capped_separately(on, sent, monkeypatch, clock, session):
    monkeypatch.setattr(settings, "followup_max_per_day", 1)
    email_ladder(monkeypatch, ladder="whatsapp@60,email@120")
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    open_window(session)
    followup.run_once()
    clock.now = NOW + 31 * MIN
    followup.run_once()
    assert len(sent.messages) == 1 and len(sent.emails) == 1


def test_no_connected_gmail_means_no_sender(on, sent, monkeypatch, session):
    email_ladder(monkeypatch)
    on["priya"].gmail_account_id = None
    session.commit()
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    followup.run_once()
    assert sent.emails[0]["sender"] is None      # outbound.send_email reports that as a failure


# ---------- API

def test_followup_api_lists_and_stops(client, on, sent, session):
    inv = make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    open_window(session)
    followup.run_once()
    body = client.get("/api/v1/followups").json()
    assert body["enabled"] is True and body["dry_run"] is False
    item, = body["items"]
    assert item["invoice_id"] == inv.id and item["kind"] == "review" and item["step"] == 1 and item["awaiting_reply"]
    assert item["ladder"] == ["whatsapp@60m", "whatsapp@120m", "voice@180m"] and item["invoice"]["supplier"] == "Sri Lakshmi Steels"
    assert client.get("/api/v1/followups?state=done").json()["items"] == []
    events = client.get("/api/v1/followups/events").json()
    assert [e["status"] for e in events] == ["sent"] and "NUMBER" not in str(events)

    assert client.post(f"/api/v1/followups/{item['id']}/stop").status_code == 422          # no X-User-Id
    r = client.post(f"/api/v1/followups/{item['id']}/stop", headers=h(on["ravi"]))
    assert r.status_code == 200 and r.json()["state"] == "done" and r.json()["done_reason"] == "stopped"
    assert client.post(f"/api/v1/followups/{item['id']}/stop", headers=h(on["ravi"])).status_code == 409
    assert client.post("/api/v1/followups/999/stop", headers=h(on["ravi"])).status_code == 404
    assert client.get("/api/v1/voice/calls").json() == []
