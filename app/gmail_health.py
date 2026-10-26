"""Gmail connection health: notice when a user's Gmail access has expired or been revoked.

Google access can end (password changed, access removed in the Google account, token
expired). Composio then marks the connected account EXPIRED/REVOKED/..., every sync fails,
and without this nobody would notice. The status is checked regularly and right after a sync
fails; a broken inbox is shown to its user ("Reconnect Gmail"), listed in the email report,
skipped by the sync until it is reconnected, and (optionally) its user gets an email.
"""
import html
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import GmailHealth, User

log = logging.getLogger("gmail_health")
OK, RECONNECT = "ok", "reconnect"
# Composio connected-account statuses that need the user to connect again
BROKEN_STATUSES = {"EXPIRED", "REVOKED", "INACTIVE", "FAILED", "DELETED", "DISABLED"}
# Errors from a Gmail call that suggest the access itself is gone (then the status is checked)
AUTH_ERROR = re.compile(r"\b401\b|\b403\b|unauthori[sz]ed|invalid_grant|token.*(expired|revoked)|"
                        r"(expired|revoked|inactive|disabled).*(connect|account|credential)|"
                        r"connected account.*(not found|not active)|reconnect|re-?authori[sz]e", re.IGNORECASE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def looks_like_auth_error(e: BaseException) -> bool:
    return bool(AUTH_ERROR.search(f"{type(e).__name__}: {e}"))


def health_of(session: Session, user: User) -> GmailHealth | None:
    return session.get(GmailHealth, user.id)


def needs_reconnect(session: Session, user: User) -> bool:
    h = health_of(session, user)
    return h is not None and h.status == RECONNECT


def serialize(h: GmailHealth | None) -> dict:
    if h is None:
        return {"status": OK, "detail": None, "problem_since": None, "checked_at": None}
    return {"status": h.status, "detail": h.detail, "problem_since": h.problem_since, "checked_at": h.checked_at}


def record(session: Session, user: User, status: str, detail: str | None = None) -> GmailHealth:
    """Store the result of a check; logs (and alerts) only when the status changes."""
    h = health_of(session, user) or GmailHealth(user_id=user.id, status=OK)
    session.add(h)
    was = h.status
    h.status, h.detail, h.checked_at = status, (detail or None) and detail[:1000], _now()
    if status == RECONNECT and was != RECONNECT:
        h.problem_since, h.alerted_at = _now(), None
        log.warning("Gmail for user %s (%s) needs to be reconnected: %s", user.id, user.email, detail)
    elif status == OK and was == RECONNECT:
        log.info("Gmail for user %s works again", user.id)
        h.problem_since, h.alerted_at = None, None
    session.commit()
    if h.status == RECONNECT and h.alerted_at is None:
        _alert(session, user, h)
    return h


def check(session: Session, user: User) -> GmailHealth | None:
    """Ask Composio about the user's connection. Returns None (nothing recorded) when it can't
    be reached: being offline says nothing about the Gmail access."""
    from app import gmail   # gmail imports this module
    if not user.gmail_account_id:
        return None
    try:
        account = gmail._with_retries("Gmail connection check",
                                      lambda: gmail._composio().connected_accounts.get(user.gmail_account_id))
    except Exception as e:
        if gmail.is_network_error(e):
            log.info("Gmail connection check for user %s skipped (offline): %s", user.id, e)
            return None
        if re.search(r"\b404\b|not found", str(e), re.IGNORECASE):
            return record(session, user, RECONNECT, "The Gmail connection no longer exists at Composio")
        log.warning("Gmail connection check for user %s failed: %s", user.id, e)
        return None
    status = str(getattr(account, "status", "") or "").upper()
    if status in BROKEN_STATUSES:
        return record(session, user, RECONNECT, f"Gmail access is {status.lower()}: connect Gmail again")
    return record(session, user, OK, status or None)


def check_all(session: Session) -> None:
    for user in session.scalars(select(User).where(User.gmail_account_id.is_not(None)).order_by(User.id)).all():
        try:
            check(session, user)
        except Exception:
            session.rollback()
            log.exception("Gmail connection check failed for user %s", user.id)


def _alert(session: Session, user: User, h: GmailHealth) -> None:
    """Email the user once per problem (GMAIL_HEALTH_ALERTS=true), from another inbox that still
    works: their own Gmail can't send it."""
    if not settings.gmail_health_alerts:
        return
    from app import gmail
    broken = {r.user_id for r in session.scalars(select(GmailHealth).where(GmailHealth.status == RECONNECT))}
    sender = session.scalars(select(User).where(User.gmail_account_id.is_not(None), User.id.not_in(broken))
                             .order_by(User.id)).first()
    if sender is None:
        log.warning("No working Gmail to send the reconnect alert for user %s from", user.id)
        return
    link = settings.report_app_url.rstrip("/")
    body = (f"<p>Hello {html.escape(user.name)},</p><p>The invoice app can no longer read your Gmail "
            f"({html.escape(user.email)}): {html.escape(h.detail or 'access ended')}.</p>"
            "<p>Invoices emailed to you are not being picked up until you connect Gmail again"
            + (f' on the <a href="{html.escape(link)}/#/connections">Gmail screen</a>' if link else
               " (Gmail screen of the app)") + ". Emails that arrive meanwhile are fetched once you do.</p>")
    try:
        gmail.send_email(sender, [user.email], "Action needed: reconnect your Gmail to the invoice app", body)
        h.alerted_at = _now()
        session.commit()
        log.info("Reconnect alert for user %s sent from %s", user.id, sender.email)
    except Exception as e:
        session.rollback()
        log.warning("Reconnect alert for user %s not sent: %s", user.id, e)
