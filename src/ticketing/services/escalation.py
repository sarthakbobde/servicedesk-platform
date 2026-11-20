"""Escalation (F7, D11, D27) and the SLA warning and breach alerts.

The worker evaluates every unfinished current timer whose step has active rules. A rule fires at most once per
ticket per reopen cycle (`escalation_firing` unique on ticket, rule, cycle); INCREASE_PRIORITY fires at most once
per ticket per cycle across all rules, so a priority change cannot loop. Every firing writes an INTERNAL history
row and any alert, both naming the rule.
"""
from __future__ import annotations

from sqlalchemy import and_, exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .. import audit, clock, settings, worktime
from ..errors import AppError
from ..models import (
    EscalationFiring, EscalationRule, Priority, Project, RoleAssignment, SlaMetric, SlaPolicy, SlaTimer, Team, Ticket,
    TicketEvent, TicketType, User,
)
from ..permissions import Principal, system_principal
from . import configuration as cfg
from . import jobs, notify, routing, sla
from .common import raise_if

TRIGGERS = ("PERCENT_USED", "BREACHED", "MINUTES_AFTER_BREACH", "UNASSIGNED_MINUTES", "ON_HOLD_MINUTES")
ACTIONS = ("NOTIFY", "REASSIGN", "INCREASE_PRIORITY")
TARGETS = ("REQUESTER", "RESOLVER", "TEAM_MANAGERS", "PROJECT_ADMINS", "USER", "TEAM")
DEFAULT_POLICY_TARGETS = ("REQUESTER", "RESOLVER", "TEAM_MANAGERS", "PROJECT_ADMINS")


# ================================================================ rule administration (API-098, API-099)
def _metric_and_policy(db: Session, p: Principal, metric_id):
    m = db.get(SlaMetric, metric_id)
    if m is None or not m.is_active:
        from ..errors import not_found
        raise not_found()
    pol = sla.get_policy(db, p, m.policy_id)  # same rights as the policy; 404 outside scope
    return m, pol


def list_rules(db: Session, p: Principal, metric_id) -> list[EscalationRule]:
    m, _ = _metric_and_policy(db, p, metric_id)
    return list(db.scalars(select(EscalationRule).where(EscalationRule.metric_id == m.id,
                                                        EscalationRule.is_active.is_(True))
                           .order_by(EscalationRule.level)))


def rule_view(r: EscalationRule) -> dict:
    return {"id": r.id, "metric_id": r.metric_id, "name": r.name, "level": r.level, "trigger": r.trigger,
            "threshold": r.threshold, "action": r.action, "target_type": r.target_type, "target_id": r.target_id}


def validate_rules(db: Session, pol: SlaPolicy, rules) -> tuple[list, dict]:
    fields, clean, levels = {}, [], set()
    if not isinstance(rules, list):
        return [], {"rules": "a list"}
    owning_team = None
    project_id = None
    if pol.ticket_type_id:
        tt = db.get(TicketType, pol.ticket_type_id)
        owning_team, project_id = tt.team_id, tt.project_id
    for i, r in enumerate(rules):
        k = f"rules.{i}"
        if not isinstance(r, dict):
            fields[k] = "an object"
            continue
        try:
            level = int(r.get("level"))
            if level < 1:
                raise ValueError
        except (TypeError, ValueError):
            fields[f"{k}.level"] = "1 or more"
            level = None
        if level is not None:
            if level in levels:
                fields[f"{k}.level"] = "one rule per step and level"
            levels.add(level)
        trig, action, target = r.get("trigger"), r.get("action"), r.get("target_type")
        if trig not in TRIGGERS:
            fields[f"{k}.trigger"] = f"one of {', '.join(TRIGGERS)}"
        try:
            threshold = int(r.get("threshold") or 0)
        except (TypeError, ValueError):
            threshold = -1
        if trig == "PERCENT_USED" and not 1 <= threshold <= 1000:
            fields[f"{k}.threshold"] = "percent 1 to 1000"
        elif trig in ("MINUTES_AFTER_BREACH", "UNASSIGNED_MINUTES", "ON_HOLD_MINUTES") and threshold < 1:
            fields[f"{k}.threshold"] = "minutes, 1 or more"
        elif trig == "BREACHED":
            threshold = 0
        if action not in ACTIONS:
            fields[f"{k}.action"] = f"one of {', '.join(ACTIONS)}"
        target_id = r.get("target_id")
        # F7.6 allowed pairs
        if action == "INCREASE_PRIORITY":
            if target not in (None, ""):
                fields[f"{k}.target_type"] = "INCREASE_PRIORITY takes no target"
            target, target_id = None, None
        elif action == "REASSIGN":
            if target != "USER":
                fields[f"{k}.target_type"] = "REASSIGN needs target USER"
        elif action == "NOTIFY" and target not in TARGETS:
            fields[f"{k}.target_type"] = f"one of {', '.join(TARGETS)}"
        if target in ("USER", "TEAM"):
            if pol.ticket_type_id is None:
                fields[f"{k}.target_type"] = "default policies may target REQUESTER, RESOLVER, TEAM_MANAGERS or " \
                                             "PROJECT_ADMINS only"
            elif target_id in (None, ""):
                fields[f"{k}.target_id"] = "required for USER and TEAM"
            elif target == "TEAM":
                team = db.get(Team, target_id)
                if team is None or team.project_id != project_id or not team.is_active:
                    fields[f"{k}.target_id"] = "an active team of the policy's project"
            elif target == "USER":
                user = db.get(User, target_id)
                in_project = user is not None and user.is_active and db.scalar(select(RoleAssignment.id).where(
                    RoleAssignment.user_id == user.id, RoleAssignment.is_active.is_(True),
                    RoleAssignment.project_id == project_id).limit(1))
                if not in_project:
                    fields[f"{k}.target_id"] = "an active person holding a role in the policy's project"
                elif action == "REASSIGN" and not db.scalar(select(RoleAssignment.id).where(
                        RoleAssignment.user_id == user.id, RoleAssignment.team_id == owning_team,
                        RoleAssignment.role == "RESOLVER", RoleAssignment.is_active.is_(True))):
                    fields[f"{k}.target_id"] = "REASSIGN needs an active resolver of the owning team"
        else:
            target_id = None
        name = (str(r.get("name") or "")).strip()[:200] or f"Level {level} {trig or ''} {action or ''}".strip()
        clean.append({"level": level, "trigger": trig, "threshold": threshold, "action": action,
                      "target_type": target, "target_id": int(target_id) if target_id not in (None, "") else None,
                      "name": name})
    return clean, fields


def put_rules(db: Session, p: Principal, metric_id, data) -> list[EscalationRule]:
    m, pol = _metric_and_policy(db, p, metric_id)
    rules = data.get("rules") if isinstance(data, dict) else data
    clean, fields = validate_rules(db, pol, rules)
    raise_if(fields)
    existing = {r.level: r for r in db.scalars(select(EscalationRule).where(EscalationRule.metric_id == m.id))}
    old = [rule_view(r) for r in existing.values() if r.is_active]
    seen = set()
    for c in clean:
        r = existing.get(c["level"])
        seen.add(c["level"])
        if r is None:
            db.add(EscalationRule(metric_id=m.id, is_active=True, **c))
        else:
            for k, v in c.items():
                setattr(r, k, v)
            r.is_active = True
    for level, r in existing.items():
        if level not in seen:
            r.is_active = False
    db.flush()
    org = proj = None
    if pol.ticket_type_id:
        tt = db.get(TicketType, pol.ticket_type_id)
        proj = tt.project_id
        org = db.scalar(select(Project.organization_id).where(Project.id == proj))
    audit.record(db, p.id, "update", "escalation_rules", m.id, {"rules": old}, {"rules": clean}, org, proj)
    return list_rules(db, p, metric_id)


# ================================================================ target resolution (F7.3)
def resolve_targets(db: Session, t: Ticket, rule: EscalationRule) -> list[int]:
    tt = rule.target_type
    if tt == "REQUESTER":
        return [t.requester_id]
    if tt == "RESOLVER":
        return [t.resolver_id] if t.resolver_id else []
    if tt == "TEAM_MANAGERS":
        return [u.id for _, u in routing.team_people(db, t.team_id, roles=("TEAM_MANAGER",))]
    if tt == "PROJECT_ADMINS":
        return list(db.scalars(select(RoleAssignment.user_id).join(User, User.id == RoleAssignment.user_id).where(
            RoleAssignment.project_id == t.project_id, RoleAssignment.role == "PROJECT_ADMIN",
            RoleAssignment.is_active.is_(True), User.is_active.is_(True))))
    if tt == "USER" and rule.target_id:
        ok = db.scalar(select(RoleAssignment.id).where(RoleAssignment.user_id == rule.target_id,
                                                       RoleAssignment.project_id == t.project_id,
                                                       RoleAssignment.is_active.is_(True)).limit(1))
        return [rule.target_id] if ok else []
    if tt == "TEAM" and rule.target_id:
        team = db.get(Team, rule.target_id)
        if team is None or team.project_id != t.project_id:
            return []
        return [u.id for _, u in routing.team_people(db, team.id, roles=("RESOLVER", "TEAM_MANAGER"))]
    return []


def _staff_recipients(db: Session, t: Ticket) -> list[int]:
    if t.resolver_id:
        return [t.resolver_id]
    return [u.id for _, u in routing.team_people(db, t.team_id, roles=("TEAM_MANAGER",))]


# ================================================================ evaluation (F7.4)
def _entered_status_at(db: Session, t: Ticket, statuses) -> object:
    return db.scalar(select(TicketEvent.created_at).where(TicketEvent.ticket_id == t.id,
                                                          TicketEvent.to_status.in_(statuses))
                     .order_by(TicketEvent.created_at.desc(), TicketEvent.id.desc()).limit(1))


def trigger_met(db: Session, t: Ticket, tm: SlaTimer, rule: EscalationRule, spec, now) -> bool:
    used = sla.used_seconds(spec, tm, now)
    target = sla.target_seconds(tm)
    if rule.trigger == "PERCENT_USED":
        return tm.state == "RUNNING" and used * 100 >= rule.threshold * target
    if rule.trigger == "BREACHED":
        return tm.breached_at is not None
    if rule.trigger == "MINUTES_AFTER_BREACH":
        return tm.breached_at is not None and used - target >= rule.threshold * 60
    if rule.trigger == "UNASSIGNED_MINUTES":
        if t.status_code != "NEW" or t.resolver_id is not None:
            return False
        since = _entered_status_at(db, t, ("NEW",)) or t.created_at
        return worktime.working_minutes(spec, since, now) >= rule.threshold
    if rule.trigger == "ON_HOLD_MINUTES":
        if t.status_code != "ON_HOLD":
            return False
        since = _entered_status_at(db, t, ("ON_HOLD",))
        return since is not None and worktime.working_minutes(spec, since, now) >= rule.threshold
    return False


def _record_firing(db: Session, t: Ticket, rule: EscalationRule, now) -> bool:
    new_id = db.scalar(insert(EscalationFiring).values(ticket_id=t.id, rule_id=rule.id, cycle=t.sla_cycle,
                                                       fired_at=now)
                       .on_conflict_do_nothing(constraint="uq_firing_once").returning(EscalationFiring.id))
    return new_id is not None


def _priority_raised_this_cycle(db: Session, t: Ticket) -> bool:
    return bool(db.scalar(select(exists().where(
        EscalationFiring.ticket_id == t.id, EscalationFiring.cycle == t.sla_cycle,
        EscalationFiring.rule_id == EscalationRule.id, EscalationRule.action == "INCREASE_PRIORITY"))))


def fire(db: Session, t: Ticket, tm: SlaTimer, rule: EscalationRule, now) -> bool:
    from . import tickets
    if rule.action == "INCREASE_PRIORITY" and _priority_raised_this_cycle(db, t):
        return False
    if not _record_firing(db, t, rule, now):
        return False
    outcome = {"rule": rule.name, "level": rule.level, "trigger": rule.trigger, "action": rule.action,
               "step": tm.name}
    sysp = system_principal(db)
    if rule.action == "NOTIFY":
        recipients = resolve_targets(db, t, rule)
        notify.notify(db, "ESCALATION", t, recipients, rule_id=rule.id, extra={"rule": rule.name})
        outcome["notified"] = recipients
    elif rule.action == "REASSIGN":
        target = rule.target_id
        team_ok = any(u.id == target for _, u in routing.team_people(db, t.team_id))
        if not team_ok or target == t.resolver_id:
            outcome["skipped"] = "target is not an active resolver of the team" if not team_ok else "already assigned"
        elif t.status_code == "NEW":
            tickets.apply_assignment(db, t, target, None, "assigned", mode="escalation")
        elif t.status_code in ("ASSIGNED", "IN_PROGRESS", "ON_HOLD", "AWAITING_REQUESTER"):
            tickets.apply_assignment(db, t, target, None, "reassigned", note_text=f"Escalation rule {rule.name}")
        outcome["resolver_id"] = target
    elif rule.action == "INCREASE_PRIORITY":
        cur = db.get(Priority, t.priority_id)
        higher = db.scalar(select(Priority).where(Priority.rank < cur.rank, Priority.is_active.is_(True))
                           .order_by(Priority.rank.desc()).limit(1))
        if higher is None:
            outcome["skipped"] = "already at the highest priority"
        else:
            tickets.change_priority(db, t, higher, None, rule_id=rule.id)
            outcome["priority"] = higher.name
    tickets.add_event(db, t, "escalation_fired", None, t.status_code, t.status_code, visibility="INTERNAL",
                      rule_id=rule.id, note_text=rule.name, data=outcome)
    return True


@jobs.sweep("escalation")
def escalation_sweep(db: Session) -> int:
    """Evaluate running timers (and paused ones for ON_HOLD_MINUTES) in bulk, without locks; only tickets with a
    rule that is due to fire are locked, re-checked and fired."""
    now = clock.now()
    rules_by_metric: dict[int, list] = {}
    for r in db.scalars(select(EscalationRule).where(EscalationRule.is_active.is_(True))
                        .order_by(EscalationRule.level)):
        rules_by_metric.setdefault(r.metric_id, []).append(r)
    if not rules_by_metric:
        return 0
    rows = db.execute(select(SlaTimer, Ticket).join(Ticket, Ticket.id == SlaTimer.ticket_id).where(
        SlaTimer.is_current.is_(True), SlaTimer.state.in_(("RUNNING", "PAUSED")),
        SlaTimer.metric_id.in_(list(rules_by_metric)))).all()
    fired_keys = set(db.execute(select(EscalationFiring.ticket_id, EscalationFiring.rule_id, EscalationFiring.cycle)
                                .join(Ticket, and_(Ticket.id == EscalationFiring.ticket_id,
                                                   Ticket.sla_cycle == EscalationFiring.cycle))).all())
    cache: dict = {}
    due: dict[int, list] = {}
    for tm, t in rows:
        spec = cfg.calendar_spec(db, t.sla_calendar_id, cache)
        for rule in rules_by_metric.get(tm.metric_id, ()):
            if (t.id, rule.id, t.sla_cycle) in fired_keys:
                continue
            if tm.state == "PAUSED" and rule.trigger != "ON_HOLD_MINUTES":
                continue
            if trigger_met(db, t, tm, rule, spec, now):
                due.setdefault(t.id, []).append((tm.id, rule))
    fired = 0
    for tid, pairs in due.items():
        t = db.scalar(select(Ticket).where(Ticket.id == tid).with_for_update(skip_locked=True))
        if t is None:
            continue
        spec = cfg.calendar_spec(db, t.sla_calendar_id, cache)
        for tm_id, rule in pairs:
            tm = db.get(SlaTimer, tm_id)
            db.refresh(tm)
            if not tm.is_current or tm.state not in ("RUNNING", "PAUSED"):
                continue  # replaced by a priority change or stopped meanwhile
            if trigger_met(db, t, tm, rule, spec, now) and fire(db, t, tm, rule, now):
                fired += 1
        db.flush()
    return fired


@jobs.sweep("sla_warning")
def warning_sweep(db: Session) -> int:
    """SLA_WARNING once per timer when it has used `sla.warning_percent` of its time."""
    now = clock.now()
    pct = settings.get(db, "sla.warning_percent")
    n = 0
    cache: dict = {}
    for tm in db.scalars(select(SlaTimer).where(SlaTimer.is_current.is_(True), SlaTimer.state == "RUNNING",
                                                SlaTimer.warned_at.is_(None), SlaTimer.breached_at.is_(None))
                         .with_for_update(skip_locked=True)):
        t = db.get(Ticket, tm.ticket_id)
        spec = cfg.calendar_spec(db, t.sla_calendar_id, cache)
        if sla.used_seconds(spec, tm, now) * 100 >= pct * sla.target_seconds(tm):
            tm.warned_at = now
            notify.notify(db, "SLA_WARNING", t, _staff_recipients(db, t))
            n += 1
    return n


def breach_alert(db: Session, t: Ticket, tm: SlaTimer) -> None:
    notify.notify(db, "SLA_BREACH", t, _staff_recipients(db, t))


if breach_alert not in sla.BREACH_HOOKS:
    sla.BREACH_HOOKS.append(breach_alert)
