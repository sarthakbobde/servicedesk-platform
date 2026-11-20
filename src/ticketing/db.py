from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_config


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(get_config().database_url, pool_size=20, max_overflow=20, pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def SessionLocal():
    get_engine()
    return _SessionLocal()


def reset_engine(url: str | None = None):
    """Used by tests to point at another database."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(url or get_config().database_url, pool_size=20, max_overflow=20)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine
