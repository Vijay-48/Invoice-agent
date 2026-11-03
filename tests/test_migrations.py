"""Database migrations (app/migrate.py, migrations/): they build exactly the tables in app/models.py."""
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from app import migrate
from app.db import Base, engine


def empty_schema():
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))


def differences() -> list:
    with engine.connect() as conn:
        return compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)


def version() -> str:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head() -> str:
    return ScriptDirectory.from_config(migrate.config()).get_current_head()


def test_migrations_build_the_models_from_an_empty_database():
    empty_schema()
    migrate.upgrade()
    assert differences() == [], "app/models.py changed: add a migration (alembic revision --autogenerate)"
    assert version() == head()
    migrate.upgrade()              # running again changes nothing
    assert version() == head()


def test_a_database_made_before_migrations_is_stamped_then_upgraded():
    """Up to 2026-10-30 create_all made the tables of that day (= the baseline) and no alembic_version."""
    from alembic import command
    empty_schema()
    with engine.begin() as conn:
        command.upgrade(migrate.config(conn), migrate.BASELINE)
        conn.execute(text("DROP TABLE alembic_version"))
    assert "alembic_version" not in inspect(engine).get_table_names()
    assert "suppliers" not in inspect(engine).get_table_names()
    migrate.upgrade()
    assert version() == head() and differences() == []


def test_an_empty_database_is_not_stamped():
    """Stamping is only for databases that already have the tables: an empty one gets every migration."""
    empty_schema()
    migrate.upgrade()
    assert "invoices" in inspect(engine).get_table_names()
