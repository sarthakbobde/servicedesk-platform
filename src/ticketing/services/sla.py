"""SLA policies and timers (F6, D9, D23, D28, D29).

Model: every step of the matched policy gets one timer when the SLA starts. All timers of a ticket measure the same
clock — working time on the policy's calendar since the SLA start, excluding pauses — and each compares it with its
own cumulative target (plus manual extensions). A timer is RUNNING or PAUSED until it STOPS (its stop status or a
later status on the working path was reached) or is SKIPPED (rejected or withdrawn first). The outcome is MET when
it stopped before it was due, BREACHED when the due time passed while it was unfinished (`breached_at` set once).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, exists, false, func, or_, select, update
from sqlalchemy.orm import Session

from .. import audit, clock, worktime
from ..errors import AppError, not_found
from ..models import (
    Calendar, Priority, Project, SlaAdjustment, SlaMetric, SlaPolicy, SlaTimer, Ticket, TicketType,
)
from ..permissions import Principal
from . import configuration as cfg
from . import jobs
from .common import get_or_404, paginate, raise_if, text_field

WORKING_PATH = ("NEW", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "CLOSED")
PAUSED_STATUSES = ("ON_HOLD", "AWAITING_REQUESTER")
STOP_CHOICES = ("ASSIGNED", "IN_PROGRESS", "RESOLVED")
UNFINISHED = ("RUNNING", "PAUSED")


# ================================================================ policy match (F6.1)
def match_policy(db: Session, priority_id: int, type_id: int) -> SlaPolicy | None:
    pol = db.scalar(select(SlaPolicy).where(SlaPolicy.priority_id == priority_id, SlaPolicy.ticket_type_id == type_id,
                                            SlaPolicy.is_active.is_(True)))
    if pol is None:
        pol = db.scalar(select(SlaPolicy).where(SlaPolicy.priority_id == priority_id,
                                                SlaPolicy.ticket_type_id.is_(None), SlaPolicy.is_active.is_(True)))
    return pol


def policy_metrics(db: Session, policy_id: int) -> list[SlaMetric]:
    return list(db.scalars(select(SlaMetric).where(SlaMetric.policy_id == policy_id, SlaMetric.is_active.is_(True))
                           .order_by(SlaMetric.position)))


def sla_calendar_id(db: Session, t: Ticket) -> int | None:
    return t.sla_calendar_id


def _spec(db: Session, t: Ticket, cache: dict | None = None) -> worktime.CalendarSpec:
    return cfg.calendar_spec(db, t.sla_calendar_id, cache)


def current_timers(db: Session, t: Ticket, lock: bool = False) -> list[SlaTimer]:
    q = select(SlaTimer).where(SlaTimer.ticket_id == t.id, SlaTimer.is_current.is_(True)).order_by(SlaTimer.position)
    if lock:
        q = q.with_for_update()
    return list(db.scalars(q))


# ================================================================ timer arithmetic
def target_seconds(tm: SlaTimer) -> int:
    return (tm.minutes_target + (tm.extension_minutes or 0)) * 60


def used_seconds(spec, tm: SlaTimer, now: datetime) -> int:
    used = tm.seconds_used or 0
    if tm.state == "RUNNING" and tm.running_since is not None:
        used += worktime.working_seconds(spec, tm.running_since, now)
    return used


def _accumulate(spec, tm: SlaTimer, now: datetime):
    if tm.state == "RUNNING" and tm.running_since is not None:
        tm.seconds_used = (tm.seconds_used or 0) + worktime.working_seconds(spec, tm.running_since, now)
        tm.running_since = now


def _recompute_due(spec, tm: SlaTimer):
    if tm.state != "RUNNING":
        tm.due_at = None
        return
    remaining = target_seconds(tm) - (tm.seconds_used or 0)
    if remaining > 0:
        tm.due_at = worktime.add_working_seconds(spec, tm.running_since, remaining)
    elif tm.due_at is None:
        tm.due_at = tm.running_since


def _mark_breach(db: Session, t: Ticket, tm: SlaTimer, at: datetime):
    """Set breached_at once and write the history row (F6.6). Phase 4 sends the breach alert from the sweep."""
    if tm.breached_at is not None:
        return False
    tm.breached_at = at
    from .tickets import add_event
    add_event(db, t, "sla_breached", None, t.status_code, t.status_code, visibility="INTERNAL",
              data={"step": tm.name, "position": tm.position, "due_at": at.isoformat()})
    return True


def _check_overdue_now(db, t, spec, tm, now):
    """When a timer pauses or stops after its target was used up, the breach is recorded at the due time."""
    if tm.breached_at is None and (tm.seconds_used or 0) >= target_seconds(tm):
        _mark_breach(db, t, tm, tm.due_at or now)


def is_paused(t: Ticket) -> bool:
    return t.status_code in PAUSED_STATUSES or bool(t.manual_paused)


def _pause(db, t, spec, tm, now, reason):
    _accumulate(spec, tm, now)
    _check_overdue_now(db, t, spec, tm, now)
    tm.state, tm.running_since, tm.due_at = "PAUSED", None, None
    tm.paused_reason, tm.paused_since = reason, now


def _run(spec, tm, now):
    tm.state, tm.running_since = "RUNNING", now
    tm.paused_reason = tm.paused_since = None
    _recompute_due(spec, tm)


def _stop(db, t, spec, tm, now, state="STOPPED"):
    _accumulate(spec, tm, now)
    if state == "STOPPED":
        _check_overdue_now(db, t, spec, tm, now)
    tm.state, tm.stopped_at, tm.running_since = state, now, None
    tm.paused_reason = tm.paused_since = None


def reached(status: str, stop_status: str) -> bool:
    if status not in WORKING_PATH or stop_status not in WORKING_PATH:
        return False
    return WORKING_PATH.index(status) >= WORKING_PATH.index(stop_status)


def _sync_pause(db, t, spec, timers, now):
    paused = is_paused(t)
    reason = "MANUAL" if t.manual_paused and t.status_code not in PAUSED_STATUSES else "STATUS"
    for tm in timers:
        if tm.state == "RUNNING" and paused:
            _pause(db, t, spec, tm, now, reason)
        elif tm.state == "PAUSED" and not paused:
            _run(spec, tm, now)
        elif tm.state == "PAUSED" and paused:
            tm.paused_reason = reason


# ================================================================ lifecycle hooks
def start(db: Session, t: Ticket, at: datetime | None = None):
    """Start the SLA for the ticket's current cycle (creation and reopen)."""
    now = at or clock.now()
    pol = match_policy(db, t.priority_id, t.type_id)
    t.sla_started_at = now
    t.manual_paused, t.manual_pause_reason = False, None
    if pol is None:
        t.sla_policy_id = None
        t.sla_calendar_id = cfg.default_calendar(db).id if cfg.default_calendar(db) else None
        return []
    t.sla_policy_id = pol.id
    t.sla_calendar_id = pol.calendar_id or (cfg.default_calendar(db).id if cfg.default_calendar(db) else None)
    spec = _spec(db, t)
    timers = []
    for m in policy_metrics(db, pol.id):
        tm = SlaTimer(ticket_id=t.id, metric_id=m.id, cycle=t.sla_cycle, position=m.position, name=m.name,
                      stop_status_code=m.stop_status_code, minutes_target=m.minutes, extension_minutes=0,
                      state="RUNNING", seconds_used=0, running_since=now, is_current=True)
        _recompute_due(spec, tm)
        db.add(tm)
        timers.append(tm)
    db.flush()
    return timers


def on_created(db: Session, ticket: Ticket) -> None:
    start(db, ticket, ticket.created_at)


def on_transition(db: Session, t: Ticket, old: str, new: str, action: str) -> None:
    now = clock.now()
    if action == "reopen":  # F6.8: reopen restarts all
        db.execute(update(SlaTimer).where(SlaTimer.ticket_id == t.id, SlaTimer.is_current.is_(True))
                   .values(is_current=False))
        start(db, t, now)
        return
    timers = current_timers(db, t, lock=True)
    if not timers:
        return
    spec = _spec(db, t)
    if new in ("REJECTED", "WITHDRAWN"):  # F6.5: never stop; unreached steps are skipped
        for tm in timers:
            if tm.state in UNFINISHED:
                _stop(db, t, spec, tm, now, state="SKIPPED")
        return
    if action == "release":  # F6.8: re-arm steps no longer reached
        ref = next((tm for tm in timers if tm.state in UNFINISHED), None)
        clock_used = used_seconds(spec, ref, now) if ref else None
        for tm in timers:
            if tm.state == "STOPPED" and not reached(new, tm.stop_status_code):
                if clock_used is None:
                    clock_used = tm.seconds_used or 0
                tm.state, tm.stopped_at = "RUNNING", None
                tm.seconds_used, tm.running_since = clock_used, now
                _recompute_due(spec, tm)
    for tm in timers:
        if tm.state in UNFINISHED and reached(new, tm.stop_status_code):
            _stop(db, t, spec, tm, now)
    _sync_pause(db, t, spec, [tm for tm in timers if tm.state in UNFINISHED], now)


def on_priority_change(db: Session, t: Ticket, old_priority_id: int) -> None:
    """D28: replace the timers under the new priority's policy, counted from the original SLA start; steps already
    MET stay MET."""
    now = clock.now()
    old = current_timers(db, t, lock=True)
    spec_old = _spec(db, t)
    ref = next((tm for tm in old if tm.state in UNFINISHED), None)
    if ref is not None:
        clock_used = used_seconds(spec_old, ref, now)
    elif t.sla_started_at:
        clock_used = worktime.working_seconds(spec_old, t.sla_started_at, now)
    else:
        clock_used = 0
    met = {tm.position: tm for tm in old if tm.state == "STOPPED" and tm.breached_at is None}
    for tm in old:
        if tm.state in UNFINISHED:
            _accumulate(spec_old, tm, now)
        tm.is_current = False
    db.flush()
    pol = match_policy(db, t.priority_id, t.type_id)
    if pol is None:
        t.sla_policy_id = None
        return
    t.sla_policy_id = pol.id
    t.sla_calendar_id = pol.calendar_id or (cfg.default_calendar(db).id if cfg.default_calendar(db) else None)
    spec = _spec(db, t)
    paused = is_paused(t)
    for m in policy_metrics(db, pol.id):
        tm = SlaTimer(ticket_id=t.id, metric_id=m.id, cycle=t.sla_cycle, position=m.position, name=m.name,
                      stop_status_code=m.stop_status_code, minutes_target=m.minutes, extension_minutes=0,
                      is_current=True, seconds_used=clock_used)
        prev = met.get(m.position)
        if prev is not None:  # stays MET
            tm.state, tm.stopped_at, tm.seconds_used, tm.due_at = "STOPPED", prev.stopped_at, prev.seconds_used, prev.due_at
        elif reached(t.status_code, m.stop_status_code):
            tm.state, tm.stopped_at = "STOPPED", now
            if clock_used >= target_seconds(tm):
                tm.breached_at = now
        elif paused:
            tm.state = "PAUSED"
            tm.paused_reason = "MANUAL" if t.manual_paused and t.status_code not in PAUSED_STATUSES else "STATUS"
            tm.paused_since = now
            if clock_used >= target_seconds(tm):
                tm.breached_at = now
        else:
            tm.state, tm.running_since = "RUNNING", now
            _recompute_due(spec, tm)
        db.add(tm)
    db.flush()
    for tm in current_timers(db, t):
        if tm.breached_at == now:
            from .tickets import add_event
            add_event(db, t, "sla_breached", None, t.status_code, t.status_code, visibility="INTERNAL",
                      data={"step": tm.name, "position": tm.position, "reason": "priority change"})


# ================================================================ manual actions (F6.7)
def action_available(db: Session, t: Ticket, action: str) -> bool:
    timers = current_timers(db, t)
    unfinished = [tm for tm in timers if tm.state in UNFINISHED]
    if action == "sla-pause":
        return bool(unfinished) and not t.manual_paused
    if action == "sla-resume":
        return bool(t.manual_paused)
    if action == "sla-extend":
        return bool(unfinished)
    return False


def manual_action(db: Session, p: Principal, t: Ticket, action: str, body: dict) -> None:
    from .tickets import add_event, note
    now = clock.now()
    timers = current_timers(db, t, lock=True)
    unfinished = [tm for tm in timers if tm.state in UNFINISHED]
    spec = _spec(db, t)
    fields = {}
    if action == "sla-pause":
        reason = note(fields, body, "reason")
        raise_if(fields)
        if t.manual_paused or not unfinished:
            raise AppError("invalid_state", "Already manually paused, or no running step.")
        t.manual_paused, t.manual_pause_reason = True, reason
        _sync_pause(db, t, spec, unfinished, now)
        for tm in unfinished:
            if t.status_code not in PAUSED_STATUSES:
                tm.paused_reason = "MANUAL"
        db.add(SlaAdjustment(ticket_id=t.id, kind="PAUSE", reason=reason, by_id=p.id, at=now))
        add_event(db, t, "sla_paused", p.id, t.status_code, t.status_code, note_text=reason, visibility="INTERNAL")
    elif action == "sla-resume":
        if not t.manual_paused:
            raise AppError("invalid_state", "No manual pause is active.")
        t.manual_paused, t.manual_pause_reason = False, None
        _sync_pause(db, t, spec, unfinished, now)
        db.add(SlaAdjustment(ticket_id=t.id, kind="RESUME", by_id=p.id, at=now))
        add_event(db, t, "sla_resumed", p.id, t.status_code, t.status_code, visibility="INTERNAL")
    elif action == "sla-extend":
        reason = note(fields, body, "reason")
        try:
            minutes = int(body.get("minutes"))
            if not 1 <= minutes <= 10_000:
                raise ValueError
        except (TypeError, ValueError):
            fields["minutes"] = "1 to 10,000"
        try:
            position = int(body.get("step"))
        except (TypeError, ValueError):
            fields["step"] = "the step position"
        raise_if(fields)
        tm = next((x for x in timers if x.position == position), None)
        if tm is None:
            raise AppError("validation_failed", fields={"step": "no such step"})
        if tm.state not in UNFINISHED:
            raise AppError("invalid_state", "That step is finished.")
        _accumulate(spec, tm, now)
        tm.extension_minutes = (tm.extension_minutes or 0) + minutes
        _recompute_due(spec, tm)
        db.add(SlaAdjustment(ticket_id=t.id, kind="EXTEND", position=position, minutes=minutes, reason=reason,
                             by_id=p.id, at=now))
        add_event(db, t, "sla_extended", p.id, t.status_code, t.status_code, note_text=reason, visibility="INTERNAL",
                  data={"step": tm.name, "position": position, "minutes": minutes})
    else:
        raise not_found()


# ================================================================ reading (G11, lists)
def display_state(tm: SlaTimer) -> str:
    if tm.state == "SKIPPED":
        return "SKIPPED"
    if tm.state == "STOPPED":
        return "BREACHED" if tm.breached_at else "MET"
    if tm.breached_at:
        return "BREACHED"
    return tm.state


def timer_view(db: Session, t: Ticket, tm: SlaTimer, spec=None, tz: str | None = None) -> dict:
    now = clock.now()
    spec = spec or _spec(db, t)
    used = used_seconds(spec, tm, now) if tm.state in UNFINISHED else (tm.seconds_used or 0)
    total = tm.minutes_target + (tm.extension_minutes or 0)
    return {"step": tm.name, "position": tm.position, "due_at": tm.due_at,
            "team_time_zone": tz or cfg.calendar_time_zone(db, t.sla_calendar_id), "state": display_state(tm),
            "minutes_total": total, "minutes_target": tm.minutes_target,
            "minutes_extended": tm.extension_minutes or 0, "minutes_used": used // 60,
            "minutes_left": max(0, total - used // 60) if tm.state in UNFINISHED else None,
            "breached_at": tm.breached_at, "stopped_at": tm.stopped_at,
            "paused_reason": tm.paused_reason if tm.state == "PAUSED" else None,
            "percent_used": round(100 * used / max(1, total * 60))}


def read_timers(db: Session, p: Principal, t: Ticket) -> dict:
    if p.via == "CREDENTIAL" or not p.is_staff_on(t):
        raise AppError("forbidden", "SLA timers are for staff only.")
    timers = current_timers(db, t)
    spec = _spec(db, t)
    tz = cfg.calendar_time_zone(db, t.sla_calendar_id)
    return {"has_policy": t.sla_policy_id is not None, "manual_pause": t.manual_paused,
            "manual_pause_reason": t.manual_pause_reason, "time_zone": tz,
            "items": [timer_view(db, t, tm, spec, tz) for tm in timers]}


def _nearest(timers: list[SlaTimer]) -> SlaTimer | None:
    open_ = [tm for tm in timers if tm.state in UNFINISHED]
    running = sorted([tm for tm in open_ if tm.state == "RUNNING" and tm.due_at], key=lambda x: x.due_at)
    if running:
        return running[0]
    return open_[0] if open_ else None


def nearest_due(db: Session, ticket: Ticket) -> dict | None:
    tm = _nearest(current_timers(db, ticket))
    if tm is None:
        return None
    return {"step": tm.name, "position": tm.position, "due_at": tm.due_at, "state": display_state(tm),
            "time_zone": cfg.calendar_time_zone(db, ticket.sla_calendar_id)}


def nearest_due_many(db: Session, ticket_ids) -> dict:
    if not ticket_ids:
        return {}
    by_ticket: dict[int, list] = {}
    for tm in db.scalars(select(SlaTimer).where(SlaTimer.ticket_id.in_(ticket_ids), SlaTimer.is_current.is_(True),
                                                SlaTimer.state.in_(UNFINISHED))):
        by_ticket.setdefault(tm.ticket_id, []).append(tm)
    cal = dict(db.execute(select(Ticket.id, Ticket.sla_calendar_id).where(Ticket.id.in_(ticket_ids))).all())
    tz_cache = {}
    out = {}
    for tid in ticket_ids:
        tm = _nearest(by_ticket.get(tid, []))
        if tm is None:
            out[tid] = None
            continue
        cid = cal.get(tid)
        if cid not in tz_cache:
            tz_cache[cid] = cfg.calendar_time_zone(db, cid)
        out[tid] = {"step": tm.name, "position": tm.position, "due_at": tm.due_at, "state": display_state(tm),
                    "time_zone": tz_cache[cid]}
    return out


def overdue_clause():
    now = clock.now()
    return exists().where(SlaTimer.ticket_id == Ticket.id, SlaTimer.is_current.is_(True),
                          SlaTimer.state.in_(UNFINISHED),
                          or_(SlaTimer.breached_at.isnot(None),
                              and_(SlaTimer.state == "RUNNING", SlaTimer.due_at <= now)))


# ================================================================ worker: breaches (F6.6)
@jobs.sweep("sla_breach")
def breach_sweep(db: Session) -> int:
    now = clock.now()
    rows = list(db.scalars(select(SlaTimer).where(SlaTimer.is_current.is_(True), SlaTimer.state == "RUNNING",
                                                  SlaTimer.breached_at.is_(None), SlaTimer.due_at <= now)
                           .with_for_update(skip_locked=True)))
    n = 0
    for tm in rows:
        t = db.get(Ticket, tm.ticket_id)
        if _mark_breach(db, t, tm, tm.due_at):
            n += 1
            on_breach(db, t, tm)
    return n


def on_breach(db: Session, t: Ticket, tm: SlaTimer) -> None:
    """Phase 4 attaches the breach alert here."""
    hook = BREACH_HOOKS
    for fn in hook:
        fn(db, t, tm)


BREACH_HOOKS: list = []


# ================================================================ policy administration (API-092..097)
def _can_manage(p: Principal, db: Session, pol_type_id: int | None) -> bool:
    if pol_type_id is None:
        return p.is_platform_admin
    tt = db.get(TicketType, pol_type_id)
    return tt is not None and p.admin_of_project(tt.project_id)


def list_policies(db: Session, p: Principal, priority_id=None, type_id=None, project_id=None, active=None,
                  defaults=None, page=1, page_size=100):
    if not p.any_admin():
        raise AppError("forbidden")
    q = select(SlaPolicy)
    if not p.is_platform_admin:
        ids = p.admin_project_ids() or {-1}
        q = q.where(SlaPolicy.ticket_type_id.in_(select(TicketType.id).where(TicketType.project_id.in_(ids))))
    if priority_id:
        q = q.where(SlaPolicy.priority_id == int(priority_id))
    if type_id:
        q = q.where(SlaPolicy.ticket_type_id == int(type_id))
    if project_id:
        q = q.where(SlaPolicy.ticket_type_id.in_(select(TicketType.id).where(TicketType.project_id == int(project_id))))
    if defaults is not None:
        q = q.where(SlaPolicy.ticket_type_id.is_(None) if defaults else SlaPolicy.ticket_type_id.isnot(None))
    if active is not None:
        q = q.where(SlaPolicy.is_active.is_(active))
    return paginate(db, q, page, page_size, None, None, [SlaPolicy.priority_id, SlaPolicy.id])


def missing_combinations(db: Session, p: Principal) -> list[dict]:
    """S25: priority x type combinations (in scope) with neither a type policy nor a default policy."""
    if not p.any_admin():
        raise AppError("forbidden")
    prios = cfg.list_priorities(db, active_only=True)
    q = select(TicketType).where(TicketType.is_active.is_(True))
    ids = p.admin_project_ids()
    if ids is not None:
        q = q.where(TicketType.project_id.in_(ids or {-1}))
    types = list(db.scalars(q.order_by(TicketType.project_id, TicketType.name)))
    pols = {(x.priority_id, x.ticket_type_id) for x in db.scalars(select(SlaPolicy).where(SlaPolicy.is_active.is_(True)))}
    out = []
    for tt in types:
        for pr in prios:
            if (pr.id, tt.id) not in pols and (pr.id, None) not in pols:
                out.append({"priority": {"id": pr.id, "name": pr.name},
                            "type": {"id": tt.id, "name": tt.name, "project_id": tt.project_id}})
    return out


def get_policy(db: Session, p: Principal, pid) -> SlaPolicy:
    pol = get_or_404(db, SlaPolicy, pid)
    if not _can_manage(p, db, pol.ticket_type_id):
        raise not_found()
    return pol


def _policy_scope(db, pol):
    if pol.ticket_type_id is None:
        return None, None
    tt = db.get(TicketType, pol.ticket_type_id)
    proj = db.get(Project, tt.project_id)
    return proj.organization_id, proj.id


def create_policy(db: Session, p: Principal, data: dict) -> SlaPolicy:
    if not p.any_admin():
        raise AppError("forbidden")
    fields = {}
    type_id = data.get("ticket_type_id") or None
    if not _can_manage(p, db, type_id):
        raise AppError("forbidden", "Default policies are managed by platform admins; type policies by the "
                       "project's admins." if type_id is None else None)
    name = text_field(fields, data, "name")
    prio = db.get(Priority, data.get("priority_id")) if data.get("priority_id") else None
    if prio is None or not prio.is_active:
        fields["priority_id"] = "an active priority"
    cal_id = data.get("calendar_id") or None
    if cal_id:
        cal = db.get(Calendar, cal_id)
        if cal is None or not cal.is_active:
            fields["calendar_id"] = "unknown or inactive calendar"
    raise_if(fields)
    if db.scalar(select(SlaPolicy.id).where(SlaPolicy.priority_id == prio.id, SlaPolicy.is_active.is_(True),
                                            SlaPolicy.ticket_type_id == type_id if type_id
                                            else SlaPolicy.ticket_type_id.is_(None))):
        raise AppError("validation_failed", fields={"priority_id": "one policy per priority and type"})
    pol = SlaPolicy(name=name, priority_id=prio.id, ticket_type_id=type_id, calendar_id=cal_id, is_active=True)
    db.add(pol)
    db.flush()
    org, proj = _policy_scope(db, pol)
    audit.record(db, p.id, "create", "sla_policy", pol.id, None, audit.snapshot(pol), org, proj)
    return pol


def update_policy(db: Session, p: Principal, pid, data: dict) -> SlaPolicy:
    pol = get_policy(db, p, pid)
    old = audit.snapshot(pol)
    fields = {}
    if "name" in data:
        name = text_field(fields, data, "name")
        if name:
            pol.name = name
    if "is_active" in data:
        v = data.get("is_active")
        if not isinstance(v, bool):
            fields["is_active"] = "true or false"
        elif v and not pol.is_active and db.scalar(select(SlaPolicy.id).where(
                SlaPolicy.priority_id == pol.priority_id, SlaPolicy.is_active.is_(True), SlaPolicy.id != pol.id,
                SlaPolicy.ticket_type_id == pol.ticket_type_id if pol.ticket_type_id
                else SlaPolicy.ticket_type_id.is_(None))):
            fields["is_active"] = "another active policy exists for this priority and type"
        else:
            pol.is_active = v
    for locked in ("priority_id", "ticket_type_id", "calendar_id"):
        if locked in data:
            fields[locked] = "cannot change; create a new policy instead"
    raise_if(fields)
    org, proj = _policy_scope(db, pol)
    audit.record(db, p.id, "update", "sla_policy", pol.id, old, audit.snapshot(pol), org, proj)
    return pol


def metric_view(m: SlaMetric) -> dict:
    return {"id": m.id, "position": m.position, "name": m.name, "minutes": m.minutes,
            "stop_status_code": m.stop_status_code}


def validate_steps(steps) -> tuple[list, dict]:
    fields, clean = {}, []
    if not isinstance(steps, list) or not steps:
        return [], {"steps": "at least one step"}
    prev = 0
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            fields[f"steps.{i}"] = "an object"
            continue
        name = (str(s.get("name") or "")).strip()
        if not name or len(name) > 100:
            fields[f"steps.{i}.name"] = "1 to 100 characters"
        try:
            minutes = int(s.get("minutes"))
        except (TypeError, ValueError):
            minutes = None
        if minutes is None or minutes <= 0:
            fields[f"steps.{i}.minutes"] = "above zero"
        elif minutes < prev:
            fields[f"steps.{i}.minutes"] = "minutes are cumulative and never decrease (D29)"
        else:
            prev = minutes
        stop = s.get("stop_status_code")
        if stop not in STOP_CHOICES:
            fields[f"steps.{i}.stop_status_code"] = f"one of {', '.join(STOP_CHOICES)}"
        clean.append({"position": i + 1, "name": name, "minutes": minutes, "stop_status_code": stop})
    if clean and clean[-1]["stop_status_code"] != "RESOLVED":
        fields["steps"] = "the last step (the total) must stop at RESOLVED"
    return clean, fields


def put_metrics(db: Session, p: Principal, pid, data) -> list[SlaMetric]:
    """Replace the steps as a whole. Steps are matched by position so escalation rules stay attached."""
    pol = get_policy(db, p, pid)
    steps = data.get("steps") if isinstance(data, dict) else data
    if isinstance(steps, list):
        steps = sorted(steps, key=lambda s: (s.get("position") or 0) if isinstance(s, dict) else 0)
    clean, fields = validate_steps(steps)
    raise_if(fields)
    existing = {m.position: m for m in db.scalars(select(SlaMetric).where(SlaMetric.policy_id == pol.id))}
    old = [metric_view(m) for m in existing.values() if m.is_active]
    for s in clean:
        m = existing.get(s["position"])
        if m is None:
            db.add(SlaMetric(policy_id=pol.id, is_active=True, **s))
        else:
            m.name, m.minutes, m.stop_status_code, m.is_active = s["name"], s["minutes"], s["stop_status_code"], True
    for pos, m in existing.items():
        if pos > len(clean):
            m.is_active = False
    db.flush()
    org, proj = _policy_scope(db, pol)
    audit.record(db, p.id, "update", "sla_metrics", pol.id, {"steps": old}, {"steps": clean}, org, proj)
    return policy_metrics(db, pol.id)
