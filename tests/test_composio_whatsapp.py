# ruff: noqa: F811  (pytest fixtures imported from test_followup)
"""Follow-up reminders sent through Composio's WhatsApp (FOLLOWUP_WHATSAPP_VIA=composio, D-114). Composio is
faked: nothing is sent. Receiving, replies and GRNs stay on Twilio and are tested elsewhere."""
from types import SimpleNamespace

import pytest

from app import followup, outbound
from app.config import settings
from app.models import FollowUpEvent
from tests.test_followup import MIN, NOW, NUMBER, clock, make_invoice, on, rows, sent  # noqa: F401


@pytest.fixture
def composio(monkeypatch):
    """A fake Composio client: records each tools.execute; `.answer` is what it returns."""
    calls = SimpleNamespace(executed=[], answer={"successful": True,
                                                 "data": {"messages": [{"id": "wamid.HBgM123"}]}})

    def execute(slug, arguments, *, user_id, connected_account_id):
        calls.executed.append({"slug": slug, "args": arguments, "user_id": user_id, "account": connected_account_id})
        if isinstance(calls.answer, Exception):
            raise calls.answer
        return calls.answer

    monkeypatch.setattr(outbound, "composio_whatsapp", lambda: SimpleNamespace(tools=SimpleNamespace(execute=execute)))
    for name, value in dict(followup_dry_run=False, composio_api_key="ak_test", composio_whatsapp_account_id="ca_wa",
                            composio_whatsapp_user_id="ap-whatsapp", composio_whatsapp_phone_number_id="1234567890",
                            composio_whatsapp_template="", composio_whatsapp_template_language="en").items():
        monkeypatch.setattr(settings, name, value)
    return calls


def test_plain_text(composio):
    result = outbound.send_whatsapp_composio(NUMBER, "2 invoices are waiting")
    assert (result.status, result.sid) == ("sent", "wamid.HBgM123")
    call, = composio.executed
    assert call == {"slug": "WHATSAPP_SEND_MESSAGE", "user_id": "ap-whatsapp", "account": "ca_wa",
                    "args": {"phone_number_id": "1234567890", "to_number": "919876543210",
                             "text": "2 invoices are waiting"}}


def test_template_puts_the_text_in_its_one_variable(composio, monkeypatch):
    monkeypatch.setattr(settings, "composio_whatsapp_template", "invoice_reminder")
    outbound.send_whatsapp_composio(NUMBER, "Waiting:\n#1 Steels 6,136", template=True)
    args = composio.executed[0]["args"]
    assert composio.executed[0]["slug"] == "WHATSAPP_SEND_TEMPLATE_MESSAGE"
    assert (args["template_name"], args["language_code"], args["to_number"]) == ("invoice_reminder", "en", "919876543210")
    assert args["components"] == [{"type": "body", "parameters": [{"type": "text", "text": "Waiting: | #1 Steels 6,136"}]}]


def test_failures_never_carry_the_number(composio):
    composio.answer = {"successful": False, "error": "Recipient 919876543210 not in allowed list"}
    result = outbound.send_whatsapp_composio(NUMBER, "x")
    assert result.status == "failed" and "919876543210" not in result.detail and "<number>" in result.detail
    composio.answer = ConnectionError("to 919876543210")
    result = outbound.send_whatsapp_composio(NUMBER, "x")
    assert (result.status, result.detail) == ("failed", "Composio: ConnectionError")


def test_missing_settings_and_dry_run(composio, monkeypatch):
    monkeypatch.setattr(settings, "composio_whatsapp_phone_number_id", "")
    assert outbound.send_whatsapp_composio(NUMBER, "x").detail == "COMPOSIO_WHATSAPP_PHONE_NUMBER_ID not set"
    monkeypatch.setattr(settings, "followup_dry_run", True)
    assert outbound.send_whatsapp_composio(NUMBER, "x").status == "dry_run" and not composio.executed


def test_message_id_wherever_the_toolkit_puts_it():
    assert outbound._message_id({"response_data": {"messages": [{"id": "wamid.A"}]}}) == "wamid.A"
    assert outbound._message_id({"id": "wamid.B"}) == "wamid.B"
    assert outbound._message_id({"ok": True}) is None


# ---------- the ladder, switched to Composio

@pytest.fixture
def via_composio(on, composio, monkeypatch):
    monkeypatch.setattr(settings, "followup_whatsapp_via", "composio")
    return on


def test_reminders_go_through_composio_not_twilio(via_composio, sent, composio, session):
    make_invoice(session, via_composio["priya"], since=NOW - 90 * MIN)   # never wrote to us: no window
    followup.run_once()
    assert not sent.messages                                           # Twilio not used
    call, = composio.executed
    assert call["slug"] == "WHATSAPP_SEND_MESSAGE" and call["args"]["to_number"] == "919876543210"
    text = call["args"]["text"]
    assert "Sri Lakshmi Steels" in text and "Replies to this number aren't read." in text and "Reply 1" not in text
    event, = rows(session, FollowUpEvent)
    assert (event.channel, event.status, event.provider_sid) == ("whatsapp", "sent", "wamid.HBgM123")


def test_with_a_template_it_is_always_the_template(via_composio, sent, composio, monkeypatch, session):
    monkeypatch.setattr(settings, "composio_whatsapp_template", "invoice_reminder")
    make_invoice(session, via_composio["priya"], since=NOW - 90 * MIN)
    followup.run_once()
    assert composio.executed[0]["slug"] == "WHATSAPP_SEND_TEMPLATE_MESSAGE"


def test_twilio_stays_the_default(on, sent, composio, monkeypatch, session):
    monkeypatch.setattr(settings, "twilio_whatsapp_template_sid", "HX" + "a" * 32)
    make_invoice(session, on["priya"], since=NOW - 90 * MIN)
    followup.run_once()
    assert len(sent.messages) == 1 and not composio.executed
    assert "Reply 1" in sent.messages[0]["body"]
