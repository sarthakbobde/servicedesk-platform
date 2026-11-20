"""Accounts and sign-in (F1): hashing, sign-in, lockout, server-side sessions and revocable tokens, change and
reset password, deactivation."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import clock, settings
from ..errors import AppError
from ..models import ApiCredential, AuthSession, User
from ..permissions import Principal, load_principal
from ..security import fast_hash, hash_password, new_token, temporary_password, verify_password


TOUCH_EVERY = timedelta(seconds=60)


def find_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.email) == (email or "").strip().lower()))


def _portal_allowed(db: Session, user: User) -> bool:
    if user.user_type != "PRODUCT_USER":
        return True
    return bool(settings.get(db, "product_user.portal_access", user.project_id))


def authenticate(db: Session, email: str, password: str) -> User:
    """Check email and password with lockout (F1.2, F1.3). One message for every failure."""
    s = settings.effective(db)
    user = find_by_email(db, email)
    now = clock.now()
    if user is None:
        verify_password(None, password or "")
        raise AppError("login_failed")
    db.execute(select(User).where(User.id == user.id).with_for_update())  # serialise counter updates
    if user.locked_until and user.locked_until > now:
        raise AppError("login_failed")
    ok = verify_password(user.password_hash, password or "")
    if not ok or not user.is_active or not _portal_allowed(db, user):
        if ok is False:
            user.failed_logins = (user.failed_logins or 0) + 1
            if user.failed_logins >= s["auth.lockout_attempts"]:
                user.locked_until = now + timedelta(minutes=s["auth.lockout_minutes"])
                user.failed_logins = 0
        db.commit()  # the failure count must survive the error response
        raise AppError("login_failed")
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    return user


def start_session(db: Session, user: User, kind: str = "SESSION") -> tuple[str, AuthSession]:
    s = settings.effective(db)
    now = clock.now()
    token = new_token()
    if kind == "SESSION":
        expires = min(now + timedelta(hours=s["auth.session_max_hours"]),
                      now + timedelta(minutes=s["auth.session_idle_minutes"]))
    else:
        expires = now + timedelta(minutes=s["auth.token_minutes"])
    row = AuthSession(user_id=user.id, kind=kind, token_hash=fast_hash(token), created_at=now, last_seen_at=now,
                      expires_at=expires)
    db.add(row)
    db.flush()
    return token, row


def resolve_token(db: Session, token: str, kind: str) -> tuple[User, AuthSession] | None:
    """Session cookie or bearer token -> user. Applies idle and absolute limits (F1.7) and deactivation (F1.9)."""
    if not token:
        return None
    row = db.scalar(select(AuthSession).where(AuthSession.token_hash == fast_hash(token), AuthSession.kind == kind))
    now = clock.now()
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active or (kind == "SESSION" and not _portal_allowed(db, user)):
        return None
    if kind == "SESSION":
        s = settings.effective(db)
        absolute = row.created_at + timedelta(hours=s["auth.session_max_hours"])
        if now - row.last_seen_at > timedelta(minutes=s["auth.session_idle_minutes"]) or now >= absolute:
            row.revoked_at = now
            return None
        if now - row.last_seen_at >= TOUCH_EVERY:
            # Touch at most once a minute, in its own short transaction, so the session row is never held locked
            # for the length of a request (many tabs and requests share one session).
            from ..db import get_engine
            expires = min(absolute, now + timedelta(minutes=s["auth.session_idle_minutes"]))
            with get_engine().begin() as conn:
                conn.execute(update(AuthSession).where(AuthSession.id == row.id)
                             .values(last_seen_at=now, expires_at=expires))
            from sqlalchemy.orm.attributes import set_committed_value  # reflect it without a second UPDATE
            set_committed_value(row, "last_seen_at", now)
            set_committed_value(row, "expires_at", expires)
    return user, row


def resolve_credential(db: Session, key: str, secret: str) -> ApiCredential | None:
    if not key or not secret:
        return None
    cred = db.scalar(select(ApiCredential).where(ApiCredential.key_prefix == key))
    if cred is None or not cred.is_active or fast_hash(secret) != cred.secret_hash:
        return None
    cred.last_used_at = clock.now()
    return cred


def logout(db: Session, session: AuthSession | None):
    if session is not None and session.revoked_at is None:
        session.revoked_at = clock.now()


def revoke_all(db: Session, user_id: int, except_id: int | None = None):
    q = update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
    if except_id:
        q = q.where(AuthSession.id != except_id)
    db.execute(q.values(revoked_at=clock.now()))


def check_password_rules(db: Session, user: User, new_password: str, current_password: str | None = None) -> dict:
    fields = {}
    minimum = settings.get(db, "auth.password_min_length")
    if not new_password or len(new_password) < minimum:
        fields["new_password"] = f"at least {minimum} characters"
    elif user.email and new_password.strip().lower() == user.email.strip().lower():
        fields["new_password"] = "must not equal the email"
    elif current_password is not None and new_password == current_password:
        fields["new_password"] = "must differ from the current password"
    elif user.password_hash and verify_password(user.password_hash, new_password):
        fields["new_password"] = "must differ from the current password"
    return fields


def change_password(db: Session, p: Principal, current_password: str, new_password: str):
    user = p.user
    if not verify_password(user.password_hash, current_password or ""):
        raise AppError("validation_failed", fields={"current_password": "not correct"})
    fields = check_password_rules(db, user, new_password, current_password)
    if fields:
        raise AppError("validation_failed", fields=fields)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    revoke_all(db, user.id, except_id=p.session.id if p.session else None)  # F1.5: other sessions end


def issue_temporary_password(db: Session, user: User) -> str:
    """F1.1 / F1.6: shown once; old password stops working; change forced."""
    temp = temporary_password()
    user.password_hash = hash_password(temp)
    user.must_change_password = True
    user.failed_logins = 0
    user.locked_until = None
    revoke_all(db, user.id)
    return temp


def principal_for(db: Session, user: User, via: str, session=None, credential=None) -> Principal:
    return load_principal(db, user, via=via, session=session, credential=credential)
