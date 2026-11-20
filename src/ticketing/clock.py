"""Injectable clock. All business code calls `now()`; tests move time with `set_now`/`advance`."""
from datetime import datetime, timedelta, timezone

_override: datetime | None = None


def now() -> datetime:
    return _override if _override is not None else datetime.now(timezone.utc)


def set_now(value: datetime | None) -> None:
    global _override
    if value is not None and value.tzinfo is None:
        raise ValueError("clock value must be timezone-aware")
    _override = value


def advance(**kwargs) -> datetime:
    global _override
    _override = now() + timedelta(**kwargs)
    return _override
