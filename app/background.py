"""Runs the worker and the Gmail listener inside the API process, so one command
(`uvicorn app.api:app`) starts everything. Started and stopped by the API's lifespan.

Set RUN_WORKER=false / RUN_GMAIL_LISTENER=false to run them as separate processes
instead (python -m app.worker / python -m app.gmail_listener).
"""
import logging
import threading
import time

from app import worker, gmail_listener, reports, followup
from app.config import settings

log = logging.getLogger("background")
SHUTDOWN_WAIT_SECONDS = 30   # lets the worker finish the invoice it is reading


class Background:
    def __init__(self):
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        log.info("Starting background jobs: worker=%s, gmail listener=%s",
                 settings.run_worker, settings.run_gmail_listener and bool(settings.composio_api_key))
        if settings.run_worker:
            # Several documents read at once (each waits mostly for OpenAI); the queue (SKIP LOCKED)
            # gives every document to exactly one of them
            for i in range(max(1, settings.worker_concurrency)):
                self._spawn("worker" if i == 0 else f"worker-{i + 1}", worker.run)
        elif settings.run_excel_export:
            # Documents are read by worker processes elsewhere; the workbook is written here only
            self._spawn("excel-export", worker.export_loop)
        if settings.run_gmail_listener:
            if settings.composio_api_key:
                self._spawn("gmail-listener", gmail_listener.run)
            else:
                log.info("COMPOSIO_API_KEY not set: Gmail listener not started (uploads still work)")
        if settings.report_schedule.strip().lower() in reports.SCHEDULES:
            self._spawn("reports", reports.run)
        if settings.followup_enabled:
            self._spawn("followup", followup.run)

    def _spawn(self, name: str, target) -> None:
        t = threading.Thread(target=target, args=(self.stop,), name=name, daemon=True)
        t.start()
        self.threads.append(t)

    def shutdown(self) -> None:
        log.info("Stopping background jobs")
        self.stop.set()
        deadline = time.monotonic() + SHUTDOWN_WAIT_SECONDS
        for t in self.threads:
            t.join(max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                # Daemon thread dies with the process; an invoice it was reading goes
                # back to the queue after PROCESSING_TIMEOUT_MINUTES
                log.warning("%s did not stop within %ss", t.name, SHUTDOWN_WAIT_SECONDS)
