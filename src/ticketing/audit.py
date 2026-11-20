"""Configuration change log (F12). Ticket activity goes to ticket_event, not here."""
from datetime import date, datetime

from sqlalchemy.orm import Session

from . import clock
from .models import AuditLog

SECRET_FIELDS = {"password_hash", "secret_hash", "token_hash"}


def _plain(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def snapshot(obj, fields: list[str] | None = None) -> dict:
    if obj is None:
        return None
    cols = fields or [c.key for c in obj.__table__.columns]
    return {c: _plain(getattr(obj, c)) for c in cols if c not in SECRET_FIELDS}


def record(db: Session, actor_id: int | None, action: str, entity: str, entity_id: int | None,
           old: dict | None = None, new: dict | None = None, organization_id: int | None = None,
           project_id: int | None = None) -> None:
    if old is not None and new is not None:  # keep only what changed
        keys = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
        if not keys:
            return
        old = {k: old.get(k) for k in keys}
        new = {k: new.get(k) for k in keys}
    db.add(AuditLog(actor_id=actor_id or None, action=action, entity=entity, entity_id=entity_id,
                    organization_id=organization_id, project_id=project_id, old_value=old, new_value=new,
                    created_at=clock.now()))
