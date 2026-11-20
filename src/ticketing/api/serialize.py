"""JSON shapes (A11.3, A11.5). Times are ISO 8601 UTC with Z."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    ApiCredential, AuditLog, Calendar, Organization, Project, RoleAssignment, Team, User,
)
from ..worktime import fmt_hhmm


def ts(v: datetime | None) -> str | None:
    if v is None:
        return None
    return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def page(items: list, meta: dict) -> dict:
    return {"items": items, **meta}


def organization(o: Organization, projects: list | None = None) -> dict:
    d = {"id": o.id, "name": o.name, "type": o.type, "is_active": o.is_active, "created_at": ts(o.created_at)}
    if projects is not None:
        d["projects"] = [project(p) for p in projects]
    return d


def project(p: Project, counts: dict | None = None) -> dict:
    d = {"id": p.id, "organization_id": p.organization_id, "name": p.name, "key": p.key,
         "key_locked": p.ticket_seq > 0, "is_active": p.is_active, "created_at": ts(p.created_at)}
    if counts is not None:
        d["counts"] = counts
    return d


def team(t: Team, members: list | None = None) -> dict:
    d = {"id": t.id, "project_id": t.project_id, "name": t.name, "is_active": t.is_active}
    if members is not None:
        d["managers"] = [{"assignment_id": ra.id, "user": {"id": u.id, "name": u.name}, "calendar_id": ra.calendar_id}
                         for ra, u in members if ra.role == "TEAM_MANAGER"]
        d["resolvers"] = [{"assignment_id": ra.id, "user": {"id": u.id, "name": u.name}, "calendar_id": ra.calendar_id,
                           "user_active": u.is_active} for ra, u in members if ra.role == "RESOLVER"]
    return d


def user(u: User, assignments: list | None = None, db: Session | None = None) -> dict:
    d = {"id": u.id, "user_type": u.user_type, "name": u.name, "email": u.email,
         "organization_id": u.organization_id, "project_id": u.project_id, "external_id": u.external_id,
         "is_platform_admin": u.is_platform_admin, "is_active": u.is_active,
         "must_change_password": u.must_change_password, "has_portal_account": bool(u.password_hash),
         "locked": bool(u.locked_until and u.locked_until > datetime.now(timezone.utc)),
         "last_login_at": ts(u.last_login_at), "created_at": ts(u.created_at)}
    if assignments is not None:
        d["assignments"] = [assignment(a, db) for a in assignments]
    return d


def user_ref(u: User | None) -> dict | None:
    return {"id": u.id, "name": u.name} if u else None


def assignment(a: RoleAssignment, db: Session | None = None) -> dict:
    d = {"id": a.id, "user_id": a.user_id, "role": a.role, "scope": a.scope, "organization_id": a.organization_id,
         "project_id": a.project_id, "team_id": a.team_id, "calendar_id": a.calendar_id, "is_active": a.is_active,
         "created_at": ts(a.created_at)}
    if db is not None:
        org = db.get(Organization, a.organization_id)
        d["organization"] = {"id": org.id, "name": org.name} if org else None
        if a.project_id:
            p = db.get(Project, a.project_id)
            d["project"] = {"id": p.id, "key": p.key, "name": p.name}
        if a.team_id:
            t = db.get(Team, a.team_id)
            d["team"] = {"id": t.id, "name": t.name}
        if a.calendar_id:
            c = db.get(Calendar, a.calendar_id)
            d["calendar"] = {"id": c.id, "name": c.name, "time_zone": c.time_zone}
    return d


def credential(c: ApiCredential, secret: str | None = None) -> dict:
    d = {"id": c.id, "project_id": c.project_id, "name": c.name, "key_prefix": c.key_prefix,
         "is_active": c.is_active, "last_used_at": ts(c.last_used_at), "created_at": ts(c.created_at)}
    if secret is not None:
        d["secret"] = secret
    return d


def audit_row(a: AuditLog, names: dict | None = None) -> dict:
    return {"id": a.id, "actor": {"id": a.actor_id, "name": (names or {}).get(a.actor_id)} if a.actor_id else "System",
            "action": a.action, "entity": a.entity, "entity_id": a.entity_id, "project_id": a.project_id,
            "organization_id": a.organization_id, "old_value": a.old_value, "new_value": a.new_value,
            "created_at": ts(a.created_at)}


def user_names(db: Session, ids) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return dict(db.execute(select(User.id, User.name).where(User.id.in_(ids))).all())


def hhmm(m):
    return fmt_hhmm(m)
