"""G1 Session and account (API-001..005) and G2 Health (API-006)."""
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import settings
from ..config import get_config
from ..errors import AppError
from ..models import Project
from ..permissions import Principal
from ..services import auth as auth_service
from ..services.capabilities import capabilities
from . import serialize as S
from .deps import (
    SESSION_COOKIE, TxRoute, client_ip, csrf_token_for, get_db, login_limiter, person, person_or_product,
)

router = APIRouter(prefix="/api/v1", route_class=TxRoute)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=500)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=500)
    new_password: str = Field(max_length=500)


def set_session_cookie(response: Response, token: str, db: Session):
    hours = settings.get(db, "auth.session_max_hours")
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=get_config().cookie_secure,
                        max_age=hours * 3600, path="/")


def _authenticate(request: Request, db: Session, body: LoginIn):
    ip = client_ip(request)
    login_limiter.check(ip)
    try:
        return auth_service.authenticate(db, body.email, body.password)
    except AppError:
        login_limiter.fail(ip)
        raise


@router.post("/auth/login", tags=["G1 Session and account"], summary="API-001 Sign in")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    user = _authenticate(request, db, body)
    token, row = auth_service.start_session(db, user, "SESSION")
    set_session_cookie(response, token, db)
    return {"user": {"id": user.id, "name": user.name, "email": user.email, "user_type": user.user_type},
            "must_change_password": user.must_change_password, "csrf_token": csrf_token_for(row.token_hash)}


@router.post("/auth/logout", status_code=204, tags=["G1 Session and account"], summary="API-002 Sign out")
def logout(response: Response, p: Principal = Depends(person), db: Session = Depends(get_db)):
    auth_service.logout(db, p.session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = 204
    return None


@router.post("/auth/token", tags=["G1 Session and account"], summary="API-003 Exchange email and password for a token")
def token(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    user = _authenticate(request, db, body)
    if user.user_type == "PRODUCT_USER":
        raise AppError("forbidden", "Product users use the project credential, not tokens.")
    tok, row = auth_service.start_session(db, user, "API_TOKEN")
    return {"access_token": tok, "token_type": "Bearer",
            "expires_in": int((row.expires_at - row.created_at).total_seconds()),
            "must_change_password": user.must_change_password}


@router.post("/auth/change-password", status_code=204, tags=["G1 Session and account"],
             summary="API-004 Change own password")
def change_password(body: ChangePasswordIn, p: Principal = Depends(person), db: Session = Depends(get_db)):
    auth_service.change_password(db, p, body.current_password, body.new_password)
    return Response(status_code=204)


@router.get("/me", tags=["G1 Session and account"], summary="API-005 Who am I")
def me(p: Principal = Depends(person_or_product), db: Session = Depends(get_db)):
    u = p.user
    out = {"user": {"id": u.id, "name": u.name, "email": u.email, "user_type": u.user_type,
                    "is_platform_admin": p.is_platform_admin, "must_change_password": u.must_change_password}}
    if p.via == "CREDENTIAL":
        proj = db.get(Project, u.project_id)
        out["project"] = {"id": proj.id, "key": proj.key, "name": proj.name}
        out["user"]["external_id"] = u.external_id
        return out
    out["assignments"] = [S.assignment(a, db) for a in p.assignments]
    ids = p.raisable_project_ids()
    q = select(Project).where(Project.is_active.is_(True)).order_by(Project.name)
    if ids is not None:
        q = q.where(Project.id.in_(ids or {-1}))
    out["projects"] = [{"id": x.id, "key": x.key, "name": x.name} for x in db.scalars(q)]
    out["capabilities"] = sorted(capabilities(p))
    return out


@router.get("/health", tags=["G2 Health"], summary="API-006 Liveness and database check")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("select 1"))
        return {"status": "ok", "database": "ok"}
    except Exception:  # pragma: no cover - exercised by the health check drill
        return Response('{"status":"degraded","database":"error"}', status_code=503, media_type="application/json")
