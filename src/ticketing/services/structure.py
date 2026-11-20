"""Structure services (F2): organisations, projects, teams, users, role assignments, bulk add, project credentials,
project settings. Every write checks scope, validates, audits, in the caller's transaction."""
from __future__ import annotations

import re
import secrets

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from .. import audit, settings
from ..errors import AppError, not_found
from ..models import (
    ApiCredential, Calendar, Organization, Project, RoleAssignment, Team, Ticket, TicketStatus, User,
)
from ..permissions import ORG_ADMIN, PROJECT_ADMIN, RESOLVER, ROLE_SCOPE, TEAM_MANAGER, Principal
from ..security import fast_hash, new_token
from . import auth as auth_service
from .common import bool_field, get_or_404, paginate, raise_if, text_field

KEY_RE = re.compile(r"^[A-Z0-9]{2,10}$")
DONE_CODES = ("RESOLVED", "REJECTED", "WITHDRAWN", "CLOSED")


def open_ticket_filter():
    return Ticket.status_code.notin_(DONE_CODES)


# ================================================================ organisations
def list_organizations(db: Session, p: Principal, type_: str | None = None, active: bool | None = None,
                       page=1, page_size=25, sort=None):
    q = select(Organization)
    if not p.is_platform_admin:
        orgs = p.orgs_admin()
        if not orgs:
            raise AppError("forbidden")
        q = q.where(Organization.id.in_(orgs))
    if type_:
        q = q.where(Organization.type == type_)
    if active is not None:
        q = q.where(Organization.is_active.is_(active))
    return paginate(db, q, page, page_size, sort, {"name": Organization.name, "id": Organization.id},
                    Organization.id)


def get_organization(db: Session, p: Principal, org_id) -> Organization:
    org = get_or_404(db, Organization, org_id)
    if not p.admin_of_org(org.id):
        raise not_found()
    return org


def create_organization(db: Session, p: Principal, data: dict) -> Organization:
    if not p.is_platform_admin:
        raise AppError("forbidden")
    fields = {}
    name = text_field(fields, data, "name")
    type_ = data.get("type")
    if type_ not in ("INTERNAL", "CLIENT"):
        fields["type"] = "INTERNAL or CLIENT"
    raise_if(fields)
    if db.scalar(select(Organization.id).where(func.lower(Organization.name) == name.lower())):
        raise AppError("validation_failed", fields={"name": "already exists"})
    org = Organization(name=name, type=type_, is_active=True)
    db.add(org)
    db.flush()
    audit.record(db, p.id, "create", "organization", org.id, None, audit.snapshot(org), organization_id=org.id)
    return org


def update_organization(db: Session, p: Principal, org_id, data: dict) -> Organization:
    org = get_organization(db, p, org_id)
    if not p.is_platform_admin:
        raise AppError("forbidden")
    old = audit.snapshot(org)
    fields = {}
    if "name" in data:
        name = text_field(fields, data, "name")
        if name and db.scalar(select(Organization.id).where(func.lower(Organization.name) == name.lower(),
                                                           Organization.id != org.id)):
            fields["name"] = "already exists"
        elif name:
            org.name = name
    if "type" in data:
        if data["type"] not in ("INTERNAL", "CLIENT"):
            fields["type"] = "INTERNAL or CLIENT"
        else:
            org.type = data["type"]
    if "is_active" in data:
        v = bool_field(fields, data, "is_active")
        if v is not None:
            org.is_active = v
    raise_if(fields)
    audit.record(db, p.id, "update", "organization", org.id, old, audit.snapshot(org), organization_id=org.id)
    return org


# ================================================================ projects
def _project_scope_query(p: Principal):
    q = select(Project)
    ids = p.admin_project_ids()
    if ids is not None:
        q = q.where(Project.id.in_(ids or {-1}))
    return q


def list_projects(db: Session, p: Principal, organization_id=None, active=None, page=1, page_size=25, sort=None):
    if not p.any_admin():
        raise AppError("forbidden")
    q = _project_scope_query(p)
    if organization_id:
        q = q.where(Project.organization_id == int(organization_id))
    if active is not None:
        q = q.where(Project.is_active.is_(active))
    return paginate(db, q, page, page_size, sort, {"name": Project.name, "key": Project.key, "id": Project.id},
                    Project.id)


def get_project(db: Session, p: Principal, project_id) -> Project:
    proj = get_or_404(db, Project, project_id)
    if not p.admin_of_project(proj.id):
        raise not_found()
    return proj


def project_counts(db: Session, proj: Project) -> dict:
    return {
        "teams": db.scalar(select(func.count()).select_from(Team).where(Team.project_id == proj.id)),
        "open_tickets": db.scalar(select(func.count()).select_from(Ticket).where(
            Ticket.project_id == proj.id, open_ticket_filter())),
        "tickets": db.scalar(select(func.count()).select_from(Ticket).where(Ticket.project_id == proj.id)),
        "people": db.scalar(select(func.count(func.distinct(RoleAssignment.user_id))).where(
            RoleAssignment.project_id == proj.id, RoleAssignment.is_active.is_(True))),
    }


def create_project(db: Session, p: Principal, data: dict) -> Project:
    if not (p.is_platform_admin or p.orgs_admin()):
        raise AppError("forbidden")
    fields = {}
    try:
        org_id = int(data.get("organization_id"))
    except (TypeError, ValueError):
        org_id = None
        fields["organization_id"] = "required"
    name = text_field(fields, data, "name")
    key = (data.get("key") or "").strip().upper()
    if not KEY_RE.match(key):
        fields["key"] = "2 to 10 capitals or digits"
    raise_if(fields)
    org = db.get(Organization, org_id)
    if org is None or not p.admin_of_org(org.id):
        if org is not None and p.any_admin():
            raise AppError("forbidden")
        raise AppError("validation_failed", fields={"organization_id": "unknown"})
    if not org.is_active:
        raise AppError("inactive_reference", fields={"organization_id": "inactive"})
    if db.scalar(select(Project.id).where(Project.key == key)):
        raise AppError("validation_failed", fields={"key": "already used"})
    if db.scalar(select(Project.id).where(Project.organization_id == org.id, func.lower(Project.name) == name.lower())):
        raise AppError("validation_failed", fields={"name": "already exists in this organisation"})
    proj = Project(organization_id=org.id, name=name, key=key, ticket_seq=0, is_active=True)
    db.add(proj)
    db.flush()
    audit.record(db, p.id, "create", "project", proj.id, None, audit.snapshot(proj), org.id, proj.id)
    return proj


def update_project(db: Session, p: Principal, project_id, data: dict) -> Project:
    proj = get_project(db, p, project_id)
    old = audit.snapshot(proj)
    fields = {}
    if "name" in data:
        name = text_field(fields, data, "name")
        if name and db.scalar(select(Project.id).where(Project.organization_id == proj.organization_id,
                                                      func.lower(Project.name) == name.lower(),
                                                      Project.id != proj.id)):
            fields["name"] = "already exists in this organisation"
        elif name:
            proj.name = name
    if "key" in data:
        key = (data.get("key") or "").strip().upper()
        if key != proj.key:
            if proj.ticket_seq > 0 or db.scalar(select(exists().where(Ticket.project_id == proj.id))):
                fields["key"] = "locked: the project already has tickets (D15)"
            elif not KEY_RE.match(key):
                fields["key"] = "2 to 10 capitals or digits"
            elif db.scalar(select(Project.id).where(Project.key == key)):
                fields["key"] = "already used"
            else:
                proj.key = key
    if "is_active" in data:
        v = bool_field(fields, data, "is_active")
        if v is not None:
            proj.is_active = v
    raise_if(fields)
    audit.record(db, p.id, "update", "project", proj.id, old, audit.snapshot(proj), proj.organization_id, proj.id)
    return proj


def get_project_settings(db: Session, p: Principal, project_id) -> list[dict]:
    proj = get_project(db, p, project_id)
    return settings.describe(db, proj.id)


def put_project_settings(db: Session, p: Principal, project_id, changes: dict) -> list[dict]:
    proj = get_project(db, p, project_id)
    if not isinstance(changes, dict):
        raise AppError("validation_failed", fields={"body": "map of key to value"})
    diff = settings.put_project(db, proj.id, changes, p.id)
    for k, (o, n) in diff.items():
        audit.record(db, p.id, "update", "setting", proj.id, {k: o}, {k: n}, proj.organization_id, proj.id)
    return settings.describe(db, proj.id)


def get_global_settings(db: Session, p: Principal) -> list[dict]:
    if not p.is_platform_admin:
        raise AppError("forbidden")
    return settings.describe(db)


def put_global_settings(db: Session, p: Principal, changes: dict) -> list[dict]:
    if not p.is_platform_admin:
        raise AppError("forbidden")
    if not isinstance(changes, dict):
        raise AppError("validation_failed", fields={"body": "map of key to value"})
    diff = settings.put_global(db, changes, p.id)
    for k, (o, n) in diff.items():
        audit.record(db, p.id, "update", "setting", None, {k: o}, {k: n})
    return settings.describe(db)


# ================================================================ teams
def list_teams(db: Session, p: Principal, project_id, active=None, page=1, page_size=100, sort=None):
    proj = get_project(db, p, project_id)
    q = select(Team).where(Team.project_id == proj.id)
    if active is not None:
        q = q.where(Team.is_active.is_(active))
    return paginate(db, q, page, page_size, sort, {"name": Team.name, "id": Team.id}, Team.name)


def get_team(db: Session, p: Principal, team_id) -> Team:
    team = get_or_404(db, Team, team_id)
    if not p.admin_of_project(team.project_id):
        raise not_found()
    return team


def team_members(db: Session, team: Team) -> list[tuple[RoleAssignment, User]]:
    return db.execute(select(RoleAssignment, User).join(User, User.id == RoleAssignment.user_id).where(
        RoleAssignment.team_id == team.id, RoleAssignment.is_active.is_(True)).order_by(User.name)).all()


def create_team(db: Session, p: Principal, project_id, data: dict) -> Team:
    proj = get_project(db, p, project_id)
    fields = {}
    name = text_field(fields, data, "name")
    raise_if(fields)
    if db.scalar(select(Team.id).where(Team.project_id == proj.id, func.lower(Team.name) == name.lower())):
        raise AppError("validation_failed", fields={"name": "already exists in this project"})
    team = Team(project_id=proj.id, name=name, is_active=True)
    db.add(team)
    db.flush()
    audit.record(db, p.id, "create", "team", team.id, None, audit.snapshot(team), proj.organization_id, proj.id)
    return team


def update_team(db: Session, p: Principal, team_id, data: dict) -> Team:
    team = get_team(db, p, team_id)
    old = audit.snapshot(team)
    fields = {}
    if "name" in data:
        name = text_field(fields, data, "name")
        if name and db.scalar(select(Team.id).where(Team.project_id == team.project_id,
                                                   func.lower(Team.name) == name.lower(), Team.id != team.id)):
            fields["name"] = "already exists in this project"
        elif name:
            team.name = name
    if "is_active" in data:
        v = bool_field(fields, data, "is_active")
        if v is False and team.is_active:
            n = db.scalar(select(func.count()).select_from(Ticket).where(Ticket.team_id == team.id,
                                                                          open_ticket_filter()))
            if n:
                fields["is_active"] = f"the team has {n} open tickets; close them first (F2.9)"
            else:
                team.is_active = False
        elif v is True:
            team.is_active = True
    raise_if(fields)
    audit.record(db, p.id, "update", "team", team.id, old, audit.snapshot(team), p.org_of(team.project_id),
                 team.project_id)
    return team


# ================================================================ users
def _user_scope_filter(p: Principal):
    """Who the caller may administer. Platform: everyone. Org admin: members of the organisation and product
    users of its projects. Project admin: members of the project's organisation, product users of the project,
    and anyone holding a role in it."""
    if p.is_platform_admin:
        return None
    conds = []
    orgs = p.orgs_admin()
    if orgs:
        conds.append(User.organization_id.in_(orgs))
        conds.append(User.project_id.in_(select(Project.id).where(Project.organization_id.in_(orgs))))
    projs = p.projects_admin()
    if projs:
        conds.append(User.organization_id.in_(select(Project.organization_id).where(Project.id.in_(projs))))
        conds.append(User.project_id.in_(projs))
        conds.append(User.id.in_(select(RoleAssignment.user_id).where(RoleAssignment.project_id.in_(projs))))
    if not conds:
        raise AppError("forbidden")
    return or_(*conds)


def list_users(db: Session, p: Principal, q_text=None, user_type=None, organization_id=None, project_id=None,
               active=None, page=1, page_size=25, sort=None):
    q = select(User)
    f = _user_scope_filter(p)
    if f is not None:
        q = q.where(f)
    if q_text:
        like = f"%{q_text.strip().lower()}%"
        q = q.where(or_(func.lower(User.name).like(like), func.lower(User.email).like(like),
                        func.lower(User.external_id).like(like)))
    if user_type:
        q = q.where(User.user_type == user_type)
    if organization_id:
        q = q.where(User.organization_id == int(organization_id))
    if project_id:
        pid = int(project_id)
        q = q.where(or_(User.project_id == pid, User.id.in_(
            select(RoleAssignment.user_id).where(RoleAssignment.project_id == pid, RoleAssignment.is_active.is_(True)))))
    if active is not None:
        q = q.where(User.is_active.is_(active))
    return paginate(db, q, page, page_size, sort, {"name": User.name, "email": User.email, "id": User.id}, User.id)


def get_user(db: Session, p: Principal, user_id) -> User:
    user = get_or_404(db, User, user_id)
    f = _user_scope_filter(p)
    if f is not None and not db.scalar(select(User.id).where(User.id == user.id, f)):
        raise not_found()
    return user


def user_assignments(db: Session, user_id: int) -> list[RoleAssignment]:
    return list(db.scalars(select(RoleAssignment).where(RoleAssignment.user_id == user_id)
                           .order_by(RoleAssignment.is_active.desc(), RoleAssignment.id)))


def _clean_email(fields: dict, value, required=True):
    if value is None or not str(value).strip():
        if required:
            fields["email"] = "required"
        return None
    try:
        return validate_email(str(value).strip(), check_deliverability=False, test_environment=True).normalized.lower()
    except EmailNotValidError:
        fields["email"] = "not a valid email"
        return None


def _email_taken(db: Session, email: str, exclude_id: int | None = None) -> bool:
    q = select(User.id).where(func.lower(User.email) == email.lower())
    if exclude_id:
        q = q.where(User.id != exclude_id)
    return db.scalar(q) is not None


def create_user(db: Session, p: Principal, data: dict) -> tuple[User, str | None]:
    if not p.any_admin():
        raise AppError("forbidden")
    fields = {}
    user_type = data.get("user_type") or "MEMBER"
    if user_type not in ("MEMBER", "PRODUCT_USER"):
        fields["user_type"] = "MEMBER or PRODUCT_USER"
    name = text_field(fields, data, "name")
    email = _clean_email(fields, data.get("email"))
    raise_if(fields)
    if _email_taken(db, email):
        raise AppError("validation_failed", fields={"email": "already used (one account per email)"})
    if user_type == "MEMBER":
        org = db.get(Organization, data.get("organization_id")) if data.get("organization_id") else None
        if org is None:
            raise AppError("validation_failed", fields={"organization_id": "required for members"})
        allowed = p.admin_of_org(org.id) or any(p.org_of(pid) == org.id for pid in p.projects_admin())
        if not allowed:
            raise AppError("forbidden")
        if not org.is_active:
            raise AppError("inactive_reference", fields={"organization_id": "inactive"})
        is_pa = bool(data.get("is_platform_admin"))
        if is_pa and not p.is_platform_admin:
            raise AppError("forbidden", "Only platform admins can grant platform administration.")
        user = User(user_type="MEMBER", name=name, email=email, organization_id=org.id, is_platform_admin=is_pa)
        org_id, proj_id = org.id, None
    else:
        proj = db.get(Project, data.get("project_id")) if data.get("project_id") else None
        ext = text_field(fields, data, "external_id")
        if proj is None:
            fields["project_id"] = "required for product users"
        raise_if(fields)
        if not p.admin_of_project(proj.id):
            raise AppError("forbidden")
        if not settings.get(db, "product_user.portal_access", proj.id):
            raise AppError("validation_failed", "Portal access is off for this project, so a portal account "
                           "cannot be created (F9.4). Register the product user through the API instead.",
                           fields={"project_id": "portal access is off"})
        existing = db.scalar(select(User).where(User.project_id == proj.id, User.external_id == ext))
        if existing is not None:
            if existing.password_hash:
                raise AppError("validation_failed", fields={"external_id": "already has a portal account"})
            # The product registered this person through the API first: give that identity a portal account.
            user = existing
            user.name, user.email = name, email
        else:
            user = User(user_type="PRODUCT_USER", name=name, email=email, project_id=proj.id, external_id=ext)
        org_id, proj_id = proj.organization_id, proj.id
    if user.id is None:
        db.add(user)
    db.flush()
    temp = auth_service.issue_temporary_password(db, user)
    audit.record(db, p.id, "create", "user", user.id, None, audit.snapshot(user), org_id, proj_id)
    return user, temp


def update_user(db: Session, p: Principal, user_id, data: dict) -> User:
    user = get_user(db, p, user_id)
    old = audit.snapshot(user)
    fields = {}
    if "name" in data:
        name = text_field(fields, data, "name")
        if name:
            user.name = name
    if "email" in data:
        email = _clean_email(fields, data.get("email"), required=user.user_type == "MEMBER" or bool(user.password_hash))
        if email and _email_taken(db, email, user.id):
            fields["email"] = "already used"
        elif email or "email" not in fields:
            user.email = email
    if "is_platform_admin" in data:
        if not p.is_platform_admin:
            raise AppError("forbidden")
        if user.user_type != "MEMBER":
            fields["is_platform_admin"] = "only members"
        elif user.id == p.id and not data["is_platform_admin"]:
            fields["is_platform_admin"] = "you cannot remove your own platform administration"
        else:
            user.is_platform_admin = bool(data["is_platform_admin"])
    if "is_active" in data:
        v = bool_field(fields, data, "is_active")
        if v is False and user.id == p.id:
            fields["is_active"] = "you cannot deactivate yourself"
        elif v is not None:
            user.is_active = v
            if v is False:
                auth_service.revoke_all(db, user.id)  # F1.9: tokens stop working
    raise_if(fields)
    audit.record(db, p.id, "update", "user", user.id, old, audit.snapshot(user), user.organization_id,
                 user.project_id)
    return user


def reset_password(db: Session, p: Principal, user_id) -> tuple[User, str]:
    user = get_user(db, p, user_id)
    if user.user_type == "PRODUCT_USER" and not settings.get(db, "product_user.portal_access", user.project_id):
        raise AppError("validation_failed", fields={"user": "portal access is off for this product user's project"})
    if not user.email:
        raise AppError("validation_failed", fields={"email": "the user needs an email to sign in"})
    temp = auth_service.issue_temporary_password(db, user)
    audit.record(db, p.id, "reset_password", "user", user.id, None, {"must_change_password": True},
                 user.organization_id, user.project_id)
    return user, temp


def open_tickets_held(db: Session, user_id: int) -> int:
    return db.scalar(select(func.count()).select_from(Ticket).where(Ticket.resolver_id == user_id,
                                                                     open_ticket_filter())) or 0


# ================================================================ role assignments
def _assignment_target(db: Session, data: dict, role: str, fields: dict):
    """Resolve team, project and organisation for a role from the request (A4 scope rule)."""
    scope = ROLE_SCOPE.get(role)
    team = proj = org = None
    if scope == "TEAM":
        team = db.get(Team, data.get("team_id")) if data.get("team_id") else None
        if team is None:
            fields["team_id"] = "required for team roles"
            return scope, None, None, None
        proj = db.get(Project, team.project_id)
        org = db.get(Organization, proj.organization_id)
    elif scope == "PROJECT":
        proj = db.get(Project, data.get("project_id")) if data.get("project_id") else None
        if proj is None:
            fields["project_id"] = "required for PROJECT_ADMIN"
            return scope, None, None, None
        org = db.get(Organization, proj.organization_id)
    elif scope == "ORGANIZATION":
        org = db.get(Organization, data.get("organization_id")) if data.get("organization_id") else None
        if org is None:
            fields["organization_id"] = "required for ORG_ADMIN"
    else:
        fields["role"] = "RESOLVER, TEAM_MANAGER, PROJECT_ADMIN or ORG_ADMIN"
    return scope, team, proj, org


def _can_grant(p: Principal, scope: str, proj: Project | None, org: Organization | None) -> bool:
    if p.is_platform_admin:
        return True
    if scope == "ORGANIZATION":
        return org.id in p.orgs_admin()
    return p.admin_of_project(proj.id)


def _check_holder(db: Session, user: User, org: Organization) -> str | None:
    if user.user_type == "PRODUCT_USER":
        return "product users never hold roles (F2.6)"
    if not user.is_active:
        return "the user is inactive"
    home = db.get(Organization, user.organization_id)
    if home is not None and home.type == "CLIENT" and home.id != org.id:
        return "client staff only hold roles in their own organisation (F2.6)"
    return None


def _check_calendar(db: Session, fields: dict, scope: str, calendar_id):
    if calendar_id in (None, "", 0):
        return None
    if scope != "TEAM":
        fields["calendar_id"] = "only team roles carry a shift calendar"
        return None
    cal = db.get(Calendar, int(calendar_id))
    if cal is None or not cal.is_active:
        fields["calendar_id"] = "unknown or inactive calendar"
        return None
    return cal.id


def _duplicate(db: Session, user_id: int, role: str, scope: str, team, proj, org, exclude_id=None) -> bool:
    q = select(RoleAssignment.id).where(RoleAssignment.user_id == user_id, RoleAssignment.role == role,
                                        RoleAssignment.is_active.is_(True))
    if scope == "TEAM":
        q = q.where(RoleAssignment.team_id == team.id)
    elif scope == "PROJECT":
        q = q.where(RoleAssignment.project_id == proj.id, RoleAssignment.scope == "PROJECT")
    else:
        q = q.where(RoleAssignment.organization_id == org.id, RoleAssignment.scope == "ORGANIZATION")
    if exclude_id:
        q = q.where(RoleAssignment.id != exclude_id)
    return db.scalar(q) is not None


def list_assignments(db: Session, p: Principal, user_id=None, project_id=None, team_id=None, role=None,
                     active=None, page=1, page_size=50, sort=None):
    q = select(RoleAssignment)
    if not p.is_platform_admin:
        projs = p.admin_project_ids() or set()
        conds = [RoleAssignment.project_id.in_(projs or {-1})]
        if p.orgs_admin():
            conds.append(RoleAssignment.organization_id.in_(p.orgs_admin()))
        if not projs and not p.orgs_admin():
            raise AppError("forbidden")
        q = q.where(or_(*conds))
    for col, v in ((RoleAssignment.user_id, user_id), (RoleAssignment.project_id, project_id),
                   (RoleAssignment.team_id, team_id)):
        if v:
            q = q.where(col == int(v))
    if role:
        q = q.where(RoleAssignment.role == role)
    if active is not None:
        q = q.where(RoleAssignment.is_active.is_(active))
    return paginate(db, q, page, page_size, sort, {"id": RoleAssignment.id}, RoleAssignment.id)


def create_assignment(db: Session, p: Principal, data: dict, audit_it: bool = True) -> RoleAssignment:
    if not p.any_admin():
        raise AppError("forbidden")
    fields = {}
    role = data.get("role")
    user = db.get(User, data.get("user_id")) if data.get("user_id") else None
    if user is None:
        fields["user_id"] = "unknown user"
    scope, team, proj, org = _assignment_target(db, data, role, fields)
    raise_if(fields)
    if not _can_grant(p, scope, proj, org):
        raise AppError("forbidden", "You can grant roles only within your own scope (F2.6).")
    if (team and not team.is_active) or (proj and not proj.is_active) or not org.is_active:
        raise AppError("inactive_reference", fields={"scope": "inactive team, project or organisation"})
    problem = _check_holder(db, user, org)
    if problem:
        raise AppError("validation_failed", fields={"user_id": problem})
    cal_id = _check_calendar(db, fields, scope, data.get("calendar_id"))
    raise_if(fields)
    if _duplicate(db, user.id, role, scope, team, proj, org):
        raise AppError("validation_failed", fields={"user_id": "already holds this role here"})
    ra = RoleAssignment(user_id=user.id, role=role, scope=scope, organization_id=org.id,
                        project_id=proj.id if proj else None, team_id=team.id if team else None,
                        calendar_id=cal_id, is_active=True)
    db.add(ra)
    db.flush()
    if audit_it:
        audit.record(db, p.id, "create", "role_assignment", ra.id, None, audit.snapshot(ra), org.id, ra.project_id)
    return ra


def update_assignment(db: Session, p: Principal, ra_id, data: dict) -> RoleAssignment:
    ra = get_or_404(db, RoleAssignment, ra_id)
    proj = db.get(Project, ra.project_id) if ra.project_id else None
    org = db.get(Organization, ra.organization_id)
    if not _can_grant(p, ra.scope, proj, org):
        raise not_found()
    old = audit.snapshot(ra)
    fields = {}
    if "role" in data and data["role"] != ra.role:
        if ROLE_SCOPE.get(data["role"]) != ra.scope:
            fields["role"] = "a role change must keep the scope (team roles: RESOLVER or TEAM_MANAGER)"
        else:
            ra.role = data["role"]
    if "team_id" in data and data["team_id"] != ra.team_id:
        team = db.get(Team, data["team_id"]) if data["team_id"] else None
        if ra.scope != "TEAM" or team is None or team.project_id != ra.project_id or not team.is_active:
            fields["team_id"] = "must be an active team of the same project"
        else:
            ra.team_id = team.id
    if "calendar_id" in data:
        if data["calendar_id"] in (None, "", 0):
            ra.calendar_id = None
        else:
            cal = _check_calendar(db, fields, ra.scope, data["calendar_id"])
            if cal:
                ra.calendar_id = cal
    if "is_active" in data:
        v = bool_field(fields, data, "is_active")
        if v is not None:
            ra.is_active = v
    raise_if(fields)
    if ra.is_active:
        team = db.get(Team, ra.team_id) if ra.team_id else None
        if _duplicate(db, ra.user_id, ra.role, ra.scope, team, proj, org, exclude_id=ra.id):
            raise AppError("validation_failed", fields={"role": "the user already holds this role here"})
    audit.record(db, p.id, "update", "role_assignment", ra.id, old, audit.snapshot(ra), ra.organization_id,
                 ra.project_id)
    return ra


def bulk_assign(db: Session, p: Principal, data: dict) -> dict:
    """F2.5: one role in one team for many people. Reports created, skipped as duplicates, refused with reason."""
    if not p.any_admin():
        raise AppError("forbidden")
    fields = {}
    role = data.get("role")
    if role not in (RESOLVER, TEAM_MANAGER):
        fields["role"] = "RESOLVER or TEAM_MANAGER"
    team = db.get(Team, data.get("team_id")) if data.get("team_id") else None
    if team is None:
        fields["team_id"] = "required"
    ids = data.get("user_ids")
    if not isinstance(ids, list) or not ids:
        fields["user_ids"] = "a non-empty list"
    raise_if(fields)
    if not p.admin_of_project(team.project_id):
        raise AppError("forbidden")
    result = {"created": [], "skipped": [], "refused": []}
    for uid in ids:
        base = {"user_id": uid, "role": role, "team_id": team.id, "calendar_id": data.get("calendar_id")}
        try:
            with db.begin_nested():
                ra = create_assignment(db, p, base)
            result["created"].append({"user_id": uid, "assignment_id": ra.id})
        except AppError as e:
            reason = "; ".join(f"{v}" for v in (e.fields or {}).values()) or e.message
            if "already holds" in reason:
                result["skipped"].append({"user_id": uid, "reason": "duplicate"})
            else:
                result["refused"].append({"user_id": uid, "reason": reason})
    return result


# ================================================================ credentials
def list_credentials(db: Session, p: Principal, project_id):
    proj = get_project(db, p, project_id)
    return list(db.scalars(select(ApiCredential).where(ApiCredential.project_id == proj.id).order_by(ApiCredential.id)))


def create_credential(db: Session, p: Principal, project_id, data: dict) -> tuple[ApiCredential, str]:
    proj = get_project(db, p, project_id)
    fields = {}
    name = text_field(fields, data, "name")
    raise_if(fields)
    key = "pk_" + secrets.token_hex(8)
    secret = new_token() + new_token()  # long random secret; only its SHA-256 is stored (F2.8)
    cred = ApiCredential(project_id=proj.id, name=name, key_prefix=key, secret_hash=fast_hash(secret), is_active=True)
    db.add(cred)
    db.flush()
    audit.record(db, p.id, "create", "credential", cred.id, None, audit.snapshot(cred), proj.organization_id, proj.id)
    return cred, secret


def update_credential(db: Session, p: Principal, cred_id, data: dict) -> ApiCredential:
    cred = get_or_404(db, ApiCredential, cred_id)
    if not p.admin_of_project(cred.project_id):
        raise not_found()
    old = audit.snapshot(cred)
    fields = {}
    if "name" in data:
        name = text_field(fields, data, "name")
        if name:
            cred.name = name
    if "is_active" in data:
        v = bool_field(fields, data, "is_active")
        if v is not None:
            cred.is_active = v
    raise_if(fields)
    audit.record(db, p.id, "update", "credential", cred.id, old, audit.snapshot(cred), p.org_of(cred.project_id),
                 cred.project_id)
    return cred


# ================================================================ audit read
def list_audit(db: Session, p: Principal, entity=None, entity_id=None, actor=None, date_from=None, date_to=None,
               page=1, page_size=50):
    from ..models import AuditLog
    q = select(AuditLog)
    if not p.is_platform_admin:
        projs = p.projects_admin()
        orgs = p.orgs_admin()
        if not projs and not orgs:
            raise AppError("forbidden")
        conds = []
        if orgs:
            conds.append(AuditLog.organization_id.in_(orgs))
        if projs:
            conds.append(AuditLog.project_id.in_(projs))
        q = q.where(or_(*conds))
    if entity:
        q = q.where(AuditLog.entity == entity)
    if entity_id:
        q = q.where(AuditLog.entity_id == int(entity_id))
    if actor:
        q = q.where(AuditLog.actor_id == int(actor))
    if date_from:
        q = q.where(AuditLog.created_at >= date_from)
    if date_to:
        q = q.where(AuditLog.created_at < date_to)
    return paginate(db, q, page, page_size, None, None, AuditLog.id.desc())


def status_codes(db: Session) -> list[str]:
    return list(db.scalars(select(TicketStatus.code).order_by(TicketStatus.sort_order)))
