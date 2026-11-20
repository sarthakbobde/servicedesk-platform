"""Notifications (F8.1, F8.2, F8.4, F8.5). Written in the action's transaction: an in-app row per recipient
(in-app is always on) and an outbox row per enabled external channel whose template for the type is enabled.
External delivery happens after commit (services/channels.py, run by the worker)."""
from __future__ import annotations

import string
from datetime import timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import clock
from ..config import get_config
from ..errors import AppError, not_found
from ..models import (
    Notification, NotificationChannel, NotificationOutbox, NotificationTemplate, Priority, Project, Ticket,
    TicketStatus, User,
)
from ..permissions import Principal
from ..seed import TEMPLATES
from .common import paginate

TYPES = tuple(TEMPLATES)
PLACEHOLDERS = ("ticket_number", "title", "status", "priority", "resolver", "due_time", "link")


class _Safe(dict):
    def __missing__(self, key):
        return ""


def placeholders_in(text: str) -> set[str]:
    names = set()
    for _, field, _, _ in string.Formatter().parse(text or ""):
        if field is not None:
            names.add(field)
    return names


def check_placeholders(*texts: str) -> list[str]:
    try:
        unknown = set().union(*(placeholders_in(t) for t in texts)) - set(PLACEHOLDERS)
    except ValueError:
        return ["unbalanced braces"]
    return sorted(unknown)


def render(text: str | None, ctx: dict) -> str:
    if not text:
        return ""
    try:
        return string.Formatter().vformat(text, (), _Safe(ctx))
    except (ValueError, IndexError, KeyError):
        return text


def ticket_number(db: Session, t: Ticket) -> str:
    key = db.scalar(select(Project.key).where(Project.id == t.project_id))
    return f"{key}-{t.number}"


def context(db: Session, t: Ticket, extra: dict | None = None) -> dict:
    from . import sla  # late import: sla depends on tickets helpers
    resolver = db.get(User, t.resolver_id) if t.resolver_id else None
    due = sla.nearest_due(db, t)
    due_text = ""
    if due and due.get("due_at"):
        tz = due.get("time_zone") or "UTC"
        due_text = due["due_at"].astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M ") + tz
    ctx = {
        "ticket_number": ticket_number(db, t),
        "title": t.title,
        "status": db.scalar(select(TicketStatus.name).where(TicketStatus.code == t.status_code)) or t.status_code,
        "priority": db.scalar(select(Priority.name).where(Priority.id == t.priority_id)) or "",
        "resolver": resolver.name if resolver else "unassigned",
        "due_time": due_text,
        "link": f"{get_config().base_url.rstrip('/')}/tickets/{t.id}",
    }
    ctx.update(extra or {})
    return ctx


def notify(db: Session, ntype: str, ticket: Ticket, user_ids, actor_id: int | None = None, rule_id: int | None = None,
           extra: dict | None = None) -> list[Notification]:
    assert ntype in TYPES, ntype
    ids = []
    for uid in user_ids:
        if uid and uid != actor_id and uid not in ids:
            ids.append(uid)
    if not ids:
        return []
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_(ids), User.is_active.is_(True)))}
    if not users:
        return []
    ctx = context(db, ticket, extra)
    rows = db.execute(select(NotificationTemplate, NotificationChannel).join(
        NotificationChannel, NotificationChannel.id == NotificationTemplate.channel_id).where(
        NotificationTemplate.notification_type == ntype)).all()
    in_app = next((tpl for tpl, ch in rows if ch.type == "IN_APP"), None)
    subject_t, body_t = (in_app.subject, in_app.body) if in_app else TEMPLATES[ntype]
    external = [(tpl, ch) for tpl, ch in rows if ch.type != "IN_APP" and ch.enabled and tpl.enabled]
    created, webhook_done = [], set()
    now = clock.now()
    for uid in ids:
        user = users.get(uid)
        if user is None:
            continue
        n = Notification(user_id=uid, ticket_id=ticket.id, type=ntype, subject=render(subject_t, ctx)[:300],
                         body=render(body_t, ctx), rule_id=rule_id, created_at=now)
        db.add(n)
        db.flush()
        created.append(n)
        for tpl, ch in external:
            if ch.type == "EMAIL" and not user.email:
                continue
            if ch.type == "CHAT_WEBHOOK":  # one post per channel per event, not per recipient
                if ch.id in webhook_done:
                    continue
                webhook_done.add(ch.id)
            db.add(NotificationOutbox(notification_id=n.id, channel_id=ch.id, subject=render(tpl.subject, ctx)[:300],
                                      body=render(tpl.body, ctx), status="PENDING", next_attempt_at=now,
                                      created_at=now))
    return created


# ---------------------------------------------------------------- inbox (G8)
def inbox(db: Session, p: Principal, unread: bool | None = None, page=1, page_size=25):
    q = select(Notification).where(Notification.user_id == p.id)
    if unread:
        q = q.where(Notification.is_read.is_(False))
    return paginate(db, q, page, page_size, None, None, [Notification.created_at.desc(), Notification.id.desc()])


def unread_count(db: Session, p: Principal) -> int:
    return db.scalar(select(func.count()).select_from(Notification).where(
        Notification.user_id == p.id, Notification.is_read.is_(False))) or 0


def mark_read(db: Session, p: Principal, nid) -> None:
    try:
        n = db.get(Notification, int(nid))
    except (TypeError, ValueError):
        n = None
    if n is None or n.user_id != p.id:
        raise not_found()
    n.is_read = True


def mark_all_read(db: Session, p: Principal) -> None:
    db.execute(update(Notification).where(Notification.user_id == p.id, Notification.is_read.is_(False))
               .values(is_read=True))


def ensure_type(ntype: str):
    if ntype not in TYPES:
        raise AppError("validation_failed", fields={"notification_type": f"one of {', '.join(TYPES)}"})


def utc(dt):
    return dt.astimezone(timezone.utc) if dt else None
