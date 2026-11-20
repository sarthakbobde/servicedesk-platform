"""Operations view: admin dashboard (F10.4) and system status (F10.5). Counts only, no charts (Q3)."""
from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .. import clock, settings
from ..errors import AppError
from ..models import (
    Calendar, CalendarHours, Job, NotificationChannel, NotificationOutbox, Project, RoleAssignment, SlaTimer, Team,
    Ticket, TicketStatus,
)
from ..permissions import Principal
from . import jobs, sla
from .tickets import ACTIVE, Ctx, view


def _scope(p: Principal, q, project=None, team=None):
    ids = p.admin_project_ids()
    if ids is not None:
        q = q.where(Ticket.project_id.in_(ids or {-1}))
    if project:
        q = q.where(Ticket.project_id == int(project))
    if team:
        q = q.where(Ticket.team_id == int(team))
    return q


def dashboard(db: Session, p: Principal, project=None, team=None) -> dict:
    if not p.any_admin():
        raise AppError("forbidden")
    now = clock.now()
    ctx = Ctx(db)
    active = Ticket.status_code.in_(ACTIVE)
    overdue_q = _scope(p, select(Ticket).where(active, sla.overdue_clause()), project, team)
    overdue = list(db.scalars(overdue_q.order_by(Ticket.created_at).limit(20)))
    pct = settings.get(db, "sla.warning_percent")
    # at risk: a running step past the warning share but not yet due (approximation by due time: within the last
    # (100 - pct)% of its target counted in wall-clock minutes is not exact, so use the stored warning mark)
    risk_q = _scope(p, select(Ticket).where(active, ~sla.overdue_clause(), select(SlaTimer.id).where(
        SlaTimer.ticket_id == Ticket.id, SlaTimer.is_current.is_(True), SlaTimer.state == "RUNNING",
        SlaTimer.warned_at.isnot(None), SlaTimer.breached_at.is_(None)).exists()), project, team)
    at_risk = list(db.scalars(risk_q.order_by(Ticket.created_at).limit(20)))
    oldest_q = _scope(p, select(Ticket).where(Ticket.status_code == "NEW", Ticket.resolver_id.is_(None)), project, team)
    oldest = list(db.scalars(oldest_q.order_by(Ticket.created_at).limit(10)))

    def grouped(col, extra=None):
        q = _scope(p, select(col, func.count()).where(active), project, team)
        return db.execute(q.group_by(col)).all()

    status_names = {s.code: s.name for s in db.scalars(select(TicketStatus))}
    by_status = [{"code": c, "name": status_names.get(c, c), "count": n} for c, n in grouped(Ticket.status_code)]
    by_team = []
    for tid, n in grouped(Ticket.team_id):
        t = ctx.team(tid)
        by_team.append({"id": tid, "name": t.name, "project_id": t.project_id, "count": n})
    by_project = []
    for pid, n in grouped(Ticket.project_id):
        pr = ctx.project(pid)
        by_project.append({"id": pid, "key": pr.key, "name": pr.name, "count": n})
    dues = sla.nearest_due_many(db, [t.id for t in overdue + at_risk + oldest])
    brief = lambda t: view(ctx, p, t, due=dues.get(t.id), with_actions=False)  # noqa: E731
    count = lambda q: db.scalar(select(func.count()).select_from(q.order_by(None).subquery()))  # noqa: E731
    return {
        "overdue": {"count": count(overdue_q), "items": [brief(t) for t in overdue]},
        "at_risk": {"count": count(risk_q), "items": [brief(t) for t in at_risk], "warning_percent": pct},
        "oldest_unassigned": {"count": count(oldest_q), "items": [brief(t) for t in oldest]},
        "by_status": sorted(by_status, key=lambda x: list(status_names).index(x["code"]) if x["code"] in status_names else 99),
        "by_team": sorted(by_team, key=lambda x: -x["count"]),
        "by_project": sorted(by_project, key=lambda x: -x["count"]),
        "generated_at": now,
    }


def system_status(db: Session, p: Principal) -> dict:
    if not p.is_platform_admin:
        raise AppError("forbidden")
    now = clock.now()
    warnings = []
    # calendars without working hours that people or policies use
    empty = list(db.scalars(select(Calendar).where(Calendar.is_active.is_(True), ~select(CalendarHours.id).where(
        CalendarHours.calendar_id == Calendar.id).exists())))
    for c in empty:
        warnings.append({"kind": "calendar_without_hours", "calendar": {"id": c.id, "name": c.name}})
    inactive_used = db.execute(select(Calendar.id, Calendar.name, func.count()).join(
        RoleAssignment, RoleAssignment.calendar_id == Calendar.id).where(
        Calendar.is_active.is_(False), RoleAssignment.is_active.is_(True)).group_by(Calendar.id, Calendar.name)).all()
    for cid, name, n in inactive_used:
        warnings.append({"kind": "inactive_calendar_in_use", "calendar": {"id": cid, "name": name}, "assignments": n})
    for gap in sla.missing_combinations(db, p):
        warnings.append({"kind": "missing_sla_policy", **gap})
    no_policy = db.scalar(select(func.count()).select_from(Ticket).where(Ticket.status_code.in_(ACTIVE),
                                                                         Ticket.sla_policy_id.is_(None)))
    if no_policy:
        warnings.append({"kind": "open_tickets_without_sla", "count": no_policy})
    failed = db.execute(select(NotificationOutbox).where(NotificationOutbox.status == "FAILED")
                        .order_by(NotificationOutbox.id.desc()).limit(20)).scalars().all()
    channels = [{"id": c.id, "name": c.name, "type": c.type, "enabled": c.enabled,
                 "last_failure_at": c.last_failure_at, "last_failure_text": c.last_failure_text}
                for c in db.scalars(select(NotificationChannel).order_by(NotificationChannel.id))]
    return {
        "worker_last_run": jobs.worker_last_run(db),
        "job_backlog": db.scalar(select(func.count()).select_from(Job).where(Job.status == "PENDING",
                                                                             Job.run_at <= now)),
        "jobs_failed": db.scalar(select(func.count()).select_from(Job).where(Job.status == "FAILED")),
        "outbox_pending": db.scalar(select(func.count()).select_from(NotificationOutbox).where(
            NotificationOutbox.status == "PENDING")),
        "failed_sends": {"count": db.scalar(select(func.count()).select_from(NotificationOutbox).where(
            NotificationOutbox.status == "FAILED")),
            "recent": [{"id": o.id, "channel_id": o.channel_id, "attempts": o.attempts, "last_error": o.last_error,
                        "created_at": o.created_at} for o in failed]},
        "channels": channels,
        "warnings": warnings,
        "generated_at": now,
    }
