"""Request plumbing: one transaction per request, caller resolution (session, token, project credential), the
forced password change gate and CSRF for cookie-authenticated API writes."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import Depends, Request
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import settings
from ..config import get_config
from ..db import SessionLocal
from ..errors import AppError
from ..models import User
from ..permissions import Principal, load_principal
from ..security import constant_eq, sign
from ..services import auth as auth_service

SESSION_COOKIE = "tsid"
CSRF_HEADER = "x-csrf-token"


class TxRoute(APIRoute):
    """Commits the request's session after the handler succeeds and before the response is sent."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            response = await original(request)
            db = getattr(request.state, "db", None)
            if db is not None and db.is_active:
                db.commit()
            return response

        return handler


async def get_db(request: Request):
    """Async generator on purpose: its teardown runs on the event loop, so a request that holds a row lock never
    waits for a threadpool slot that the requests blocked on that lock are occupying (a deadlock under load)."""
    db = SessionLocal()
    request.state.db = db
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def csrf_token_for(session_token_hash: str) -> str:
    return sign(get_config().secret_key, "csrf:" + session_token_hash)


# Paths a flagged account may still use (A11.2 password_change_required). /me is allowed so the portal can route.
PASSWORD_GATE_ALLOWED = ("/api/v1/auth/change-password", "/api/v1/auth/logout", "/api/v1/me")


def _person(request: Request, db: Session) -> Principal | None:
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        found = auth_service.resolve_token(db, authz[7:].strip(), "API_TOKEN")
        if not found:
            raise AppError("not_authenticated")
        user, row = found
        return load_principal(db, user, via="TOKEN", session=row)
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        found = auth_service.resolve_token(db, cookie, "SESSION")
        if not found:
            db.commit()  # keep the idle-expiry revocation
            raise AppError("not_authenticated", "Your session expired. Sign in again.")
        user, row = found
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            sent = request.headers.get(CSRF_HEADER, "")
            if not sent or not constant_eq(sent, csrf_token_for(row.token_hash)):
                raise AppError("forbidden", "Missing or invalid CSRF token (send the X-CSRF-Token header).")
        return load_principal(db, user, via="SESSION", session=row)
    return None


def _product(request: Request, db: Session, need_user: bool = True) -> Principal | None:
    key = request.headers.get("x-project-key")
    if not key:
        return None
    cred = auth_service.resolve_credential(db, key, request.headers.get("x-project-secret", ""))
    if cred is None:
        raise AppError("not_authenticated", "Unknown or revoked project credential.")
    if not settings.get(db, "product_user.api_access", cred.project_id):
        raise AppError("forbidden", "API access for product users is off for this project.")
    request.state.credential = cred
    if not need_user:
        return None
    ext = (request.headers.get("x-external-user") or "").strip()
    if not ext:
        raise AppError("not_authenticated", "Send X-External-User with the product user's ID.")
    user = db.scalar(select(User).where(User.project_id == cred.project_id, User.external_id == ext))
    if user is None or not user.is_active:
        raise AppError("not_authenticated", "Unknown product user. Register them first (PUT /product-users/{id}).")
    return load_principal(db, user, via="CREDENTIAL", credential=cred)


def _gate(request: Request, p: Principal) -> Principal:
    if p.user.must_change_password and p.via != "CREDENTIAL" and request.url.path not in PASSWORD_GATE_ALLOWED:
        raise AppError("password_change_required")
    return p


def person(request: Request, db: Session = Depends(get_db)) -> Principal:
    """Auth U: a signed-in person (session or token)."""
    p = _person(request, db)
    if p is None:
        raise AppError("not_authenticated")
    return _gate(request, p)


def person_or_product(request: Request, db: Session = Depends(get_db)) -> Principal:
    """Auth U or K."""
    p = _product(request, db) or _person(request, db)
    if p is None:
        raise AppError("not_authenticated")
    return _gate(request, p)


def credential(request: Request, db: Session = Depends(get_db)):
    """Auth K without a product user (G9)."""
    _product(request, db, need_user=False)
    cred = getattr(request.state, "credential", None)
    if cred is None:
        raise AppError("not_authenticated", "Send X-Project-Key and X-Project-Secret.")
    return cred


def staff_only(p: Principal):
    if p.via == "CREDENTIAL":
        raise AppError("forbidden")
    return p


class RateLimiter:
    """Failed sign-in attempts per source (A11.2 rate_limited). In-process; fine for one deployment."""

    def __init__(self, limit: int = 20, window_seconds: int = 300):
        self.limit, self.window = limit, window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, source: str):
        now = time.monotonic()
        q = self.hits[source]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            raise AppError("rate_limited")

    def fail(self, source: str):
        self.hits[source].append(time.monotonic())

    def reset(self):
        self.hits.clear()


login_limiter = RateLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
