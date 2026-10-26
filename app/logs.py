"""Logging for every process (API, worker, Gmail listener).

LOG_LEVEL=INFO (default) shows each invoice's journey; LOG_LEVEL=DEBUG adds every
check result, every Composio call and its timing. Library chatter (HTTP requests,
Composio telemetry) is kept at WARNING: those lines also contain signed download URLs.
"""
import logging
import time
from contextlib import contextmanager

from app.config import settings

FORMAT = "%(asctime)s %(levelname)-7s %(name)-14s %(message)s"
QUIET = ["httpx", "httpx2", "httpcore", "httpcore2", "urllib3", "websocket", "composio", "agno", "openai"]


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    if not any(getattr(h, "_ap_handler", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(FORMAT, datefmt="%H:%M:%S"))
        handler._ap_handler = True
        root.addHandler(handler)
    root.setLevel(level)
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.DEBUG if settings.log_level.upper() == "TRACE" else logging.WARNING)


@contextmanager
def timed(log: logging.Logger, what: str, level: int = logging.DEBUG):
    """Log how long a step took: `with timed(log, "extract invoice 7"): ...`"""
    start = time.monotonic()
    try:
        yield
    except Exception:
        log.log(level, "%s failed after %.2fs", what, time.monotonic() - start)
        raise
    log.log(level, "%s took %.2fs", what, time.monotonic() - start)
