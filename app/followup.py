"""Follow-up WhatsApp messages, emails and phone calls: nobody should have to remember to look.

Three kinds of follow-up, one row each per invoice (table follow_ups):
  review  invoice waiting for a person (NEEDS_REVIEW / NEEDS_PASSWORD): a ladder of reminders,
          by default WhatsApp after 4 h, WhatsApp after 24 h, then an AI phone call after 48 h
          (an email step, "email@minutes", goes out through a connected Gmail: no Twilio needed)
  export  invoice APPROVED but not in Excel for 30 minutes (the workbook is open, or no worker runs)
  result  a file sent on WhatsApp: tell the sender what became of it (approved, needs review, ...)
The ladders are FOLLOWUP_REVIEW_LADDER / FOLLOWUP_EXPORT_LADDER ("channel@minutes,...").

Rules that keep it from becoming noise or a risk:
  - Off unless FOLLOWUP_ENABLED=true, and with FOLLOWUP_DRY_RUN=true (the default) nothing is sent:
    each step is only recorded in follow_up_events, so the whole ladder can be watched first.
  - Only numbers listed in WHATSAPP_SENDERS and addresses in FOLLOWUP_EMAIL_TO are contacted (a step
    for a channel the person can't be reached on is skipped), only within FOLLOWUP_HOURS, with a daily
    cap per person. A person gets one message (or call) listing their invoices, not one per invoice.
  - A reply or a call can only pause reminders (1 = I'm handling it, 2 = remind me later, stop).
    It never approves, rejects or changes an invoice: that stays in the app, with a verified user.
    Emails take no replies: they stop by themselves once the invoice is dealt with.
  - The ladder stops by itself the moment the invoice leaves the waiting state.
  - WhatsApp allows free-form messages only for 24 hours after the person last wrote to us; after
    that the approved template (TWILIO_WHATSAPP_TEMPLATE_SID) is used, or the step is skipped and
    the ladder moves on (the call is the fallback).

Several processes may run this: due rows are claimed with SKIP LOCKED and a lease, and Twilio is
never called while a database transaction holds rows.
"""
import functools
import html
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import outbound, whatsapp
from app.config import settings
from app.db import SessionLocal
from app.models import FollowUp, FollowUpEvent, Invoice, Status, User, VoiceCall, WhatsAppSession
from app.reports import _money, _signed_total, default_sender

log = logging.getLogger("followup")

CHECK_EVERY_SECONDS = 60
CLAIM_LEASE_MINUTES = 10        # a claimed follow-up is not claimed again for this long
CALL_WAIT_MINUTES = 30          # after placing a call: the status callback should arrive within this
CALL_RETRY_MINUTES = 60         # a call that nobody answered is tried once more after this
MAX_CALL_ATTEMPTS = 2
MAX_SEND_FAILURES = 3
RETRY_AFTER_FAILURE_MINUTES = 30
SESSION_MARGIN = timedelta(minutes=10)   # WhatsApp's 24-hour window, with a margin
HELP_EVERY_MINUTES = 10
LINES_PER_SECTION = 5
WAITING = (Status.NEEDS_REVIEW, Status.NEEDS_PASSWORD)
RESULT_STATUSES = (Status.APPROVED, Status.EXPORTED, Status.NEEDS_REVIEW, Status.NEEDS_PASSWORD,
                   Status.DUPLICATE, Status.NOT_INVOICE, Status.REJECTED, Status.FAILED)
RESULT_WORDS = {
    Status.APPROVED: "approved, waiting to be written to Excel", Status.EXPORTED: "approved and in Excel",
    Status.NEEDS_REVIEW: "needs a person to review it", Status.NEEDS_PASSWORD: "is password protected: enter the password in the app",
    Status.DUPLICATE: "was already received", Status.NOT_INVOICE: "is not an invoice",
    Status.REJECTED: "was rejected", Status.FAILED: "could not be read",
}
COMMANDS = {"1": "handling", "2": "later", "stop": "stop"}
CHANNELS = ("whatsapp", "email", "voice")
NO_ADDRESS = {"whatsapp": "no number in WHATSAPP_SENDERS", "voice": "no number in WHATSAPP_SENDERS",
              "email": "email address not in FOLLOWUP_EMAIL_TO"}
PAUSING_OUTCOMES = ("handling", "remind_later")
HELP_WITH_WAITING = "Reply 1 if you are handling it, 2 to be reminded later, or STOP to end these reminders."
HELP_NOTHING = "Send me an invoice as a PDF or a photo and I will process it."


def _now() -> datetime:
    return datetime.now().astimezone()


# ---------- settings

@functools.lru_cache(maxsize=8)
def _parse_ladder(raw: str) -> tuple[tuple[str, int], ...]:
    steps = []
    for item in raw.split(","):
        item = item.strip().lower()
        if not item:
            continue
        channel, _, minutes = item.partition("@")
        if channel not in CHANNELS or not minutes.strip().isdigit():
            log.warning("Follow-up ladder step %r is not 'whatsapp@minutes', 'email@minutes' or 'voice@minutes'; "
                        "skipped", item)
            continue
        steps.append((channel, int(minutes)))
    return tuple(sorted(steps, key=lambda s: s[1]))


def ladder(kind: str) -> tuple[tuple[str, int], ...]:
    if kind == "result":
        return (("whatsapp", 0),)
    return _parse_ladder(settings.followup_review_ladder if kind == "review" else settings.followup_export_ladder)


@functools.lru_cache(maxsize=8)
def _senders(raw: str) -> dict[str, int | None]:
    return whatsapp.parse_senders(raw)


def number_for(user_id: int) -> str | None:
    """The WhatsApp/phone number of a user: only numbers in WHATSAPP_SENDERS are ever contacted."""
    for number, uid in _senders(settings.whatsapp_senders).items():
        if uid == user_id:
            return number
    return None


@functools.lru_cache(maxsize=8)
def _emails(raw: str) -> frozenset[str]:
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def address_for(session: Session, user_id: int, channel: str) -> str | None:
    """Where a user is reached on a channel: a number from WHATSAPP_SENDERS, or their users.email when
    FOLLOWUP_EMAIL_TO lists it (without a login anyone can create a user with any email address)."""
    if channel != "email":
        return number_for(user_id)
    user = session.get(User, user_id)
    if user is None or not user.email or user.email.lower() not in _emails(settings.followup_email_to):
        return None
    return user.email


def reachable_user_ids(session: Session, channels=CHANNELS) -> set[int]:
    """Users with an address on at least one of `channels`."""
    ids = set()
    if {"whatsapp", "voice"} & set(channels):
        ids |= {uid for uid in _senders(settings.whatsapp_senders).values() if uid}
    emails = _emails(settings.followup_email_to)
    if "email" in channels and emails:
        ids |= set(session.scalars(select(User.id).where(func.lower(User.email).in_(emails))))
    return ids


def _reachable_later(session: Session, fu: FollowUp) -> bool:
    """Can a later step of this follow-up's ladder reach its person?"""
    return any(address_for(session, fu.target_user_id, channel) for channel, _ in ladder(fu.kind)[fu.step + 1:])


def _parse_time(text: str) -> dtime:
    hour, minute = text.strip().split(":")
    return dtime(int(hour), int(minute))


def _hours() -> tuple[dtime, dtime]:
    try:
        start, end = settings.followup_hours.split("-")
        return _parse_time(start), _parse_time(end)
    except ValueError:
        log.warning("FOLLOWUP_HOURS %r isn't 'HH:MM-HH:MM'; using 09:00-19:00", settings.followup_hours)
        return dtime(9, 0), dtime(19, 0)


def in_hours(now: datetime) -> bool:
    start, end = _hours()
    t = now.time().replace(tzinfo=None)
    return start <= t < end if start <= end else (t >= start or t < end)


def next_open(now: datetime) -> datetime:
    """When the sending hours next begin (now, if they have)."""
    if in_hours(now):
        return now
    start, _ = _hours()
    at = now.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def _midnight(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ---------- finding and closing follow-ups

def discover(session: Session, now: datetime) -> int:
    """Start a follow-up for invoices that have been waiting long enough. Only invoices of people we can
    reach on the ladder's channels are considered, and a follow-up starts only once the first step is
    due: an invoice that is dealt with quickly never gets a row."""
    batch = settings.followup_batch
    created = 0

    def start(kind: str, rows) -> int:
        if not rows:
            return 0
        values = [{"invoice_id": r.id, "kind": kind, "target_user_id": r.user_id, "started_at": r.since,
                   "next_due_at": now} for r in rows]
        stmt = pg_insert(FollowUp).values(values).on_conflict_do_nothing(index_elements=["invoice_id", "kind"])
        return session.execute(stmt).rowcount or 0

    has = lambda kind: ~exists().where(FollowUp.invoice_id == Invoice.id, FollowUp.kind == kind)   # noqa: E731
    # (kind, statuses, the time it has been waiting since)
    for kind, statuses, column in (("review", WAITING, Invoice.updated_at),
                                   ("export", (Status.APPROVED,), Invoice.approved_at)):
        steps = ladder(kind)
        reachable = reachable_user_ids(session, {channel for channel, _ in steps})
        if not steps or not reachable:
            continue
        cutoff = now - timedelta(minutes=steps[0][1])
        rows = session.execute(
            select(Invoice.id, Invoice.user_id, column.label("since"))
            .where(Invoice.status.in_(statuses), column <= cutoff, Invoice.user_id.in_(reachable), has(kind))
            .order_by(column).limit(batch)).all()
        created += start(kind, rows)
    reachable = reachable_user_ids(session, ("whatsapp",))
    if settings.followup_result_notices and reachable:
        rows = session.execute(
            select(Invoice.id, Invoice.user_id, Invoice.created_at.label("since"))
            .where(Invoice.source == "whatsapp", Invoice.status.in_(RESULT_STATUSES),
                   Invoice.created_at >= now - timedelta(hours=24), Invoice.user_id.in_(reachable), has("result"))
            .order_by(Invoice.id).limit(batch)).all()
        created += start("result", rows)
    return created


def close_resolved(session: Session) -> None:
    """The invoice was dealt with: its ladder ends (no more reminders)."""
    for kind, still_open in (("review", Invoice.status.notin_(WAITING)), ("export", Invoice.status != Status.APPROVED)):
        session.execute(update(FollowUp).where(
            FollowUp.state == "active", FollowUp.kind == kind,
            FollowUp.invoice_id.in_(select(Invoice.id).where(still_open)))
            .values(state="done", done_reason="resolved", awaiting_reply_since=None))


def _close(fu: FollowUp, reason: str) -> None:
    fu.state, fu.done_reason, fu.awaiting_reply_since = "done", reason, None


def _resolved(kind: str, inv: Invoice) -> bool:
    if kind == "review":
        return inv.status not in WAITING
    if kind == "export":
        return inv.status != Status.APPROVED
    return False


def _advance(fu: FollowUp, now: datetime) -> None:
    """One ladder step is taken: schedule the next one, or finish."""
    steps = ladder(fu.kind)
    fu.step += 1
    fu.last_sent_at = now
    if fu.step >= len(steps):
        fu.state, fu.done_reason = "done", "finished"
    else:
        fu.next_due_at = max(fu.started_at + timedelta(minutes=steps[fu.step][1]), now + timedelta(minutes=1))


def _event(session: Session, fu_id: int | None, user_id: int | None, channel: str, direction: str, status: str,
           detail: str | None = None, sid: str | None = None) -> None:
    session.add(FollowUpEvent(follow_up_id=fu_id, user_id=user_id, channel=channel, direction=direction,
                              status=status, detail=(detail or None) and detail[:500], provider_sid=sid))


# ---------- what is said

def _name(inv: Invoice) -> str:
    return inv.supplier_name or inv.original_filename


def _amount(inv: Invoice) -> str:
    total, currency = _signed_total(inv)
    return _money(total, currency)


def _short(text: str | None, limit: int = 110) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _lines(kind: str, items: list[tuple[FollowUp, Invoice]]) -> list[str]:
    out = []
    for _, inv in items[:LINES_PER_SECTION]:
        head = f"#{inv.id} {_name(inv)} {_amount(inv)}"
        if kind == "result":
            more = f" ({_short(inv.status_reason)})" if inv.status in (Status.NEEDS_REVIEW, Status.FAILED) and inv.status_reason else ""
            out.append(f"{head}: {RESULT_WORDS.get(inv.status, inv.status)}{more}")
        elif kind == "review":
            out.append(f"{head}: {_short(inv.status_reason) or RESULT_WORDS.get(inv.status, inv.status)}")
        else:
            out.append(f"{head}: approved but not in Excel yet")
    if len(items) > LINES_PER_SECTION:
        out.append(f"…and {len(items) - LINES_PER_SECTION} more")
    return out


def _sections(items: list[tuple[FollowUp, Invoice]]) -> list[tuple[str, str, list[str]]]:
    """(kind, heading, lines) for everything due for one person."""
    by_kind: dict[str, list] = {"result": [], "review": [], "export": []}
    for fu, inv in items:
        by_kind[fu.kind].append((fu, inv))
    out = []
    if by_kind["result"]:
        out.append(("result", "About the files you sent:", _lines("result", by_kind["result"])))
    if by_kind["review"]:
        n = len(by_kind["review"])
        out.append(("review", f"{n} invoice{'s are' if n != 1 else ' is'} waiting for review:",
                    _lines("review", by_kind["review"])))
    if by_kind["export"]:
        out.append(("export", "Approved but not yet in the Excel file (if the workbook is open, close it):",
                    _lines("export", by_kind["export"])))
    return out


def compose(items: list[tuple[FollowUp, Invoice]]) -> str:
    """One WhatsApp message for everything due for one person."""
    sections = _sections(items)
    parts = [heading + "\n" + "\n".join(lines) for _, heading, lines in sections]
    if any(kind == "review" for kind, _, _ in sections):
        parts.append("Open the invoice app to review them. " + HELP_WITH_WAITING)
    return "\n\n".join(parts)


def compose_email(name: str, items: list[tuple[FollowUp, Invoice]]) -> tuple[str, str]:
    """The same reminder as an email: (subject, HTML). It ends with outbound.email_marker(), so the
    Gmail listener never reads our own reminder as an invoice (it lists amounts and the word invoice)."""
    counts = {kind: sum(1 for fu, _ in items if fu.kind == kind) for kind in ("review", "export")}
    about = [text for n, text in ((counts["review"], f"{counts['review']} waiting for review"),
                                  (counts["export"], f"{counts['export']} approved but not in Excel")) if n]
    subject = "Invoice app reminder" + (": " + ", ".join(about) if about else "")
    link = settings.report_app_url.rstrip("/")
    app = f'<a href="{html.escape(link)}">the invoice app</a>' if link else "the invoice app"
    oldest = _now() - min(fu.started_at for fu, _ in items)
    waited = f"{oldest.days} day{'s' if oldest.days != 1 else ''}" if oldest.days else \
        f"{max(1, oldest.seconds // 3600)} hour{'s' if oldest.seconds >= 7200 else ''}"
    body = [f"<p>Hello {html.escape(name)},</p>",
            "<p>This is an automatic reminder from the accounts-payable invoice app. The invoices below are yours "
            f"(you uploaded them or they came to your mailbox) and are waiting for you; the oldest for {waited}. "
            "Until someone acts, they are not paid or written to the Excel register.</p>"]
    for _, heading, lines in _sections(items):
        body.append(f"<p><b>{html.escape(heading)}</b></p><ul>"
                    + "".join(f"<li>{html.escape(line)}</li>" for line in lines) + "</ul>")
    todo = []
    if counts["review"]:
        todo.append(f"open {app}, check each invoice waiting for review and approve or reject it")
    if counts["export"]:
        todo.append("close the Excel invoice workbook if it is open, so the approved invoices can be written to it")
    body.append(f"<p><b>What to do:</b> {'; '.join(todo)}. Nothing needs to be sent back to this email.</p>")
    body.append("<p>Why you got this: you get at most "
                f"{settings.followup_max_per_day} reminder emails a day, only during working hours, and they stop by "
                "themselves once each invoice is dealt with. If these invoices are not yours, tell the accounts team "
                "so they can stop the reminders.</p>")
    body.append(f'<p style="color:#888;font-size:12px">Sent by the invoice app · {outbound.email_marker()}</p>')
    return subject, "".join(body)


# ---------- the check: claim, prepare, send, record

@dataclass
class Group:
    """Everything due for one person on one channel, ready to send."""
    user_id: int
    channel: str
    address: str | None              # phone number, or email address
    fu_ids: list[int]
    text: str = ""                   # WhatsApp text, or the email's HTML
    subject: str = ""
    sender: User | None = None       # whose Gmail sends the email (detached from the session)
    template: bool = False
    skip: str | None = None          # why nothing is sent: no_address | window | limit
    call_id: int | None = None
    twiml: str | None = None         # given to Twilio with the call (app/voice.py call_twiml)
    kinds: set = field(default_factory=set)


def claim_due(session: Session, now: datetime) -> list[int]:
    """Follow-ups due now, claimed for this process (the lease keeps others off them)."""
    query = select(FollowUp.id).where(FollowUp.state == "active", FollowUp.next_due_at <= now)
    if not in_hours(now):
        query = query.where(FollowUp.kind == "result")      # file receipts are not held until the morning
    query = query.order_by(FollowUp.next_due_at).limit(settings.followup_batch).with_for_update(skip_locked=True)
    ids = list(session.scalars(query))
    if ids:
        session.execute(update(FollowUp).where(FollowUp.id.in_(ids))
                        .values(next_due_at=now + timedelta(minutes=CLAIM_LEASE_MINUTES)))
    session.commit()
    return ids


def _session_open(session: Session, number: str, now: datetime) -> bool:
    row = session.get(WhatsAppSession, number)
    return row is not None and now - row.last_inbound_at < timedelta(hours=24) - SESSION_MARGIN


def _count_today(session: Session, user_id: int, now: datetime, channel: str) -> int:
    if channel == "voice":
        return session.query(VoiceCall).filter(VoiceCall.user_id == user_id,
                                               VoiceCall.created_at >= _midnight(now)).count()
    return session.query(FollowUpEvent).filter(
        FollowUpEvent.user_id == user_id, FollowUpEvent.channel == channel, FollowUpEvent.direction == "out",
        FollowUpEvent.status.in_(("sent", "dry_run")), FollowUpEvent.follow_up_id.is_not(None),
        FollowUpEvent.created_at >= _midnight(now)).count()


def prepare(session: Session, ids: list[int], now: datetime) -> list[Group]:
    """Read the claimed follow-ups and decide what goes to whom. Follow-ups whose invoice was dealt
    with meanwhile are closed here; nothing is sent from this function."""
    fus = list(session.scalars(select(FollowUp).where(FollowUp.id.in_(ids))))
    invoices = {i.id: i for i in session.scalars(select(Invoice).where(Invoice.id.in_({f.invoice_id for f in fus})))}
    buckets: dict[tuple[int, str], list[tuple[FollowUp, Invoice]]] = {}
    for fu in fus:
        inv = invoices.get(fu.invoice_id)
        steps = ladder(fu.kind)
        if inv is None or _resolved(fu.kind, inv):
            _close(fu, "resolved")
        elif fu.step >= len(steps):
            _close(fu, "finished")
        else:
            channel = steps[fu.step][0]
            target = fu.target_user_id
            if channel == "voice" and settings.followup_escalation_user_id:
                target = settings.followup_escalation_user_id
            buckets.setdefault((target, channel), []).append((fu, inv))
    groups = []
    sender = None
    for (user_id, channel), items in buckets.items():
        group = Group(user_id, channel, address_for(session, user_id, channel), [fu.id for fu, _ in items],
                      kinds={fu.kind for fu, _ in items})
        if group.address is None:
            group.skip = "no_address"
        elif channel == "email":
            if _count_today(session, user_id, now, "email") >= settings.followup_max_per_day:
                group.skip = "limit"
            else:
                group.subject, group.text = compose_email(session.get(User, user_id).name, items)
                group.sender = sender = sender or _email_sender(session)
        elif channel == "whatsapp":
            group.text = compose(items)
            if _count_today(session, user_id, now, "whatsapp") >= settings.followup_max_per_day:
                group.skip = "limit"
            elif not _session_open(session, group.address, now):
                if settings.twilio_whatsapp_template_sid:
                    group.template = True
                else:
                    group.skip = "window"
        else:
            if _count_today(session, user_id, now, "voice") >= settings.followup_max_calls_per_day:
                group.skip = "limit"
            else:
                from app import voice      # here, not at the top: voice imports this module
                call = VoiceCall(user_id=user_id, follow_up_ids=group.fu_ids, status="placing")
                session.add(call)
                session.flush()
                group.call_id = call.id
                group.twiml = voice.call_twiml(session, call)
        groups.append(group)
    session.commit()
    return groups


def _email_sender(session: Session) -> User | None:
    """The connected Gmail that sends reminder emails: FOLLOWUP_EMAIL_SENDER_USER_ID, else the report's
    sender. Detached from the session, so it can be used after the session is closed."""
    uid = settings.followup_email_sender_user_id
    user = session.get(User, uid) if uid else default_sender(session)
    if user is None or not user.gmail_account_id:
        return None
    session.expunge(user)
    return user


def _send(group: Group) -> outbound.Result:
    """The only place that talks to Twilio or Gmail; called with no database transaction open."""
    if group.channel == "whatsapp":
        return outbound.send_whatsapp(group.address, group.text, template=group.template)
    if group.channel == "email":
        return outbound.send_email(group.sender, group.address, group.subject, group.text)
    base = settings.public_base_url.rstrip("/")
    return outbound.place_call(group.address, f"{base}/api/v1/webhook/voice?call={group.call_id}",
                               f"{base}/api/v1/webhook/voice/status?call={group.call_id}", twiml=group.twiml)


def record(session: Session, group: Group, result: outbound.Result | None, now: datetime) -> None:
    """Write down what happened to each follow-up in the group and schedule what comes next."""
    fus = list(session.scalars(select(FollowUp).where(FollowUp.id.in_(group.fu_ids)).with_for_update()))
    call = session.get(VoiceCall, group.call_id) if group.call_id else None
    if call is not None and result is not None:
        call.call_sid = result.sid
        call.status = {"sent": "ringing", "dry_run": "dry_run"}.get(result.status, "failed")
        if result.status == "failed":
            call.note, call.ended_at = result.detail, now
    for fu in fus:
        if fu.state != "active":
            continue
        if group.skip == "no_address":
            _event(session, fu.id, group.user_id, group.channel, "out", "skipped", NO_ADDRESS[group.channel])
            if _reachable_later(session, fu):
                _advance(fu, now)       # e.g. no WhatsApp number, but the next step is an email
            else:
                _close(fu, "unreachable")
        elif group.skip == "limit":
            _event(session, fu.id, group.user_id, group.channel, "out", "skipped", "daily limit reached")
            fu.next_due_at = next_open(_midnight(now) + timedelta(days=1))
        elif group.skip == "window":
            _event(session, fu.id, group.user_id, group.channel, "out", "skipped",
                   "24-hour WhatsApp window closed and no template is set; moving on")
            _advance(fu, now)
        elif result.status == "failed":
            _event(session, fu.id, group.user_id, group.channel, "out", "failed", result.detail)
            failures = session.query(FollowUpEvent).filter_by(follow_up_id=fu.id, status="failed").count()
            if failures >= MAX_SEND_FAILURES:
                _close(fu, "unreachable")
            else:
                fu.next_due_at = now + timedelta(minutes=RETRY_AFTER_FAILURE_MINUTES)
        else:
            _event(session, fu.id, group.user_id, group.channel, "out", result.status,
                   "template" if group.template else None, result.sid)
            if group.channel == "voice" and result.status == "sent":
                fu.next_due_at = now + timedelta(minutes=CALL_WAIT_MINUTES)   # the status callback moves it on
            else:
                if group.channel == "whatsapp" and fu.kind == "review":
                    fu.awaiting_reply_since = now
                _advance(fu, now)
    session.commit()


def run_once(now: datetime | None = None) -> int:
    """One check: start follow-ups, close the resolved, send what is due. Returns how many groups were handled."""
    now = now or _now()
    with SessionLocal() as session:
        discover(session, now)
        close_resolved(session)
        session.commit()
        ids = claim_due(session, now)
        groups = prepare(session, ids, now) if ids else []
    for group in groups:
        result = None
        if group.skip is None:
            try:
                result = _send(group)
            except Exception as e:      # the class only: a Twilio message can contain numbers
                log.error("Follow-up send failed: %s", type(e).__name__)
                result = outbound.Result("failed", detail=type(e).__name__)
        with SessionLocal() as session:
            record(session, group, result, now)
        log.info("Follow-up %s to user %s: %s (%d invoice follow-up(s))", group.channel, group.user_id,
                 group.skip or (result.status if result else "?"), len(group.fu_ids))
    return len(groups)


def run(stop: threading.Event) -> None:
    """Background loop (started with the API when FOLLOWUP_ENABLED=true)."""
    log.info("Follow-ups: %s; review ladder %s; hours %s", "DRY RUN (nothing is sent)" if settings.followup_dry_run
             else "LIVE", settings.followup_review_ladder, settings.followup_hours)
    if not (settings.whatsapp_senders.strip() or settings.followup_email_to.strip()):
        log.warning("Follow-ups are on but WHATSAPP_SENDERS and FOLLOWUP_EMAIL_TO list no one: "
                    "nobody can be contacted")
    while not stop.is_set():
        try:
            run_once()
        except Exception:
            log.exception("Follow-up check failed (will try again)")
        stop.wait(CHECK_EVERY_SECONDS)


# ---------- what people answer

def _awaiting(session: Session, user_id: int, now: datetime) -> list[FollowUp]:
    return list(session.scalars(select(FollowUp).where(
        FollowUp.target_user_id == user_id, FollowUp.awaiting_reply_since.is_not(None),
        FollowUp.awaiting_reply_since >= now - timedelta(hours=48)).with_for_update()))


def _pause(fu: FollowUp, now: datetime, hours: float) -> None:
    if fu.state == "active":
        fu.next_due_at = now + timedelta(hours=hours)
    fu.awaiting_reply_since = None


def _handle_text(session: Session, user_id: int, number: str, text: str, has_media: bool,
                 now: datetime) -> str | None:
    """What to answer to a WhatsApp text from a known sender (changes only reminder timing)."""
    command = COMMANDS.get(text)
    waiting = _awaiting(session, user_id, now)
    if command:
        if not waiting:
            return "Nothing is waiting for a reply right now."
        for fu in waiting:
            if command == "handling":
                _pause(fu, now, settings.followup_ack_hours)
            elif command == "later":
                _pause(fu, now, settings.followup_snooze_hours)
            else:
                _close(fu, "stopped")
            _event(session, fu.id, user_id, "whatsapp", "in", "reply", command)
        n = len(waiting)
        what = f"{n} invoice{'s' if n != 1 else ''}"
        if command == "handling":
            return f"Thanks. I won't remind you about {what} for {settings.followup_ack_hours:g} hours."
        if command == "later":
            return f"OK, I will remind you about {what} in {settings.followup_snooze_hours:g} hours."
        return f"OK, no more reminders about {what}."
    if has_media or not text:
        return None
    recent = session.query(FollowUpEvent).filter(
        FollowUpEvent.user_id == user_id, FollowUpEvent.direction == "out", FollowUpEvent.detail == "help",
        FollowUpEvent.created_at >= now - timedelta(minutes=HELP_EVERY_MINUTES)).count()
    if recent:
        return None
    return HELP_WITH_WAITING if waiting else HELP_NOTHING


def reply(user_id: int, number: str, text: str, label: str = "reply") -> None:
    """A free-form answer inside the 24-hour window the person just opened. Not counted in the daily cap."""
    result = outbound.send_whatsapp(number, text)
    with SessionLocal() as session:
        _event(session, None, user_id, "whatsapp", "out", result.status,
               label if result.status != "failed" else f"{label}: {result.detail}", result.sid)
        session.commit()


def on_incoming(user_id: int, sender: str, body: str, has_media: bool) -> None:
    """Called by the WhatsApp webhook for a known sender (a background task). Remembers when they last
    wrote (the 24-hour window) and answers 1 / 2 / STOP."""
    if not settings.followup_enabled:
        return
    number = whatsapp.normalize_number(sender)
    if number is None:
        return
    now = _now()
    try:
        with SessionLocal() as session:
            session.execute(pg_insert(WhatsAppSession).values(number=number, last_inbound_at=now)
                            .on_conflict_do_update(index_elements=["number"], set_={"last_inbound_at": now}))
            text = " ".join(body.lower().split())
            answer = _handle_text(session, user_id, number, text, has_media, now)
            session.commit()
        if answer:
            reply(user_id, number, answer, "help" if answer in (HELP_WITH_WAITING, HELP_NOTHING) else "reply")
    except Exception as e:
        log.error("Could not handle an incoming WhatsApp reply: %s", type(e).__name__)


def acknowledge_file(user_id: int, sender: str, created: bool) -> None:
    """Tell the sender their attachment arrived (a background task of the WhatsApp webhook)."""
    if settings.followup_enabled and (number := whatsapp.normalize_number(sender)):
        reply(user_id, number, "Received your file and queued it for reading. I will message you when it has been checked."
              if created else "That file was already received, so I haven't queued it again.", "receipt")


def notify_unreadable(user_id: int, sender: str) -> None:
    if settings.followup_enabled and (number := whatsapp.normalize_number(sender)):
        reply(user_id, number, "I couldn't use one of your attachments. It may be an unsupported type, too large or "
              "damaged. Please send a PDF or a photo of the invoice again.", "unreadable")


# ---------- phone calls (app/voice.py)

def call_context(session: Session, call: VoiceCall) -> list[dict]:
    """What the voice assistant may talk about: the call's invoices, from the database."""
    rows = []
    for fu in session.scalars(select(FollowUp).where(FollowUp.id.in_(call.follow_up_ids))):
        inv = session.get(Invoice, fu.invoice_id)
        if inv is None or _resolved(fu.kind, inv) or fu.kind == "result":
            continue
        waiting_hours = (_now() - fu.started_at).total_seconds() / 3600
        rows.append({"invoice": inv.id, "supplier": _name(inv), "amount": _amount(inv), "kind": fu.kind,
                     "why": _short(inv.status_reason, 160) or RESULT_WORDS.get(inv.status, inv.status),
                     "waiting_hours": round(waiting_hours)})
    return rows


def apply_outcome(session: Session, call_id: int, outcome: str, note: str | None = None) -> None:
    """What was agreed on the call. Like a WhatsApp reply, it only moves reminders: 'handling' pauses
    them, 'remind_later' snoozes them; other outcomes are just recorded for a person to see."""
    call = session.get(VoiceCall, call_id)
    if call is None or call.outcome is not None:
        return
    now = _now()
    call.outcome, call.note = outcome, (note or "")[:500] or None
    for fu in session.scalars(select(FollowUp).where(FollowUp.id.in_(call.follow_up_ids)).with_for_update()):
        if outcome == "handling":
            _pause(fu, now, settings.followup_ack_hours)
        elif outcome == "remind_later":
            _pause(fu, now, settings.followup_snooze_hours)
        _event(session, fu.id, call.user_id, "voice", "in", "reply", outcome + (f": {call.note}" if call.note else ""))
    session.commit()


def finish_call(session: Session, call_id: int, status: str) -> None:
    """Twilio's final call status (completed, no-answer, busy, failed, canceled): move the ladder on, or retry."""
    call = session.get(VoiceCall, call_id)
    if call is None:
        return
    now = _now()
    status = status.replace("-", "_")
    if call.ended_at is None:
        call.ended_at = now
    call.status = status
    if status == "completed" and call.outcome is None:
        call.outcome = "none"             # they hung up before anything was agreed
    for fu in session.scalars(select(FollowUp).where(FollowUp.id.in_(call.follow_up_ids)).with_for_update()):
        if fu.state != "active":
            continue
        steps = ladder(fu.kind)
        if fu.step >= len(steps) or steps[fu.step][0] != "voice":
            continue          # already moved on
        paused = fu.next_due_at if call.outcome in PAUSING_OUTCOMES and fu.next_due_at > now else None
        if status == "completed":
            _advance(fu, now)
        else:
            _event(session, fu.id, call.user_id, "voice", "out", "failed", f"call {status}")
            attempts = session.query(FollowUpEvent).filter_by(follow_up_id=fu.id, channel="voice", status="failed").count()
            if attempts >= MAX_CALL_ATTEMPTS:
                _advance(fu, now)
            else:
                fu.next_due_at = now + timedelta(minutes=CALL_RETRY_MINUTES)
        if paused is not None and fu.state == "active":
            fu.next_due_at = max(fu.next_due_at, paused)
    session.commit()


# ---------- for the API

def serialize(fu: FollowUp, inv: Invoice | None) -> dict:
    return {"id": fu.id, "invoice_id": fu.invoice_id, "kind": fu.kind, "target_user_id": fu.target_user_id,
            "state": fu.state, "done_reason": fu.done_reason, "step": fu.step,
            "ladder": [f"{c}@{m}m" for c, m in ladder(fu.kind)], "next_due_at": fu.next_due_at,
            "last_sent_at": fu.last_sent_at, "awaiting_reply": fu.awaiting_reply_since is not None,
            "invoice": None if inv is None else {"supplier": _name(inv), "status": inv.status, "amount": _amount(inv)}}


def serialize_event(e: FollowUpEvent) -> dict:
    return {"id": e.id, "follow_up_id": e.follow_up_id, "user_id": e.user_id, "channel": e.channel,
            "direction": e.direction, "status": e.status, "detail": e.detail, "created_at": e.created_at}


def serialize_call(c: VoiceCall) -> dict:
    return {"id": c.id, "user_id": c.user_id, "follow_up_ids": c.follow_up_ids, "status": c.status,
            "outcome": c.outcome, "note": c.note, "transcript": c.transcript, "created_at": c.created_at,
            "ended_at": c.ended_at}
