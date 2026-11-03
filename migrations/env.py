"""Alembic environment: the tables are app/models.py, the database is DATABASE_URL.

app/migrate.py passes its own connection (config.attributes["connection"]), so the upgrade on start
runs inside the transaction that holds the migration lock. From the command line a new connection is made.
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

import app.models  # noqa: F401  (registers the tables)
from app.config import settings
from app.db import Base

config = context.config
if config.config_file_name and not config.attributes.get("connection"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
elif (connection := config.attributes.get("connection")) is not None:
    run(connection)
else:
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        run(connection)
    engine.dispose()
