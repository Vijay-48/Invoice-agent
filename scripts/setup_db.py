"""One-time local setup, no Docker: creates the `ap` role, the `ap` and `ap_test`
databases, a .env file if missing, and the tables.

    python -m scripts.setup_db

Asks for the password of the PostgreSQL superuser (`postgres`) that you chose when
installing PostgreSQL. Set PGUSER / PGPASSWORD / PGHOST / PGPORT to skip the prompt.
Add --reset to delete tables left by the previous version of this app.
"""
import getpass
import os
import shutil
import sys
from pathlib import Path

import psycopg
from psycopg import sql

ROLE, PASSWORD = "ap", "ap"
DATABASES = ["ap", "ap_test"]
HOST = os.environ.get("PGHOST", "localhost")
PORT = os.environ.get("PGPORT", "5432")


def main():
    admin = os.environ.get("PGUSER", "postgres")
    password = os.environ.get("PGPASSWORD") or getpass.getpass(
        f"Password for PostgreSQL superuser '{admin}': ")
    try:
        conn = psycopg.connect(host=HOST, port=PORT, user=admin, password=password,
                               dbname="postgres", autocommit=True)
    except psycopg.OperationalError as e:
        sys.exit(f"Could not connect as '{admin}' on {HOST}:{PORT}.\n{e}\n"
                 "Is PostgreSQL running, and is that the right password?")

    with conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [ROLE]).fetchone():
            print(f"Role '{ROLE}' already exists (password left unchanged)")
        else:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(ROLE), sql.Literal(PASSWORD)))
            print(f"Created role '{ROLE}'")
        for db in DATABASES:
            if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [db]).fetchone():
                print(f"Database '{db}' already exists")
            else:
                conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(db), sql.Identifier(ROLE)))
                print(f"Created database '{db}'")

    try:
        psycopg.connect(host=HOST, port=PORT, user=ROLE, password=PASSWORD, dbname="ap").close()
    except psycopg.OperationalError:
        sys.exit(f"Role '{ROLE}' exists but its password is not '{PASSWORD}'. Either run\n"
                 f"  ALTER ROLE {ROLE} PASSWORD '{PASSWORD}';\n"
                 "in psql, or put the real password in DATABASE_URL in .env.")

    env = Path(".env")
    if not env.exists():
        shutil.copy(".env.example", env)
        print("Created .env from .env.example: put your OPENAI_API_KEY and COMPOSIO_API_KEY in it")

    from sqlalchemy import inspect, text
    from app.db import Base, engine
    import app.models  # noqa: F401  (registers the tables)

    # Tables from the previous version (PO matching, ledgers) have a different shape
    insp = inspect(engine)
    old = "purchase_orders" in insp.get_table_names() or (
        "invoices" in insp.get_table_names()
        and "user_id" not in {c["name"] for c in insp.get_columns("invoices")})
    if old:
        if "--reset" not in sys.argv:
            sys.exit("The 'ap' database has tables from the previous version of this app.\n"
                     "Run  python -m scripts.setup_db --reset  to delete them and start fresh\n"
                     "(this deletes all invoice records in the 'ap' database).")
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        print("Removed the previous version's tables")
    Base.metadata.create_all(engine)
    print("Tables ready")
    print("\nDone. Start the API with:  uvicorn app.api:app --reload")


if __name__ == "__main__":
    main()
