"""Bring the database up to date with the migrations in migrations/versions (Alembic).

Called on start by the API, the worker and the Gmail listener (MIGRATE_ON_START=true, the default).
A database made before migrations existed (by create_all, up to 2026-10-30) has every table but no
alembic_version: it is recorded as being at the baseline, then upgraded. A Postgres advisory lock
makes processes that start together migrate one at a time.

    python -m app.migrate      the same, by hand (e.g. before starting with MIGRATE_ON_START=false)
"""
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.config import settings
from app.db import engine as default_engine

log = logging.getLogger("migrate")
ROOT = Path(__file__).resolve().parent.parent
BASELINE = "0001"
LOCK_KEY = 726_351_001          # any constant: the advisory lock that serialises migrations


def config(connection=None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["connection"] = connection
    return cfg


def upgrade(engine: Engine = default_engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
        tables = set(inspect(conn).get_table_names())
        cfg = config(conn)
        if "invoices" in tables and "alembic_version" not in tables:
            log.info("Database was made before migrations: recording it at the baseline (%s)", BASELINE)
            command.stamp(cfg, BASELINE)
        command.upgrade(cfg, "head")
    log.info("Database is up to date")


def upgrade_on_start() -> None:
    if settings.migrate_on_start:
        upgrade()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    upgrade()
