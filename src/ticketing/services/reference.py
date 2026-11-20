"""Reference data for raise forms and dropdowns (G5) and product integration (G9)."""
from __future__ import annotations

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock, settings
from ..errors import AppError, not_found
from ..models import ApiCredential, Project, Team, TicketType, User
from ..permissions import Principal
from . import routing
from .common import get_or_404


def projects_for(db: Session, p: Principal) -> list[dict]:
    ids = p.raisable_project_ids()
    q = select(Project).where(Project.is_active.is_(True)).order_by(Project.name)
    if ids is not None:
        q = q.where(Project.id.in_(ids or {-1}))
    out = []
    for proj in db.scalars(q):
        choice = True
        if p.is_product_user:
            choice = bool(settings.get(db, "product_user.can_set_priority", proj.id))
        out.append({"id": proj.id, "key": proj.key, "name": proj.name, "portal_priority_choice": choice})
    return out


def ticket_types_for(db: Session, p: Principal, project_id) -> list[dict]:
    proj = get_or_404(db, Project, project_id)
    if not (p.can_raise_in(proj) or p.has_scope_in_project(proj.id)):
        raise not_found()
    staff = not p.is_product_user and (p.has_scope_in_project(proj.id) or p.is_platform_admin)
    rows = db.execute(select(TicketType, Team).join(Team, Team.id == TicketType.team_id).where(
        TicketType.project_id == proj.id, TicketType.is_active.is_(True), Team.is_active.is_(True))
        .order_by(TicketType.name)).all()
    out = []
    for tt, team in rows:
        d = {"id": tt.id, "name": tt.name, "description": tt.description, "team": {"id": team.id, "name": team.name}}
        if staff:
            d["assignment_mode"] = tt.assignment_mode
        out.append(d)
    return out


def team_members(db: Session, p: Principal, team_id) -> list[dict]:
    team = get_or_404(db, Team, team_id)
    if not (p.is_manager(team.id) or p.admin_of_project(team.project_id)):
        if p.is_resolver(team.id):
            raise AppError("forbidden")
        raise not_found()
    people = routing.team_people(db, team.id, roles=("RESOLVER", "TEAM_MANAGER"))
    counts = routing.open_counts(db, [u.id for _, u in people])
    cache: dict = {}
    now = clock.now()
    out = []
    for ra, u in people:
        out.append({"user": {"id": u.id, "name": u.name}, "role": ra.role, "assignment_id": ra.id,
                    "calendar_id": ra.calendar_id, "on_shift": routing.on_shift(db, ra, now, cache),
                    "open_tickets": counts.get(u.id, 0)})
    return out


# ---------------------------------------------------------------- G9 product integration
def product_user_view(db: Session, u: User) -> dict:
    return {"external_id": u.external_id, "user_id": u.id, "name": u.name, "email": u.email,
            "portal_access": bool(u.password_hash) and bool(settings.get(db, "product_user.portal_access",
                                                                          u.project_id)),
            "is_active": u.is_active}


def upsert_product_user(db: Session, cred: ApiCredential, external_id: str, data: dict) -> tuple[User, bool]:
    ext = (external_id or "").strip()
    if not ext or len(ext) > 200:
        raise AppError("validation_failed", fields={"external_id": "1 to 200 characters"})
    fields = {}
    name = data.get("name")
    if name is not None:
        name = str(name).strip()[:200] or None
    email = data.get("email")
    if email not in (None, ""):
        try:
            email = validate_email(str(email).strip(), check_deliverability=False,
                                   test_environment=True).normalized.lower()
        except EmailNotValidError:
            fields["email"] = "not a valid email"
    else:
        email = None if "email" in data else ...
    active = data.get("is_active")
    if active is not None and not isinstance(active, bool):
        fields["is_active"] = "true or false"
    for forbidden in ("password", "role", "roles", "is_platform_admin"):
        if forbidden in data:
            fields[forbidden] = "cannot be set through the API (F9.4)"
    if fields:
        raise AppError("validation_failed", fields=fields)
    user = db.scalar(select(User).where(User.project_id == cred.project_id, User.external_id == ext).with_for_update())
    created = user is None
    if email not in (None, ...):
        q = select(User.id).where(func.lower(User.email) == email)
        if user is not None:
            q = q.where(User.id != user.id)
        if db.scalar(q):
            raise AppError("validation_failed", fields={"email": "already used by another account"})
    if created:
        user = User(user_type="PRODUCT_USER", project_id=cred.project_id, external_id=ext, name=name or ext,
                    email=None if email is ... else email, is_active=True if active is None else active)
        db.add(user)
    else:
        if name:
            user.name = name
        if email is not ...:
            if email is None and user.password_hash:
                raise AppError("validation_failed", fields={"email": "a portal account needs its email"})
            user.email = email
        if active is not None:
            user.is_active = active
    db.flush()
    return user, created


def get_product_user(db: Session, cred: ApiCredential, external_id: str) -> User:
    user = db.scalar(select(User).where(User.project_id == cred.project_id, User.external_id == external_id))
    if user is None:
        raise not_found()
    return user
