"""Receives new-email events from Composio and ingests the attachments.

Started automatically by the API (app/background.py) when COMPOSIO_API_KEY is set.
Can also run on its own: python -m app.gmail_listener

Uses Composio's websocket subscription, so no public URL is needed. On start, and
every SYNC_EVERY_MINUTES, it also fetches recent emails for every connected inbox,
so emails that arrived while it was stopped (or whose event failed) are not missed.
Duplicates are skipped (same email + file name, or same file contents).
"""
import logging
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.logs import setup_logging
from app.config import settings
from app.gmail import (TRIGGER, _composio, handle_event, is_network_error, reconcile_user, sync_user,
                       update_labels, _message_time)
from app import gmail_health
from app.models import User

log = logging.getLogger("gmail_listener")
SYNC_EVERY_MINUTES = 5           # safety net if live events stop arriving
RETRY_SYNC_SECONDS = 60          # after a sync that failed on the network
LABEL_EVERY_SECONDS = 60         # Gmail labels follow invoice status changes within a minute
RECONCILE_EVERY_HOURS = 24       # daily check of Gmail against the email log
FIRST_RECONCILE_AFTER_MINUTES = 60
RECONNECT_DELAYS = (10, 120)     # first and longest wait between reconnect attempts (seconds)
NOT_CONNECTED_LIMIT_SECONDS = 90  # realtime connection not "connected" this long -> start a new one
REFRESH_EVERY_MINUTES = 30       # a fresh connection regularly, in case one goes silently dead
HEALTH_EVERY_MINUTES = 10        # is each user's Gmail access still valid (not expired/revoked)?
_sync_now = threading.Event()    # set to start a sync immediately (e.g. after reconnecting)

# Visible through GET /api/v1/gmail/status
status = {"running": False, "mode": None, "connected": False, "connection_state": None, "connected_since": None,
          "last_event_at": None, "last_sync_at": None, "last_sync_ok": None, "reconnects": 0,
          "last_error": None}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connection_state(subscription) -> str:
    """The realtime (Pusher) connection's own state: connected / connecting / failed /
    disconnected. 'connected' when it can't be read (e.g. in tests)."""
    conn = getattr(subscription, "_connection", None)
    return str(getattr(conn, "state", "connected") or "connected")


def webhook_mode() -> bool:
    return settings.gmail_delivery.strip().lower() == "webhook"


def request_sync(reason: str) -> None:
    log.info("Syncing now: %s", reason)
    _sync_now.set()


_recent_events: "OrderedDict[str, None]" = OrderedDict()
_recent_events_lock = threading.Lock()
RECENT_EVENTS_KEPT = 1000


def _first_delivery(event: dict) -> bool:
    """Composio's realtime client can deliver one event twice (it re-attaches its handler when it
    reconnects by itself); skip a repeat of an email already handed over. Keyed by the Gmail
    message id: the event's own "id" is the trigger id, the same for every email."""
    payload = event.get("payload") or {}
    message_id = payload.get("id") or payload.get("message_id") or payload.get("messageId")
    if not message_id:
        return True
    key = f"{event.get('user_id')}/{event.get('trigger_slug')}/{message_id}"
    with _recent_events_lock:
        if key in _recent_events:
            return False
        _recent_events[key] = None
        while len(_recent_events) > RECENT_EVENTS_KEPT:
            _recent_events.popitem(last=False)
    return True


_events: "queue.Queue[dict]" = queue.Queue()


def receive(event: dict) -> None:
    """Called on the realtime connection's own thread: only queue the event, so downloads
    and the AI queueing don't block it (a blocked connection misses its pings and drops)."""
    status["last_event_at"] = _now()
    _events.put(event)


def _handle_events(stop: threading.Event) -> None:
    """One of GMAIL_CONCURRENCY threads handling queued events (emails of different users or the
    same user in parallel; each email is still handled once, see gmail._one_at_a_time)."""
    while not stop.is_set():
        try:
            event = _events.get(timeout=1)
        except queue.Empty:
            continue
        try:
            on_event(event)
        except Exception:
            log.exception("Gmail event handling failed")


def on_event(event: dict) -> None:
    status["last_event_at"] = _now()
    if not _first_delivery(event):
        log.info("Repeat delivery of email %s ignored", (event.get("payload") or {}).get("id"))
        return
    with SessionLocal() as session:
        try:
            ids = handle_event(session, event)
            if ids:
                log.info("Queued invoice(s) %s", ids)
        except Exception as e:
            session.rollback()
            if is_network_error(e):
                log.warning("Gmail event not handled (%s); syncing again shortly", e)
            else:
                log.exception("Failed to handle Gmail event; the periodic sync will retry it")
            _rewind(session, event)
            if is_network_error(e):
                request_sync("a new email couldn't be fetched")   # retried every minute until it works


def _rewind(session, event: dict) -> None:
    """Move the user's sync point back before the failed email so sync_user() fetches it again."""
    user_id = (event.get("user_id") or "").removeprefix("ap-user-")
    seen = _message_time(event.get("payload") or {})
    if not (user_id.isdigit() and seen):
        return
    user = session.get(User, int(user_id))
    if user and (not user.gmail_last_sync_at or user.gmail_last_sync_at >= seen):
        user.gmail_last_sync_at = seen - timedelta(minutes=1)
        session.commit()


def _sync_one(user_id: int) -> bool:
    """Sync one inbox in its own database session. False if the network got in the way."""
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None or not user.gmail_account_id:
            return True
        if gmail_health.needs_reconnect(session, user):
            log.debug("Gmail sync for user %s skipped: needs to be reconnected", user.id)
            return True
        try:
            sync_user(session, user)
            return True
        except Exception as e:
            session.rollback()
            if is_network_error(e):   # internet down: retried in RETRY_SYNC_SECONDS
                log.warning("Gmail sync for user %s skipped: %s", user.id, e)
                return False
            log.exception("Gmail sync failed for user %s", user.id)
            if gmail_health.looks_like_auth_error(e):   # access gone? find out now, not in 10 min
                gmail_health.check(session, user)
            return True


def sync_all() -> bool:
    """Sync every connected inbox, GMAIL_CONCURRENCY at a time (one slow inbox doesn't hold up
    the others). Returns False if the network got in the way."""
    with SessionLocal() as session:
        user_ids = list(session.scalars(select(User.id).where(User.gmail_account_id.is_not(None)).order_by(User.id)))
    log.info("Gmail sync for %d connected inbox(es)", len(user_ids))
    if len(user_ids) <= 1:
        return all(_sync_one(u) for u in user_ids)
    with ThreadPoolExecutor(max_workers=max(1, settings.gmail_concurrency), thread_name_prefix="gmail-sync") as pool:
        return all(list(pool.map(_sync_one, user_ids)))


def _sync_safely() -> bool:
    try:
        ok = sync_all()
    except Exception as e:
        if is_network_error(e):
            log.warning("Gmail sync skipped: %s", e)
        else:
            log.exception("Gmail sync failed (database down?)")
        status["last_error"] = f"sync: {type(e).__name__}: {e}"[:300]
        ok = False
    status["last_sync_at"], status["last_sync_ok"] = _now(), bool(ok)
    return bool(ok)


def _wait(stop: threading.Event, seconds: float) -> None:
    """Sleep until the next sync is due, a sync is requested, or we are stopping."""
    end = time.monotonic() + seconds
    while not stop.is_set() and time.monotonic() < end:
        if _sync_now.wait(min(1.0, end - time.monotonic())):
            return


def _for_each_inbox(what: str, fn) -> None:
    """Run fn(session, user) for every connected inbox; errors are logged, never raised."""
    try:
        with SessionLocal() as session:
            for user in session.scalars(select(User).where(User.gmail_account_id.is_not(None))).all():
                if gmail_health.needs_reconnect(session, user):
                    continue   # its Gmail access is gone; nothing to do until it is reconnected
                try:
                    fn(session, user)
                except Exception as e:
                    session.rollback()
                    if is_network_error(e):
                        log.warning("%s for user %s skipped: %s", what, user.id, e)
                    else:
                        log.exception("%s failed for user %s", what, user.id)
    except Exception:
        log.exception("%s failed (database down?)", what)


def _for_all_health() -> None:
    try:
        with SessionLocal() as session:
            gmail_health.check_all(session)
    except Exception:
        log.exception("Gmail connection checks failed (database down?)")


def update_all_labels() -> None:
    if settings.gmail_apply_labels:
        _for_each_inbox("Gmail labels", update_labels)


def reconcile_all() -> None:
    _for_each_inbox("Daily Gmail check", lambda session, user: reconcile_user(session, user))


def _periodic_sync(stop: threading.Event) -> None:
    """Sync on start, every SYNC_EVERY_MINUTES, right away when requested (reconnect), and
    every RETRY_SYNC_SECONDS while the network keeps failing. In between: update Gmail
    labels every LABEL_EVERY_SECONDS, and check the last days against the email log daily."""
    next_sync = 0.0
    next_health = 0.0
    next_reconcile = time.monotonic() + FIRST_RECONCILE_AFTER_MINUTES * 60
    while not stop.is_set():
        try:   # this loop must never die: it is the safety net for missed events
            now = time.monotonic()
            if now >= next_sync or _sync_now.is_set():
                _sync_now.clear()
                ok = _sync_safely()
                if not ok:
                    log.info("Gmail sync will retry in %ss", RETRY_SYNC_SECONDS)
                next_sync = time.monotonic() + (SYNC_EVERY_MINUTES * 60 if ok else RETRY_SYNC_SECONDS)
            if time.monotonic() >= next_health:
                _for_all_health()
                next_health = time.monotonic() + HEALTH_EVERY_MINUTES * 60
            if time.monotonic() >= next_reconcile:
                reconcile_all()
                next_reconcile = time.monotonic() + RECONCILE_EVERY_HOURS * 3600
            update_all_labels()
        except Exception:
            log.exception("Gmail background loop error; continuing")
            next_sync = time.monotonic() + RETRY_SYNC_SECONDS
        _wait(stop, max(0.0, min(LABEL_EVERY_SECONDS, next_sync - time.monotonic())))


def run(stop: threading.Event) -> None:
    """Listen until `stop` is set. Runs inside the API process (app/background.py)
    or on its own via `python -m app.gmail_listener`."""
    threading.Thread(target=_periodic_sync, args=(stop,), name="gmail-sync", daemon=True).start()
    for i in range(max(1, settings.gmail_concurrency)):
        threading.Thread(target=_handle_events, args=(stop,), name=f"gmail-events-{i + 1}", daemon=True).start()
    status["running"] = True
    status["mode"] = "webhook" if webhook_mode() else "websocket"
    if webhook_mode():
        # Events arrive at POST /webhooks/composio (app/api.py) and are queued with receive();
        # the safety sync, labels and daily check still run here
        log.info("Gmail events by webhook at %s/webhooks/composio", settings.public_base_url.rstrip("/"))
        status.update(connected=True, connected_since=_now(), connection_state="webhook")
        stop.wait()
        status["running"] = False
        return
    first_delay, max_delay = RECONNECT_DELAYS
    delay = first_delay   # grows while offline (10s -> 2 min), back to 10s once connected
    connected_before = False
    while not stop.is_set():
        subscription = None
        reason = None
        try:
            subscription = _composio().triggers.subscribe()
            subscription.handle(trigger_slug=TRIGGER)(receive)
            log.info("Listening for %s events", TRIGGER)
            status.update(connected=True, connected_since=_now(), connection_state="connected")
            if connected_before:
                # Events sent while we were disconnected are lost: fetch those emails now
                request_sync("reconnected to Composio")
            connected_before = True
            started = time.monotonic()
            reason = _watch(subscription, stop)
            if time.monotonic() - started > 60:
                delay = first_delay   # it was connected for a while: a fresh problem, retry soon
            if reason == "refresh":
                log.info("Refreshing the Composio connection (every %s min)", REFRESH_EVERY_MINUTES)
            elif reason and not stop.is_set():
                log.warning("Composio connection %s; reconnecting in %ss", reason, delay)
        except Exception as e:
            status["last_error"] = f"listener: {type(e).__name__}: {e}"[:300]
            if is_network_error(e):
                log.warning("Can't reach Composio (%s); retrying in %ss", type(e).__name__, delay)
            else:
                log.exception("Gmail listener error; retrying in %ss", delay)
        finally:
            status.update(connected=False, connection_state="disconnected")
            if subscription is not None:
                subscription.stop()
        if stop.is_set():
            break
        status["reconnects"] += 1
        if reason == "refresh":
            continue   # planned refresh: reconnect at once
        stop.wait(delay)
        delay = min(delay * 2, max_delay)
    status["running"] = False
    log.info("Gmail listener stopped")


def _watch(subscription, stop: threading.Event) -> str | None:
    """Wait while the connection is healthy. Returns why it should be replaced (None = stopping).
    Besides an outright drop, catches a connection stuck reconnecting (state not 'connected'
    for NOT_CONNECTED_LIMIT_SECONDS) and refreshes it every REFRESH_EVERY_MINUTES, since a
    connection can also go silently dead while still reporting 'connected'."""
    started = time.monotonic()
    not_connected_since = None
    while not stop.is_set():
        if not subscription.is_alive() or subscription.has_errored():
            return "dropped (internet?)"
        state = _connection_state(subscription)
        status["connection_state"] = state
        if state == "connected":
            not_connected_since = None
        else:
            not_connected_since = not_connected_since or time.monotonic()
            if time.monotonic() - not_connected_since > NOT_CONNECTED_LIMIT_SECONDS:
                return f"stuck in state '{state}' for over {NOT_CONNECTED_LIMIT_SECONDS}s"
        if time.monotonic() - started > REFRESH_EVERY_MINUTES * 60:
            return "refresh"
        stop.wait(1)
    return None


def main():
    setup_logging()
    Base.metadata.create_all(engine)
    stop = threading.Event()
    try:
        run(stop)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
