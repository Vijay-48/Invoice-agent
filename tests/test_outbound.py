"""Messages and calls through Twilio (app/outbound.py): dry run, missing settings, readable failures."""
import pytest

from app import outbound
from app.config import settings


class TwilioError(Exception):
    """Shaped like twilio.base.exceptions.TwilioRestException."""

    def __init__(self, status, code, msg):
        super().__init__(msg)
        self.status, self.code, self.msg = status, code, msg


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    for name, value in dict(followup_dry_run=False, twilio_account_sid="AC" + "a" * 32, twilio_auth_token="tok",
                            twilio_whatsapp_from="whatsapp:+14155238886", twilio_voice_from="+15550100",
                            twilio_whatsapp_template_sid="").items():
        monkeypatch.setattr(settings, name, value)


class FakeClient:
    """Records what is created; raises `error` instead when one is given."""

    def __init__(self, error=None):
        self.error = error
        self.created_messages, self.created_calls = [], []
        self.messages = type("Messages", (), {"create": lambda _, **kw: self._create(self.created_messages, "SM1", kw)})()
        self.calls = type("Calls", (), {"create": lambda _, **kw: self._create(self.created_calls, "CA1", kw)})()

    def _create(self, store, sid, kwargs):
        if self.error:
            raise self.error
        store.append(kwargs)
        return type("Created", (), {"sid": sid})()


def use(monkeypatch, error=None):
    fake = FakeClient(error)
    monkeypatch.setattr(outbound, "client", lambda: fake)
    return fake


def test_dry_run_never_creates_a_client(monkeypatch):
    monkeypatch.setattr(settings, "followup_dry_run", True)
    monkeypatch.setattr(outbound, "client", lambda: pytest.fail("a dry run must not touch Twilio"))
    assert outbound.send_whatsapp("+919876543210", "hi").status == "dry_run"
    assert outbound.place_call("+919876543210", "https://x/y", "https://x/z").status == "dry_run"


def test_missing_settings_are_named(monkeypatch):
    use(monkeypatch)
    assert "TEMPLATE_SID" in outbound.send_whatsapp("+919876543210", "hi", template=True).detail
    monkeypatch.setattr(settings, "twilio_whatsapp_from", "")
    assert "TWILIO_WHATSAPP_FROM" in outbound.send_whatsapp("+919876543210", "hi").detail
    monkeypatch.setattr(settings, "twilio_voice_from", "")
    assert "TWILIO_VOICE_FROM" in outbound.place_call("+919876543210", "https://x/y", "https://x/z").detail


def test_a_twilio_refusal_says_why_without_the_number(monkeypatch):
    use(monkeypatch, TwilioError(400, 0, "The number +919876543210 is unverified: trial accounts have limited access"))
    result = outbound.place_call("+919876543210", "https://x/y", "https://x/z")
    assert result.status == "failed"
    assert "HTTP 400" in result.detail and "code 0" in result.detail and "trial accounts" in result.detail
    assert "9876543210" not in result.detail and "<number>" in result.detail


TRIAL = "Unable to create record: Invalid or disallowed parameters provided - trial accounts have limited parameter access"


def picky(monkeypatch, refuse):
    """A client that refuses (as a trial account does) any call carrying one of `refuse`."""
    fake = use(monkeypatch)

    def create(**kw):
        fake.created_calls.append(kw)
        if refuse & kw.keys():
            raise TwilioError(400, 0, TRIAL)
        return type("Created", (), {"sid": "CA1"})()
    fake.calls = type("Calls", (), {"create": lambda _, **kw: create(**kw)})()
    return fake


def test_a_full_account_gets_every_limit(monkeypatch):
    fake = picky(monkeypatch, set())
    assert outbound.place_call("+919876543210", "https://x/y", "https://x/z").status == "sent"
    assert len(fake.created_calls) == 1
    assert {"timeout", "time_limit", "status_callback", "status_callback_event"} <= fake.created_calls[0].keys()


def test_a_trial_account_gets_the_call_with_fewer_settings(monkeypatch):
    fake = picky(monkeypatch, {"timeout", "time_limit", "status_callback_event"})
    assert outbound.place_call("+919876543210", "https://x/y", "https://x/z").sid == "CA1"
    assert fake.created_calls[-1].keys() == {"to", "from_", "url", "status_callback"}


def test_a_trial_account_that_refuses_the_callback_still_gets_the_call(monkeypatch):
    fake = picky(monkeypatch, {"status_callback"})
    assert outbound.place_call("+919876543210", "https://x/y", "https://x/z").status == "sent"
    assert fake.created_calls[-1].keys() == {"to", "from_", "url"} and len(fake.created_calls) == 3


def test_the_calls_twiml_is_given_up_front_and_the_url_is_the_fallback(monkeypatch):
    fake = picky(monkeypatch, set())
    outbound.place_call("+919876543210", "https://x/y", "https://x/z", twiml="<Response/>")
    assert fake.created_calls[0]["twiml"] == "<Response/>" and "url" not in fake.created_calls[0]
    fake = picky(monkeypatch, {"twiml"})       # an account that refused inline TwiML still gets the call
    assert outbound.place_call("+919876543210", "https://x/y", "https://x/z", twiml="<Response/>").status == "sent"
    assert fake.created_calls[-1]["url"] == "https://x/y" and len(fake.created_calls) == 4


def test_other_refusals_are_not_retried(monkeypatch):
    fake = use(monkeypatch, TwilioError(400, 21219, "The number <x> is unverified. Trial accounts may only call verified numbers"))
    calls = []
    fake.calls = type("Calls", (), {"create": lambda _, **kw: calls.append(kw) or (_ for _ in ()).throw(fake.error)})()
    result = outbound.place_call("+919876543210", "https://x/y", "https://x/z")
    assert result.status == "failed" and "21219" in result.detail
    # 'Trial' appears in the message, but only the "limited parameter access" refusal is retried with fewer settings
    assert len(calls) == 1


def test_an_error_that_is_not_twilios_shows_only_its_class(monkeypatch):
    use(monkeypatch, RuntimeError("connection to +919876543210 with token tok failed"))
    detail = outbound.send_whatsapp("+919876543210", "hi").detail
    assert detail == "RuntimeError"


def test_reminder_email_through_gmail(monkeypatch):
    from types import SimpleNamespace
    from app import gmail
    sent = []
    monkeypatch.setattr(gmail, "send_email", lambda user, to, subject, body: sent.append((user.email, to, subject)))
    me = SimpleNamespace(email="ap@example.com")
    assert outbound.send_email(me, "priya@example.com", "Reminder", "<p>x</p>").status == "sent"
    assert sent == [("ap@example.com", ["priya@example.com"], "Reminder")]
    assert "no connected Gmail" in outbound.send_email(None, "priya@example.com", "Reminder", "x").detail

    def broken(*a):
        raise RuntimeError("Composio: mailbox priya@example.com is not active")
    monkeypatch.setattr(gmail, "send_email", broken)
    result = outbound.send_email(me, "priya@example.com", "Reminder", "x")
    assert result.status == "failed" and "not active" in result.detail and "priya@" not in result.detail

    monkeypatch.setattr(settings, "followup_dry_run", True)
    monkeypatch.setattr(gmail, "send_email", lambda *a: pytest.fail("a dry run must not send email"))
    assert outbound.send_email(me, "priya@example.com", "Reminder", "x").status == "dry_run"


def test_the_reminder_marker_depends_on_the_key(monkeypatch):
    marker = outbound.email_marker()
    assert outbound.is_our_reminder(f"Hello\nSent by the invoice app · {marker}") and not outbound.is_our_reminder("")
    monkeypatch.setattr(settings, "composio_api_key", "ak_other")
    assert outbound.email_marker() != marker


def test_whatsapp_messages_and_template_variable(monkeypatch):
    fake = use(monkeypatch)
    assert outbound.send_whatsapp("+919876543210", "hello").sid == "SM1"
    monkeypatch.setattr(settings, "twilio_whatsapp_template_sid", "HX" + "a" * 32)
    outbound.send_whatsapp("+919876543210", "line one\n\nline two", template=True)
    assert fake.messages is not None
    assert outbound.one_line("a\n\n b\n  c") == "a | b | c"
    assert len(outbound.one_line("x" * 5000)) <= outbound.MAX_BODY
