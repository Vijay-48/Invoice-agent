from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from app.config import settings

# Connections are used briefly (never while waiting for the AI, Gmail or a streamed answer), so a
# small pool serves many users; sized by DB_POOL_SIZE / DB_MAX_OVERFLOW
engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=settings.db_pool_size,
                       max_overflow=settings.db_max_overflow, pool_timeout=settings.db_pool_timeout)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_session():
    """FastAPI dependency."""
    with SessionLocal() as session:
        yield session
