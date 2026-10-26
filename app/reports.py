"""Summary report by email: what came in, what was approved, what is waiting.

Sent from a connected Gmail account (Composio GMAIL_SEND_EMAIL), either on a schedule
(REPORT_SCHEDULE=daily|weekly|monthly; off by default) or by hand from the frontend.
The numbers come straight from the database (no AI), so the report is always the same
for the same data.
"""
import html
import logging
import threading
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models import EmailMessage, GmailHealth, Invoice, ReportRun, Status, User

log = logging.getLogger("reports")
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}            # reports sent by hand: the last N days
SCHEDULES = ("daily", "weekly", "monthly")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
CHECK_EVERY_SECONDS = 60
STUCK_AFTER_MINUTES = 30                                     # 'sending' this long = the server stopped mid-send
RETRY_AFTER_MINUTES = 30                                     # a failed scheduled report is tried again
NOT_COUNTED = {Status.DUPLICATE, Status.REJECTED, Status.NOT_INVOICE, Status.FAILED}
WAITING = (Status.NEEDS_REVIEW, Status.NEEDS_PASSWORD)
LIST_LIMIT = 10


class ReportError(Exception):
    """The report can't be sent (no Gmail to send from, no recipients, Gmail refused it)."""


def _now() -> datetime:
    return datetime.now().astimezone()


def _midnight(d) -> datetime:
    return datetime.combine(d, dtime.min).astimezone()


# ---------- schedule

def _send_time() -> dtime:
    try:
        h, m = settings.report_time.split(":")
        return dtime(int(h), int(m))
    except ValueError:
        log.warning("REPORT_TIME %r isn't HH:MM; using 09:00", settings.report_time)
        return dtime(9, 0)


def _weekday() -> int:
    day = settings.report_weekday.strip().lower()[:3]
    return WEEKDAYS.index(day) if day in WEEKDAYS else 0


def due_slot(now: datetime) -> tuple[str, datetime, datetime] | None:
    """The scheduled report due at `now` (slot name, period start, period end), if any.
    A report is due from its send time until the end of that day, so one missed while the
    server was down is still sent when it comes back the same day."""
    schedule = settings.report_schedule.strip().lower()
    if schedule not in SCHEDULES or now.time() < _send_time():
        return None
    today = now.date()
    if schedule == "daily":
        return f"daily:{today}", _midnight(today - timedelta(days=1)), _midnight(today)
    if schedule == "weekly":
        if today.weekday() != _weekday():
            return None
        return f"weekly:{today}", _midnight(today - timedelta(days=7)), _midnight(today)
    if today.day != 1:
        return None
    last_month = (today - timedelta(days=1)).replace(day=1)
    return f"monthly:{today:%Y-%m}", _midnight(last_month), _midnight(today)


def next_run(now: datetime | None = None) -> datetime | None:
    """When the next scheduled report goes out (None when the schedule is off)."""
    now = now or _now()
    if settings.report_schedule.strip().lower() not in SCHEDULES:
        return None
    for days in range(0, 33):
        day = now.date() + timedelta(days=days)
        at = datetime.combine(day, _send_time()).astimezone()
        if at >= now and due_slot(at):
            return at
    return None


# ---------- contents

def _signed_total(inv: Invoice) -> tuple[float, str]:
    d = inv.extracted or {}
    sign = -1 if d.get("document_type") == "credit_note" else 1
    return sign * float(inv.grand_total or 0), (d.get("currency") or "INR").upper()


def _money(amount: float, currency: str = "INR") -> str:
    """₹12,34,567.89 (Indian grouping); other currencies as 'USD 1,234.50'."""
    if currency != "INR":
        return f"{currency} {amount:,.2f}"
    sign, amount = ("-" if amount < 0 else ""), abs(amount)
    whole, frac = f"{amount:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups) + "," + tail
    return f"{sign}₹{whole}.{frac}"


def _totals(rows: list[Invoice]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for inv in rows:
        amount, currency = _signed_total(inv)
        out[currency] += amount
    return dict(out)


def _brief(inv: Invoice) -> dict:
    amount, currency = _signed_total(inv)
    return {"id": inv.id, "supplier": inv.supplier_name or inv.original_filename, "invoice_no": inv.invoice_no,
            "amount": amount, "currency": currency, "status": inv.status, "reason": inv.status_reason,
            "since": inv.created_at}


def build(session: Session, start: datetime, end: datetime) -> dict:
    """The report's numbers for documents and emails between start and end (and what is
    waiting right now)."""
    received = list(session.scalars(select(Invoice).where(Invoice.created_at >= start, Invoice.created_at < end)))
    approved = list(session.scalars(select(Invoice).where(
        Invoice.approved_at >= start, Invoice.approved_at < end,
        Invoice.status.in_([Status.APPROVED, Status.EXPORTED]))))
    waiting = list(session.scalars(select(Invoice).where(Invoice.status.in_(WAITING)).order_by(Invoice.created_at)))
    failed = list(session.scalars(select(Invoice).where(Invoice.status == Status.FAILED,
                                                        Invoice.updated_at >= start, Invoice.updated_at < end)))
    emails = list(session.scalars(select(EmailMessage).where(EmailMessage.created_at >= start,
                                                             EmailMessage.created_at < end)))
    by_status: dict[str, int] = defaultdict(int)
    for inv in received:
        by_status[inv.status] += 1
    suppliers: dict[str, dict] = {}
    for inv in approved:
        amount, currency = _signed_total(inv)
        key = (inv.supplier_name or "Unknown") + ("" if currency == "INR" else f" ({currency})")
        s = suppliers.setdefault(key, {"name": key, "documents": 0, "amount": 0.0, "currency": currency})
        s["documents"] += 1
        s["amount"] += amount
    email_outcomes: dict[str, int] = defaultdict(int)
    for e in emails:
        email_outcomes[e.outcome] += 1
    health = {h.user_id: h for h in session.scalars(select(GmailHealth))}
    inboxes = [{"user": u.name, "email": u.email, "last_sync": u.gmail_last_sync_at,
                "reconnect": getattr(health.get(u.id), "status", "ok") == "reconnect",
                "problem": getattr(health.get(u.id), "detail", None)}
               for u in session.scalars(select(User).where(User.gmail_account_id.is_not(None)).order_by(User.id))]
    return {
        "period_start": start, "period_end": end,
        "received": {"documents": len(received), "counted": sum(1 for i in received if i.status not in NOT_COUNTED),
                     "by_status": dict(sorted(by_status.items())),
                     "from_gmail": sum(1 for i in received if i.source == "gmail")},
        "approved": {"documents": len(approved), "auto": sum(1 for i in approved if i.approved_by == "auto"),
                     "totals": _totals(approved),
                     "in_excel": sum(1 for i in approved if i.status == Status.EXPORTED)},
        "waiting": {"documents": len(waiting), "totals": _totals(waiting),
                    "oldest": [_brief(i) for i in waiting[:LIST_LIMIT]]},
        "failed": [_brief(i) for i in failed[:LIST_LIMIT]],
        "top_suppliers": sorted(suppliers.values(), key=lambda s: -abs(s["amount"]))[:5],
        "emails": {"seen": len(emails), "by_outcome": dict(sorted(email_outcomes.items()))},
        "inboxes": inboxes,
    }


# ---------- email body

STATUS_WORDS = {Status.EXPORTED: "In Excel", Status.APPROVED: "Approved", Status.NEEDS_REVIEW: "Needs review",
                Status.NEEDS_PASSWORD: "Needs password", Status.DUPLICATE: "Duplicate",
                Status.NOT_INVOICE: "Not an invoice", Status.REJECTED: "Rejected", Status.FAILED: "Failed",
                Status.RECEIVED: "Waiting for AI", Status.PROCESSING: "Being read"}
OUTCOME_WORDS = {"done": "turned into documents", "no_documents": "had no invoice", "skipped": "skipped (filter)",
                 "failed": "failed", "pending": "in progress"}


def _period_text(start: datetime, end: datetime) -> str:
    last = end - timedelta(seconds=1)
    if start.date() == last.date():
        return f"{start:%d %b %Y}"
    return f"{start:%d %b} – {last:%d %b %Y}"


def _totals_text(totals: dict[str, float]) -> str:
    if not totals:
        return _money(0)
    return " + ".join(_money(v, c) for c, v in sorted(totals.items(), key=lambda kv: kv[0] != "INR"))


def render(data: dict) -> tuple[str, str]:
    """(subject, HTML body) for the report. Every value from documents/emails is escaped."""
    e = html.escape
    period = _period_text(data["period_start"], data["period_end"])
    w, a, r = data["waiting"], data["approved"], data["received"]
    subject = f"Invoice summary {period}: {a['documents']} approved, {w['documents']} waiting for review"
    base = settings.report_app_url.rstrip("/")

    def link(inv_id: int) -> str:
        return f'<a href="{e(base)}/#/invoices/{inv_id}">#{inv_id}</a>' if base else f"#{inv_id}"

    td = 'style="padding:6px 10px;border-bottom:1px solid #e5e7eb;"'
    tdr = 'style="padding:6px 10px;border-bottom:1px solid #e5e7eb;text-align:right;white-space:nowrap;"'
    th = 'style="padding:6px 10px;border-bottom:2px solid #d1d5db;text-align:left;font-size:12px;color:#6b7280;"'

    def table(head: list[str], rows: list[list[str]], right: set[int] = frozenset()) -> str:
        if not rows:
            return '<p style="color:#6b7280;margin:4px 0 0;">None.</p>'
        hs = "".join(f"<th {th}>{h}</th>" for h in head)
        body = "".join("<tr>" + "".join(f"<td {tdr if i in right else td}>{c}</td>" for i, c in enumerate(row))
                       + "</tr>" for row in rows)
        return f'<table style="border-collapse:collapse;width:100%;font-size:14px;"><tr>{hs}</tr>{body}</table>'

    def card(label: str, value: str, note: str = "") -> str:
        return (f'<td style="padding:12px 14px;border:1px solid #e5e7eb;border-radius:8px;background:#f9fafb;'
                f'vertical-align:top;width:33%;"><div style="font-size:12px;color:#6b7280;">{label}</div>'
                f'<div style="font-size:20px;font-weight:600;margin-top:2px;">{value}</div>'
                f'<div style="font-size:12px;color:#6b7280;margin-top:2px;">{note}</div></td>')

    h2 = 'style="font-size:15px;margin:22px 0 6px;"'
    parts = [
        '<div style="font-family:Segoe UI,Arial,sans-serif;color:#111827;max-width:720px;">',
        f'<h1 style="font-size:20px;margin:0 0 4px;">Invoice summary</h1>'
        f'<div style="color:#6b7280;margin-bottom:14px;">{e(period)}</div>',
        '<table style="border-collapse:separate;border-spacing:8px 0;width:100%;margin-left:-8px;"><tr>',
        card("Received", str(r["documents"]), f"{r['from_gmail']} from Gmail"),
        card("Approved", str(a["documents"]), f"{a['auto']} automatically · {e(_totals_text(a['totals']))}"),
        card("Waiting for review", str(w["documents"]), e(_totals_text(w["totals"]))),
        "</tr></table>",
        f"<h2 {h2}>Waiting for review (oldest first)</h2>",
        table(["Invoice", "Supplier", "Amount", "Why"],
              [[link(i["id"]), e(i["supplier"] or ""), e(_money(i["amount"], i["currency"])),
                e((i["reason"] or STATUS_WORDS.get(i["status"], i["status"]))[:160])] for i in w["oldest"]], {2}),
    ]
    if w["documents"] > len(w["oldest"]):
        parts.append(f'<p style="color:#6b7280;font-size:13px;">…and {w["documents"] - len(w["oldest"])} more.</p>')
    parts += [
        f"<h2 {h2}>Top suppliers approved</h2>",
        table(["Supplier", "Documents", "Amount"],
              [[e(s["name"]), str(s["documents"]), e(_money(s["amount"], s["currency"]))]
               for s in data["top_suppliers"]], {1, 2}),
        f"<h2 {h2}>Documents received, by result</h2>",
        table(["Result", "Documents"], [[e(STATUS_WORDS.get(k, k)), str(v)] for k, v in r["by_status"].items()], {1}),
    ]
    if data["failed"]:
        parts += [f"<h2 {h2}>Failed (need a look)</h2>",
                  table(["Invoice", "File / supplier", "Error"],
                        [[link(i["id"]), e(i["supplier"] or ""), e((i["reason"] or "")[:160])] for i in data["failed"]])]
    em = data["emails"]
    parts += [f"<h2 {h2}>Gmail</h2>",
              f'<p style="margin:4px 0;">{em["seen"]} email(s) checked'
              + (": " + ", ".join(f"{v} {e(OUTCOME_WORDS.get(k, k))}" for k, v in em["by_outcome"].items())
                 if em["by_outcome"] else "") + ".</p>",
              table(["Inbox", "Last checked", "Status"],
                    [[e(f"{i['user']} <{i['email']}>"), e(f"{i['last_sync']:%d %b %Y %H:%M}" if i["last_sync"] else "never"),
                      '<b style="color:#b91c1c;">Reconnect Gmail</b>' + (f"<br>{e(i['problem'])}" if i["problem"] else "")
                      if i["reconnect"] else "Working"]
                     for i in data["inboxes"]])]
    if base:
        parts.append(f'<p style="margin-top:20px;"><a href="{e(base)}/#/invoices">Open the invoice app</a></p>')
    parts.append('<p style="color:#9ca3af;font-size:12px;margin-top:24px;">Sent by the invoice processing app.</p></div>')
    return subject, "".join(parts)


def preview(session: Session, period: str = "week") -> dict:
    end = _now()
    start = end - timedelta(days=PERIOD_DAYS[period])
    subject, body = render(build(session, start, end))
    return {"subject": subject, "html": body, "period_start": start, "period_end": end}


# ---------- sending

def default_recipients(session: Session) -> list[str]:
    """REPORT_RECIPIENTS only. Not "every user": without a login anyone can create a user with any
    email address, so being a user proves nothing (and the report holds the company's payables)."""
    return [r.strip() for r in settings.report_recipients.split(",") if r.strip()]


def allowed_recipients(session: Session) -> list[str]:
    """Who a report may be sent to: REPORT_RECIPIENTS, set by whoever runs the server."""
    return default_recipients(session)


def default_sender(session: Session) -> User | None:
    if settings.report_sender_user_id:
        u = session.get(User, settings.report_sender_user_id)
        if u is not None and u.gmail_account_id:
            return u
    return session.scalars(select(User).where(User.gmail_account_id.is_not(None)).order_by(User.id)).first()


def _send(sender: User, recipients: list[str], subject: str, body: str) -> None:
    from app import gmail
    if not recipients:
        raise ReportError("No one to send the report to: set REPORT_RECIPIENTS")
    try:
        gmail.send_email(sender, recipients, subject, body)
    except Exception as e:
        raise ReportError(f"Gmail ({sender.email}) couldn't send it: {e}") from e


def send(session: Session, start: datetime, end: datetime, *, sender: User | None = None,
         recipients: list[str] | None = None, sent_by: str = "schedule", slot: str | None = None) -> ReportRun:
    """Build and email the report; the attempt is recorded in report_runs either way.
    A scheduled report claims its slot first, so it is never sent twice."""
    sender = sender or default_sender(session)
    recipients = recipients or default_recipients(session)
    run = ReportRun(slot=slot, period_start=start, period_end=end, recipients=", ".join(recipients),
                    status="sending", sent_by=sent_by)
    session.add(run)
    session.commit()     # IntegrityError here = this slot was already sent (or is being sent)
    try:
        if sender is None:
            raise ReportError("No Gmail account is connected to send the report from")
        subject, body = render(build(session, start, end))
        _send(sender, recipients, subject, body)
        session.refresh(run)
        if run.status != "sending":   # marked interrupted meanwhile (a very slow send): it did go out
            log.warning("Report run %s finished after it was marked interrupted", run.id)
            run.error = None
        run.status = "sent"
        log.info("Report %s sent to %s from %s", slot or "(by hand)", run.recipients, sender.email)
    except Exception as e:
        run.status, run.error = "failed", str(e)[:1000]
        run.slot = None if slot is None else f"{slot}#failed-{run.id}"   # frees the slot for a retry
        log.error("Report %s not sent: %s", slot or "(by hand)", e)
    session.commit()
    return run


def serialize_run(r: ReportRun) -> dict:
    return {"id": r.id, "slot": r.slot, "period_start": r.period_start, "period_end": r.period_end,
            "recipients": r.recipients, "status": r.status, "error": r.error, "sent_by": r.sent_by,
            "created_at": r.created_at}


def recent_runs(session: Session, limit: int = 20) -> list[dict]:
    return [serialize_run(r) for r in session.scalars(select(ReportRun).order_by(ReportRun.id.desc()).limit(limit))]


_warned_no_recipients = False


def run_due(session: Session, now: datetime | None = None) -> ReportRun | None:
    """Send the scheduled report if one is due and not sent yet."""
    now = now or _now()
    due = due_slot(now)
    if due is None:
        return None
    slot, start, end = due
    if session.scalar(select(ReportRun.id).where(ReportRun.slot == slot)) is not None:
        return None
    if not default_recipients(session):
        global _warned_no_recipients
        if not _warned_no_recipients:
            log.warning("Scheduled report not sent: set REPORT_RECIPIENTS (who may receive it)")
            _warned_no_recipients = True
        return None
    last_failure = session.scalar(select(ReportRun.created_at).where(ReportRun.slot.like(f"{slot}#failed-%"))
                                  .order_by(ReportRun.id.desc()).limit(1))
    if last_failure is not None and _now() - last_failure < timedelta(minutes=RETRY_AFTER_MINUTES):
        return None
    try:
        return send(session, start, end, slot=slot)
    except IntegrityError:       # another process claimed the slot a moment earlier
        session.rollback()
        return None


def _clear_stuck(session: Session) -> None:
    """A report left 'sending' by a crash: mark it failed so its slot can be retried."""
    stuck = list(session.scalars(select(ReportRun).where(ReportRun.status == "sending",
                                                        ReportRun.created_at < _now() - timedelta(minutes=STUCK_AFTER_MINUTES))))
    for r in stuck:
        session.execute(update(ReportRun).where(ReportRun.id == r.id).values(
            status="failed", error="Interrupted (server stopped while sending)",
            slot=None if r.slot is None else f"{r.slot}#failed-{r.id}"))
    if stuck:
        session.commit()


def run(stop: threading.Event) -> None:
    """Background loop (started with the API when REPORT_SCHEDULE is set)."""
    log.info("Report scheduler: %s at %s%s; next report %s", settings.report_schedule, settings.report_time,
             f" on {settings.report_weekday}" if settings.report_schedule == "weekly" else "", next_run())
    while not stop.is_set():
        try:
            with SessionLocal() as session:
                _clear_stuck(session)
                run_due(session)
        except Exception:
            log.exception("Report scheduler check failed (will try again)")
        stop.wait(CHECK_EVERY_SECONDS)
