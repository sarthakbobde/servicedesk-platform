"""G3 Structure administration (API-007..034) and G4 Audit (API-035). Shared admin pattern (A11.5)."""
from datetime import datetime

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import Project
from ..permissions import Principal
from ..services import structure as st
from . import serialize as S
from .deps import TxRoute, get_db, person, staff_only

router = APIRouter(prefix="/api/v1/admin", route_class=TxRoute, tags=["G3 Structure administration"])


def admin(p: Principal = Depends(person)) -> Principal:
    return staff_only(p)


def _bool(v: str | None):
    if v is None or v == "":
        return None
    return v.lower() in ("1", "true", "yes", "on")


def _date(v: str | None, name: str):
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise AppError("validation_failed", fields={name: "ISO 8601 date or time"})



# ---------------------------------------------------------------- organisations
@router.get("/organizations", summary="API-007 List organisations")
def list_orgs(type: str | None = None, active: str | None = None, page: int = 1, page_size: int = 25,
              sort: str | None = None, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    rows, meta = st.list_organizations(db, p, type, _bool(active), page, page_size, sort)
    return S.page([S.organization(o) for o in rows], meta)


@router.post("/organizations", status_code=201, summary="API-008 Create an organisation")
def create_org(body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return S.organization(st.create_organization(db, p, body))


@router.get("/organizations/{org_id}", summary="API-009 One organisation with its projects")
def get_org(org_id: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    org = st.get_organization(db, p, org_id)
    projects = list(db.scalars(select(Project).where(Project.organization_id == org.id).order_by(Project.name)))
    return S.organization(org, projects)


@router.patch("/organizations/{org_id}", summary="API-010 Rename, activate or deactivate")
def patch_org(org_id: int, body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return S.organization(st.update_organization(db, p, org_id, body))


# ---------------------------------------------------------------- projects
@router.get("/projects", summary="API-011 List projects")
def list_projects(organization_id: int | None = None, active: str | None = None, page: int = 1, page_size: int = 25,
                  sort: str | None = None, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    rows, meta = st.list_projects(db, p, organization_id, _bool(active), page, page_size, sort)
    return S.page([S.project(x, st.project_counts(db, x)) for x in rows], meta)


@router.post("/projects", status_code=201, summary="API-012 Create a project with its ticket-number key")
def create_project(body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return S.project(st.create_project(db, p, body))


@router.get("/projects/{project_id}", summary="API-013 One project with counts")
def get_project(project_id: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    proj = st.get_project(db, p, project_id)
    return S.project(proj, st.project_counts(db, proj))


@router.patch("/projects/{project_id}", summary="API-014 Rename, activate or deactivate; key locked after first ticket")
def patch_project(project_id: int, body: dict = Body(...), p: Principal = Depends(admin),
                  db: Session = Depends(get_db)):
    return S.project(st.update_project(db, p, project_id, body))


# ---------------------------------------------------------------- settings
@router.get("/settings", summary="API-015 Global settings with defaults")
def get_settings(p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return {"items": st.get_global_settings(db, p)}


@router.put("/settings", summary="API-016 Change global settings")
def put_settings(body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return {"items": st.put_global_settings(db, p, body)}


@router.get("/projects/{project_id}/settings", summary="API-017 Effective project settings")
def get_project_settings(project_id: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return {"items": st.get_project_settings(db, p, project_id)}


@router.put("/projects/{project_id}/settings", summary="API-018 Change project settings")
def put_project_settings(project_id: int, body: dict = Body(...), p: Principal = Depends(admin),
                         db: Session = Depends(get_db)):
    return {"items": st.put_project_settings(db, p, project_id, body)}


# ---------------------------------------------------------------- teams
@router.get("/projects/{project_id}/teams", summary="API-019 Teams of a project")
def list_teams(project_id: int, active: str | None = None, page: int = 1, page_size: int = 100,
               p: Principal = Depends(admin), db: Session = Depends(get_db)):
    rows, meta = st.list_teams(db, p, project_id, _bool(active), page, page_size)
    return S.page([S.team(t) for t in rows], meta)


@router.post("/projects/{project_id}/teams", status_code=201, summary="API-020 Create a team")
def create_team(project_id: int, body: dict = Body(...), p: Principal = Depends(admin),
                db: Session = Depends(get_db)):
    return S.team(st.create_team(db, p, project_id, body))


@router.get("/teams/{team_id}", summary="API-021 One team with its managers and resolvers")
def get_team(team_id: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    team = st.get_team(db, p, team_id)
    return S.team(team, st.team_members(db, team))


@router.patch("/teams/{team_id}", summary="API-022 Rename, activate or deactivate")
def patch_team(team_id: int, body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return S.team(st.update_team(db, p, team_id, body))


# ---------------------------------------------------------------- users
@router.get("/users", summary="API-023 List and search users")
def list_users(q: str | None = None, user_type: str | None = None, organization_id: int | None = None,
               project_id: int | None = None, active: str | None = None, page: int = 1, page_size: int = 25,
               sort: str | None = None, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    rows, meta = st.list_users(db, p, q, user_type, organization_id, project_id, _bool(active), page, page_size, sort)
    return S.page([S.user(u) for u in rows], meta)


@router.post("/users", status_code=201, summary="API-024 Create a member or product user (temporary password once)")
def create_user(body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    user, temp = st.create_user(db, p, body)
    return {**S.user(user), "temporary_password": temp}


@router.get("/users/{user_id}", summary="API-025 One user with role assignments")
def get_user(user_id: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    user = st.get_user(db, p, user_id)
    out = S.user(user, st.user_assignments(db, user.id), db)
    out["open_tickets_held"] = st.open_tickets_held(db, user.id)
    return out


@router.patch("/users/{user_id}", summary="API-026 Change name or email; activate or deactivate")
def patch_user(user_id: int, body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    user = st.update_user(db, p, user_id, body)
    out = S.user(user)
    out["open_tickets_held"] = st.open_tickets_held(db, user.id)
    return out


@router.post("/users/{user_id}/reset-password", summary="API-027 New temporary password shown once")
def reset_password(user_id: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    user, temp = st.reset_password(db, p, user_id)
    return {"id": user.id, "temporary_password": temp, "must_change_password": True}


# ---------------------------------------------------------------- role assignments
@router.get("/role-assignments", summary="API-028 Filter role assignments")
def list_assignments(user_id: int | None = None, project_id: int | None = None, team_id: int | None = None,
                     role: str | None = None, active: str | None = None, page: int = 1, page_size: int = 50,
                     p: Principal = Depends(admin), db: Session = Depends(get_db)):
    rows, meta = st.list_assignments(db, p, user_id, project_id, team_id, role, _bool(active), page, page_size)
    return S.page([S.assignment(a, db) for a in rows], meta)


@router.post("/role-assignments", status_code=201, summary="API-029 Give one role at one scope")
def create_assignment(body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return S.assignment(st.create_assignment(db, p, body), db)


@router.post("/role-assignments/bulk", summary="API-030 Give one role in one team to many users")
def bulk_assign(body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return st.bulk_assign(db, p, body)


@router.patch("/role-assignments/{ra_id}", summary="API-031 Change role, team or shift calendar; deactivate")
def patch_assignment(ra_id: int, body: dict = Body(...), p: Principal = Depends(admin),
                     db: Session = Depends(get_db)):
    return S.assignment(st.update_assignment(db, p, ra_id, body), db)


# ---------------------------------------------------------------- credentials
@router.get("/projects/{project_id}/credentials", summary="API-032 List project credentials (never the secret)")
def list_credentials(project_id: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    rows = st.list_credentials(db, p, project_id)
    return {"items": [S.credential(c) for c in rows], "page": 1, "page_size": len(rows), "total": len(rows)}


@router.post("/projects/{project_id}/credentials", status_code=201, summary="API-033 Create a credential")
def create_credential(project_id: int, body: dict = Body(...), p: Principal = Depends(admin),
                      db: Session = Depends(get_db)):
    cred, secret = st.create_credential(db, p, project_id, body)
    return S.credential(cred, secret)


@router.patch("/credentials/{cred_id}", summary="API-034 Rename or revoke")
def patch_credential(cred_id: int, body: dict = Body(...), p: Principal = Depends(admin),
                     db: Session = Depends(get_db)):
    return S.credential(st.update_credential(db, p, cred_id, body))


# ---------------------------------------------------------------- audit
@router.get("/audit", tags=["G4 Audit"], summary="API-035 Configuration change log")
def audit_log(entity: str | None = None, entity_id: int | None = None, actor: int | None = None,
              date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
              page: int = 1, page_size: int = 50, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    rows, meta = st.list_audit(db, p, entity, entity_id, actor, _date(date_from, "from"), _date(date_to, "to"),
                               page, page_size)
    names = S.user_names(db, [r.actor_id for r in rows])
    return S.page([S.audit_row(r, names) for r in rows], meta)
