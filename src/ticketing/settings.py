"""Typed settings registry (F11). Global and project scope; a project value beats the global one. Values are read
from the database on every call (hot reload, no restart). Every change is audited by the caller."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import AppError
from .models import Project, Setting


@dataclass(frozen=True)
class Spec:
    key: str
    kind: type  # int | bool | str
    scope: str  # global | project
    default: object
    meaning: str
    min: int | None = None
    max: int | None = None
    choices: tuple | None = None


REGISTRY: dict[str, Spec] = {s.key: s for s in [
    Spec("auth.password_min_length", int, "global", 10, "Minimum password length", 8, 128),
    Spec("auth.lockout_attempts", int, "global", 5, "Failed sign-ins before the account locks", 1, 100),
    Spec("auth.lockout_minutes", int, "global", 15, "Minutes an account stays locked", 1, 1440),
    Spec("auth.session_idle_minutes", int, "global", 30, "Portal session idle timeout", 5, 1440),
    Spec("auth.session_max_hours", int, "global", 12, "Portal session absolute limit", 1, 168),
    Spec("auth.token_minutes", int, "global", 60, "API token lifetime", 5, 1440),
    Spec("ticket.reopen_window_days", int, "project", 7, "How long a finished ticket can be reopened", 0, 365),
    Spec("ticket.auto_close_days", int, "project", 7, "Days after finishing before CLOSED", 0, 365),
    Spec("ticket.requester_noreply_days", int, "project", 7, "Days waiting on the requester before a system reject",
         1, 365),
    Spec("routing.auto_rule", str, "project", "fewest_open", "Selection rule for auto routing",
         choices=("fewest_open",)),
    Spec("routing.retry_minutes", int, "global", 5, "Retry interval when nobody is on shift", 1, 1440),
    Spec("sla.escalation_cycle_seconds", int, "global", 60, "How often escalation is evaluated", 10, 3600),
    Spec("sla.warning_percent", int, "global", 80, "Percent of a step used that counts as at risk", 1, 99),
    Spec("notification.retry_max", int, "global", 5, "Delivery attempts", 1, 20),
    Spec("product_user.api_access", bool, "project", True, "Products may call the API"),
    Spec("product_user.portal_access", bool, "project", False, "Product users may sign in to the portal"),
    Spec("product_user.can_set_priority", bool, "project", False, "Product users may choose priority"),
]}

INTERNAL_KEYS = {"system.worker_last_run"}  # written by the worker, not user settings


def coerce(key: str, value) -> object:
    spec = REGISTRY.get(key)
    if spec is None:
        raise AppError("validation_failed", fields={key: "unknown setting"})
    try:
        if spec.kind is bool:
            if isinstance(value, str):
                if value.lower() in ("true", "on", "1", "yes"):
                    value = True
                elif value.lower() in ("false", "off", "0", "no"):
                    value = False
                else:
                    raise ValueError
            if not isinstance(value, bool):
                raise ValueError
        elif spec.kind is int:
            if isinstance(value, bool):
                raise ValueError
            value = int(value)
            if (spec.min is not None and value < spec.min) or (spec.max is not None and value > spec.max):
                raise AppError("validation_failed", fields={key: f"allowed range {spec.min} to {spec.max}"})
        else:
            value = str(value)
            if spec.choices and value not in spec.choices:
                raise AppError("validation_failed", fields={key: f"one of {', '.join(spec.choices)}"})
    except (TypeError, ValueError):
        raise AppError("validation_failed", fields={key: f"must be {spec.kind.__name__}"})
    return value


def _rows(db: Session, project_id: int | None) -> dict[str, Setting]:
    q = select(Setting).where(Setting.project_id.is_(None) if project_id is None else Setting.project_id == project_id)
    return {r.key: r for r in db.scalars(q)}


def global_values(db: Session) -> dict[str, object]:
    rows = _rows(db, None)
    return {k: (rows[k].value if k in rows else s.default) for k, s in REGISTRY.items()}


def effective(db: Session, project_id: int | None = None) -> dict[str, object]:
    values = global_values(db)
    if project_id is not None:
        for k, r in _rows(db, project_id).items():
            if k in REGISTRY and REGISTRY[k].scope == "project":
                values[k] = r.value
    return values


def get(db: Session, key: str, project_id: int | None = None):
    return effective(db, project_id)[key]


def describe(db: Session, project_id: int | None = None) -> list[dict]:
    """For S29 / S16: default, current value, where it comes from, last change."""
    glob = _rows(db, None)
    proj = _rows(db, project_id) if project_id is not None else {}
    out = []
    for k, s in REGISTRY.items():
        if project_id is not None and s.scope != "project":
            continue
        row = proj.get(k) or glob.get(k)
        value = proj[k].value if k in proj else (glob[k].value if k in glob else s.default)
        out.append({
            "key": k, "meaning": s.meaning, "scope": s.scope, "type": s.kind.__name__, "default": s.default,
            "value": value, "min": s.min, "max": s.max,
            "source": "project" if k in proj else ("global" if k in glob else "default"),
            "updated_by": row.updated_by if row else None,
            "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        })
    return out


def validate_effective(values: dict[str, object]) -> list[str]:
    problems = []
    if values["ticket.auto_close_days"] < values["ticket.reopen_window_days"]:
        problems.append("ticket.auto_close_days must be at least ticket.reopen_window_days (D25)")
    if not values["product_user.api_access"] and not values["product_user.portal_access"]:
        problems.append("at least one product-user channel (API or portal access) must stay on (D6)")
    return problems


def _save(db: Session, key: str, value, project_id: int | None, actor_id: int | None):
    from .models import Setting as S  # local alias for clarity
    from . import clock
    rows = _rows(db, project_id)
    if key in rows:
        rows[key].value = value
        rows[key].updated_by = actor_id
        rows[key].updated_at = clock.now()
    else:
        db.add(S(key=key, value=value, project_id=project_id, updated_by=actor_id, updated_at=clock.now()))


def put_global(db: Session, changes: dict, actor_id: int | None) -> dict[str, tuple]:
    """Validate the whole map, check every project's effective values, then save. Returns {key: (old, new)}."""
    fields, clean = {}, {}
    for k, v in changes.items():
        try:
            clean[k] = coerce(k, v)
        except AppError as e:
            fields.update(e.fields or {k: e.message})
    if fields:
        raise AppError("validation_failed", fields=fields)
    old = global_values(db)
    new_global = {**old, **clean}
    problems = {}
    if p := validate_effective(new_global):
        problems["(global)"] = p
    for pid, pkey in db.execute(select(Project.id, Project.key)):
        vals = dict(new_global)
        for k, r in _rows(db, pid).items():
            if k in REGISTRY and REGISTRY[k].scope == "project":
                vals[k] = r.value
        if p := validate_effective(vals):
            problems[pkey] = p
    if problems:
        raise AppError("validation_failed", "The change would leave effective settings invalid.",
                       fields={k: "; ".join(v) for k, v in problems.items()})
    diff = {}
    for k, v in clean.items():
        if old[k] != v:
            diff[k] = (old[k], v)
            _save(db, k, v, None, actor_id)
    return diff


def put_project(db: Session, project_id: int, changes: dict, actor_id: int | None) -> dict[str, tuple]:
    """Project overrides. A value of None removes the override (inherit)."""
    fields, clean, remove = {}, {}, []
    for k, v in changes.items():
        spec = REGISTRY.get(k)
        if spec is None or spec.scope != "project":
            fields[k] = "not a project setting"
            continue
        if v is None:
            remove.append(k)
            continue
        try:
            clean[k] = coerce(k, v)
        except AppError as e:
            fields.update(e.fields or {k: e.message})
    if fields:
        raise AppError("validation_failed", fields=fields)
    old = effective(db, project_id)
    glob = global_values(db)
    new = dict(old)
    for k in remove:
        new[k] = glob[k]
    new.update(clean)
    if p := validate_effective(new):
        raise AppError("validation_failed", "Effective project settings would be invalid.",
                       fields={"settings": "; ".join(p)})
    rows = _rows(db, project_id)
    diff = {}
    for k in remove:
        if k in rows:
            db.delete(rows[k])
            diff[k] = (old[k], glob[k])
    for k, v in clean.items():
        if k not in rows or rows[k].value != v:
            diff[k] = (old[k], v)
            _save(db, k, v, project_id, actor_id)
    return diff
