"""Decision Engine: plain rules, so every outcome is explainable."""
from app.config import settings
from app.models import Status
from app.pipeline.checks import Check, WARN, FAIL

RULES_VERSION = "2026-10-01.1"   # store with each decision for audit


def decide(checks: list[Check], grand_total: float) -> tuple[str, str]:
    fails = [c for c in checks if c.status == FAIL]
    warns = [c for c in checks if c.status == WARN]
    if fails:
        return Status.EXCEPTION, "; ".join(c.message for c in fails)
    if warns:
        return Status.PENDING_APPROVAL, "; ".join(c.message for c in warns)
    if grand_total > settings.auto_post_limit:
        return Status.PENDING_APPROVAL, f"Amount {grand_total:,.2f} above auto-post limit"
    return Status.AUTO_POST, "All checks passed"
