"""The one permission module (Phase 1, task 6).

Rights come from the person's active role assignments matched to the ticket's organisation, project and team.
Everything the portal and the API decide about access goes through `Principal` and the functions here. A record
outside the caller's scope is reported as not found (never forbidden).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import false, or_, select, true
from sqlalchemy.orm import Session

from .errors import AppError
from .models import ApiCredential, AuthSession, Project, RoleAssignment, Team, Ticket, User

RESOLVER, TEAM_MANAGER, PROJECT_ADMIN, ORG_ADMIN = "RESOLVER", "TEAM_MANAGER", "PROJECT_ADMIN", "ORG_ADMIN"
ROLE_SCOPE = {RESOLVER: "TEAM", TEAM_MANAGER: "TEAM", PROJECT_ADMIN: "PROJECT", ORG_ADMIN: "ORGANIZATION"}


@dataclass
class Principal:
    db: Session
    user: User
    assignments: list[RoleAssignment]
    via: str = "SESSION"  # SESSION | TOKEN | CREDENTIAL | SYSTEM
    credential: ApiCredential | None = None
    session: AuthSession | None = None
    _project_org: dict[int, int] = field(default_factory=dict)

    # ------------------------------------------------------------ identity
    @property
    def id(self) -> int:
        return self.user.id

    @property
    def is_platform_admin(self) -> bool:
        return bool(self.user.is_platform_admin) and self.user.user_type == "MEMBER"

    @property
    def is_product_user(self) -> bool:
        return self.user.user_type == "PRODUCT_USER"

    @property
    def is_staff(self) -> bool:
        """Holds any role, or is a platform admin."""
        return self.is_platform_admin or bool(self.assignments)

    # ------------------------------------------------------------ role lookups
    def teams(self, *roles: str) -> set[int]:
        roles = roles or (RESOLVER, TEAM_MANAGER)
        return {a.team_id for a in self.assignments if a.scope == "TEAM" and a.role in roles}

    def projects_admin(self) -> set[int]:
        return {a.project_id for a in self.assignments if a.role == PROJECT_ADMIN}

    def orgs_admin(self) -> set[int]:
        return {a.organization_id for a in self.assignments if a.role == ORG_ADMIN}

    def is_resolver(self, team_id: int) -> bool:
        return team_id in self.teams(RESOLVER)

    def is_manager(self, team_id: int) -> bool:
        return team_id in self.teams(TEAM_MANAGER)

    def org_of(self, project_id: int) -> int | None:
        if project_id not in self._project_org:
            self._project_org[project_id] = self.db.scalar(
                select(Project.organization_id).where(Project.id == project_id))
        return self._project_org[project_id]

    def admin_of_project(self, project_id: int) -> bool:
        """Project admin of the project, org admin of its organisation, or platform admin."""
        if self.is_platform_admin:
            return True
        return project_id in self.projects_admin() or self.org_of(project_id) in self.orgs_admin()

    def admin_of_org(self, org_id: int) -> bool:
        return self.is_platform_admin or org_id in self.orgs_admin()

    def any_admin(self) -> bool:
        return self.is_platform_admin or bool(self.projects_admin() or self.orgs_admin())

    def manager_or_above(self, ticket: Ticket) -> bool:
        return self.is_manager(ticket.team_id) or self.admin_of_project(ticket.project_id)

    def has_scope_in_project(self, project_id: int) -> bool:
        """Any role whose scope includes the project."""
        org = self.org_of(project_id)
        for a in self.assignments:
            if a.project_id == project_id or (a.role == ORG_ADMIN and a.organization_id == org):
                return True
        return False

    # ------------------------------------------------------------ admin scope over projects (lists)
    def admin_project_ids(self) -> set[int] | None:
        """Projects the caller administers; None means all (platform admin)."""
        if self.is_platform_admin:
            return None
        ids = set(self.projects_admin())
        orgs = self.orgs_admin()
        if orgs:
            ids |= set(self.db.scalars(select(Project.id).where(Project.organization_id.in_(orgs))))
        return ids

    def raisable_project_ids(self) -> set[int] | None:
        """D21: own organisation's projects for members; projects in any role's scope; all for platform admins;
        a product user only their own project. None means all."""
        if self.is_platform_admin:
            return None
        if self.is_product_user:
            return {self.user.project_id}
        ids = set(self.db.scalars(select(Project.id).where(Project.organization_id == self.user.organization_id)))
        ids |= {a.project_id for a in self.assignments if a.project_id}
        orgs = self.orgs_admin()
        if orgs:
            ids |= set(self.db.scalars(select(Project.id).where(Project.organization_id.in_(orgs))))
        return ids

    def can_raise_in(self, project: Project) -> bool:
        ids = self.raisable_project_ids()
        return ids is None or project.id in ids

    # ------------------------------------------------------------ ticket visibility
    def ticket_filter(self):
        """SQL condition for `visible_tickets` (A6 'See tickets')."""
        if self.is_platform_admin:
            return true()
        me = self.user.id
        if self.is_product_user:
            return Ticket.requester_id == me
        conds = [Ticket.requester_id == me, Ticket.resolver_id == me]
        res_teams = self.teams(RESOLVER)
        mgr_teams = self.teams(TEAM_MANAGER)
        if res_teams:
            conds.append((Ticket.team_id.in_(res_teams)) & (Ticket.status_code == "NEW"))
        if mgr_teams:
            conds.append(Ticket.team_id.in_(mgr_teams))
        if self.projects_admin():
            conds.append(Ticket.project_id.in_(self.projects_admin()))
        if self.orgs_admin():
            conds.append(Ticket.project_id.in_(select(Project.id).where(
                Project.organization_id.in_(self.orgs_admin()))))
        return or_(*conds) if conds else false()

    def can_see(self, ticket: Ticket) -> bool:
        if self.is_platform_admin:
            return True
        me = self.user.id
        if ticket.requester_id == me or ticket.resolver_id == me:
            return True
        if self.is_product_user:
            return False
        if ticket.status_code == "NEW" and self.is_resolver(ticket.team_id):
            return True
        return self.manager_or_above(ticket)

    def is_staff_on(self, ticket: Ticket) -> bool:
        """Sees the ticket as staff (not merely as its requester)."""
        if self.is_platform_admin:
            return True
        if self.is_product_user:
            return False
        if ticket.resolver_id == self.user.id:
            return True
        return self.is_resolver(ticket.team_id) or self.manager_or_above(ticket)


def load_principal(db: Session, user: User, via: str = "SESSION", credential=None, session=None) -> Principal:
    rows = list(db.scalars(select(RoleAssignment).where(
        RoleAssignment.user_id == user.id, RoleAssignment.is_active.is_(True))))
    if rows:
        active_teams = set(db.scalars(select(Team.id).where(Team.id.in_([r.team_id for r in rows if r.team_id]),
                                                            Team.is_active.is_(True))))
        active_projects = set(db.scalars(select(Project.id).where(
            Project.id.in_([r.project_id for r in rows if r.project_id]), Project.is_active.is_(True))))
        rows = [r for r in rows if (r.team_id is None or r.team_id in active_teams)
                and (r.project_id is None or r.project_id in active_projects)]
    return Principal(db=db, user=user, assignments=rows, via=via, credential=credential, session=session)


def system_principal(db: Session) -> Principal:
    """Used by the worker. Never appears in allowed_actions."""
    u = User(id=0, name="System", user_type="MEMBER", is_platform_admin=True, is_active=True)
    return Principal(db=db, user=u, assignments=[], via="SYSTEM")


def require(cond: bool, visible: bool = True):
    """Raise forbidden if the caller can see the record, else not found."""
    if not cond:
        raise AppError("forbidden" if visible else "not_found")
