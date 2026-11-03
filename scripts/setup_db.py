"""One-time local setup, no Docker: creates the `ap` role, the `ap` and `ap_test`
databases, a .env file if missing, and the tables (through the migrations, app/migrate.py).

    python -m scripts.setup_db

Asks for the password of the PostgreSQL superuser (`postgres`) that you chose when
installing PostgreSQL. Set PGUSER / PGPASSWORD / PGHOST / PGPORT to skip the prompt.
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

    from app import migrate
    migrate.upgrade()
    print("Tables ready")
    print("\nDone. Start the API with:  uvicorn app.api:app --reload")


if __name__ == "__main__":
    main()
