"""One command: the API starts the worker and the Gmail listener and stops them on shutdown."""
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import background, gmail_listener, worker
from app.api import app
from app.config import settings
from app.models import Status
from tests.conftest import make_extracted


class FakeSubscription:
    def __init__(self):
        self.alive, self.handlers, self.stopped = True, [], False

    def handle(self, **filters):
        def register(fn):
            self.handlers.append((filters, fn))
            return fn
        return register

    def is_alive(self):
        return self.alive

    def has_errored(self):
        return False

    def stop(self):
        self.alive, self.stopped = False, True


@pytest.fixture
def run_background(monkeypatch):
    monkeypatch.setattr(settings, "run_worker", True)
    monkeypatch.setattr(settings, "run_gmail_listener", True)
    monkeypatch.setattr(worker, "extract_invoice", lambda path, name=None, **kw: make_extracted())
    monkeypatch.setattr(worker, "page_count", lambda path: 1)
    monkeypatch.setattr(gmail_listener, "sync_all", lambda: None)
    subscription = FakeSubscription()
    composio = SimpleNamespace(triggers=SimpleNamespace(subscribe=lambda: subscription))
    monkeypatch.setattr(gmail_listener, "_composio", lambda: composio)
    return subscription


def wait_for(fn, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.2)
    return False


def test_api_runs_worker_and_listener(users, run_background):
    with TestClient(app) as client:
        names = {t.name for t in threading.enumerate()}
        assert {"worker", "gmail-listener"} <= names
        assert wait_for(lambda: run_background.handlers)          # subscribed to Gmail events
        assert run_background.handlers[0][0] == {"trigger_slug": "GMAIL_NEW_GMAIL_MESSAGE"}

        r = client.post("/api/v1/invoices/upload", files={"file": ("a.pdf", b"%PDF x")},
                        headers={"X-User-Id": str(users["priya"].id)})
        inv_id = r.json()["invoice_id"]
        # No separate worker process: the upload is processed and written to Excel
        assert wait_for(lambda: client.get(f"/api/v1/invoices/{inv_id}").json()["status"] == Status.EXPORTED)

    # Leaving the block = server shutdown: both threads stop and Composio is disconnected
    assert wait_for(lambda: not any(t.name.startswith(("worker", "gmail-listener")) for t in threading.enumerate()), 10)
    assert run_background.stopped


def test_listener_not_started_without_composio_key(run_background, monkeypatch):
    monkeypatch.setattr(settings, "composio_api_key", "")
    bg = background.Background()
    bg.start()
    try:
        names = [t.name for t in bg.threads]
        assert names == ["worker"] + [f"worker-{i}" for i in range(2, settings.worker_concurrency + 1)]
    finally:
        bg.shutdown()


def test_worker_survives_database_errors(monkeypatch):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("connection refused")
        return None

    monkeypatch.setattr(worker, "claim_next", flaky)
    monkeypatch.setattr(worker, "export", lambda: None)
    monkeypatch.setattr(worker.threading.Event, "wait", lambda self, timeout=None: self.is_set())
    stop = threading.Event()
    t = threading.Thread(target=worker.run, args=(stop,))
    t.start()
    assert wait_for(lambda: len(calls) >= 3, 5)       # kept going after the error
    stop.set()
    t.join(5)
    assert not t.is_alive()


def test_only_the_api_process_writes_excel(monkeypatch):
    """Workers on other PCs read documents but never write their own local workbook; with its
    workers elsewhere, the API process runs an export loop instead."""
    from app import background
    called = []
    monkeypatch.setattr(worker, "export", lambda: called.append(1))
    monkeypatch.setattr(worker, "claim_next", lambda: None)
    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda t=None: stop.set())
    worker.run(stop, write_excel=False)
    assert called == []
    monkeypatch.setattr(settings, "run_worker", False)
    monkeypatch.setattr(settings, "run_gmail_listener", False)
    monkeypatch.setattr(settings, "run_excel_export", True)
    bg = background.Background()
    bg.start()
    try:
        assert [t.name for t in bg.threads] == ["excel-export"]
    finally:
        bg.shutdown()
