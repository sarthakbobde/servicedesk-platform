"""Tickets: intake (F3), the lifecycle (F5, A10), allowed_actions, history visibility (A10.4/A10.5), lists and
counts (F10.1-F10.3). One function per action; each runs in the caller's transaction with the ticket row locked."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import Session

from .. import clock, settings, worktime
from ..errors import AppError, not_found
from ..models import (
    IdempotencyKey, Priority, Project, SlaTimer, Team, Ticket, TicketEvent, TicketStatus, TicketType, User,
)
from ..permissions import Principal
from . import configuration as cfg
from . import jobs, notify, routing, sla
from .common import paginate

NEW, ASSIGNED, IN_PROGRESS, ON_HOLD, AWAITING = "NEW", "ASSIGNED", "IN_PROGRESS", "ON_HOLD", "AWAITING_REQUESTER"
RESOLVED, REJECTED, WITHDRAWN, CLOSED = "RESOLVED", "REJECTED", "WITHDRAWN", "CLOSED"
ACTIVE = (NEW, ASSIGNED, IN_PROGRESS, ON_HOLD, AWAITING)
FINISHED = (RESOLVED, REJECTED, WITHDRAWN)
DONE = FINISHED + (CLOSED,)
PAUSED = (ON_HOLD, AWAITING)
WORKING_PATH = (NEW, ASSIGNED, IN_PROGRESS, RESOLVED, CLOSED)  # F6.5

REJECT_CODES = ("WRONG_TEAM", "DUPLICATE", "NOT_A_TICKET", "INFO_MISSING", "OTHER")  # NO_REPLY is system only
REOPEN_BLOCKING = ("WRONG_TEAM", "DUPLICATE")

# Transition table (A10.1/A10.2). Order is the order buttons are offered.
FROM = {
    "claim": (NEW,),
    "assign": (NEW,),
    "start": (ASSIGNED,),
    "hold": (ASSIGNED, IN_PROGRESS),
    "request-info": (ASSIGNED, IN_PROGRESS),
    "resume": PAUSED,
    "respond": (AWAITING,),
    "resolve": (ASSIGNED, IN_PROGRESS),
    "reject": ACTIVE,
    "withdraw": (NEW, ASSIGNED, ON_HOLD, AWAITING),
    "release": (ASSIGNED, IN_PROGRESS, ON_HOLD, AWAITING),
    "reassign": (ASSIGNED, IN_PROGRESS, ON_HOLD, AWAITING),
    "reopen": FINISHED,
    "priority": ACTIVE,
    "estimate": ACTIVE,
    "sla-pause": ACTIVE,
    "sla-resume": ACTIVE,
    "sla-extend": ACTIVE,
}
ACTIONS = tuple(FROM)
REQUESTER_ACTIONS = ("respond", "withdraw", "reopen")
SLA_ACTIONS = ("sla-pause", "sla-resume", "sla-extend")
CHILD_BLOCKED = ("resolve", "reject", "withdraw")  # a parent finishes only after its children
TEXT_MAX = 2000


# ================================================================ helpers
def note(fields: dict, body: dict, name: str, required: bool = True) -> str | None:
    v = body.get(name)
    if v is None or not str(v).strip():
        if required:
            fields[name] = "required"
        return None
    v = str(v).strip()
    if len(v) > TEXT_MAX:
        fields[name] = f"at most {TEXT_MAX} characters"
        return None
    return v


def raise_fields(fields):
    if fields:
        raise AppError("validation_failed", fields=fields)


def add_event(db: Session, t: Ticket, type_: str, actor_id: int | None, from_status: str | None = None,
              to_status: str | None = None, note_text: str | None = None, note_public: bool = False,
              visibility: str = "PUBLIC", reason_code: str | None = None, rule_id: int | None = None,
              data: dict | None = None) -> TicketEvent:
    ev = TicketEvent(ticket_id=t.id, type=type_, actor_id=actor_id or None, from_status=from_status,
                     to_status=to_status, note=note_text, note_public=note_public, visibility=visibility,
                     reason_code=reason_code, rule_id=rule_id, data=data, created_at=clock.now())
    db.add(ev)
    return ev


def set_status(db: Session, t: Ticket, new: str, action: str):
    old = t.status_code
    t.status_code = new
    t.updated_at = clock.now()
    sla.on_transition(db, t, old, new, action)
    if t.parent_id and (new in FINISHED) != (old in FINISHED) and new != CLOSED:
        parent = db.get(Ticket, t.parent_id)
        key = db.scalar(select(Project.key).where(Project.id == t.project_id))
        add_event(db, parent, "child_finished" if new in FINISHED else "child_reopened", None, parent.status_code,
                  parent.status_code, note_text=f"{key}-{t.number}: {t.title}", visibility="INTERNAL",
                  data={"child_id": t.id, "child_status": new})
    return old


NUMBER_RE = re.compile(r"^([A-Za-z0-9]{2,10})-(\d+)$")


def resolve_ref(db: Session, ref) -> Ticket | None:
    ref = str(ref).strip()
    if ref.isdigit():
        return db.get(Ticket, int(ref))
    m = NUMBER_RE.match(ref)
    if not m:
        return None
    return db.scalar(select(Ticket).join(Project, Project.id == Ticket.project_id).where(
        Project.key == m.group(1).upper(), Ticket.number == int(m.group(2))))


def load(db: Session, p: Principal, ref, lock: bool = False, action: str | None = None) -> Ticket:
    t = resolve_ref(db, ref)
    if t is None:
        raise not_found()
    if lock:
        db.refresh(t, with_for_update=True)  # row lock for the action (F5)
    if not p.can_see(t):
        # Lost a claim race: the ticket left the queue, so the team member no longer sees it.
        if action == "claim" and t.resolver_id and (p.is_resolver(t.team_id) or p.is_manager(t.team_id)):
            raise AppError("already_claimed")
        raise not_found()
    return t


# ================================================================ rights (A6 x F5)
def role_allows(p: Principal, action: str, t: Ticket) -> bool:
    if p.via == "SYSTEM":
        return True
    me = p.id
    own = t.resolver_id == me and p.is_resolver(t.team_id)
    mgr = p.is_manager(t.team_id)
    if action in REQUESTER_ACTIONS:
        return t.requester_id == me
    if action == "claim":
        return p.is_resolver(t.team_id) or mgr
    if action in ("start", "hold", "request-info", "resume", "resolve"):
        return own or mgr
    if action == "reject":
        return p.is_resolver(t.team_id) or mgr
    if action == "release":
        return own or p.manager_or_above(t)
    if action in ("assign", "reassign", "priority", "estimate") + SLA_ACTIONS:
        return p.manager_or_above(t)
    return False


class Ctx:
    """Per-call caches so lists do not query per row."""

    def __init__(self, db: Session):
        self.db = db
        self._types, self._teams, self._projects, self._prio, self._users = {}, {}, {}, {}, {}
        self._settings = {}
        self.statuses = cfg.status_map(db)

    def type(self, tid) -> TicketType:
        if tid not in self._types:
            self._types[tid] = self.db.get(TicketType, tid)
        return self._types[tid]

    def team(self, tid) -> Team:
        if tid not in self._teams:
            self._teams[tid] = self.db.get(Team, tid)
        return self._teams[tid]

    def project(self, pid) -> Project:
        if pid not in self._projects:
            self._projects[pid] = self.db.get(Project, pid)
        return self._projects[pid]

    def priority(self, pid) -> Priority:
        if pid not in self._prio:
            self._prio[pid] = self.db.get(Priority, pid)
        return self._prio[pid]

    def users(self, ids):
        missing = {i for i in ids if i and i not in self._users}
        if missing:
            for u in self.db.scalars(select(User).where(User.id.in_(missing))):
                self._users[u.id] = u
        return self._users

    def user(self, uid) -> User | None:
        return self.users([uid]).get(uid) if uid else None

    def timers(self, ticket_id):
        """Current SLA timers of a ticket, loaded once per call (allowed_actions asks three times)."""
        if not hasattr(self, "_timers"):
            self._timers = {}
        if ticket_id not in self._timers:
            self._timers[ticket_id] = list(self.db.scalars(select(SlaTimer).where(
                SlaTimer.ticket_id == ticket_id, SlaTimer.is_current.is_(True))))
        return self._timers[ticket_id]

    def prefetch_timers(self, ticket_ids):
        if not hasattr(self, "_timers"):
            self._timers = {}
        ids = [i for i in ticket_ids if i not in self._timers]
        for i in ids:
            self._timers[i] = []
        if ids:
            for tm in self.db.scalars(select(SlaTimer).where(SlaTimer.ticket_id.in_(ids),
                                                             SlaTimer.is_current.is_(True))):
                self._timers[tm.ticket_id].append(tm)

    def open_children(self, ticket_id) -> list:
        if not hasattr(self, "_open_children"):
            self._open_children = {}
        if ticket_id not in self._open_children:
            self._open_children[ticket_id] = list(self.db.scalars(select(Ticket).where(
                Ticket.parent_id == ticket_id, Ticket.status_code.in_(ACTIVE)).order_by(Ticket.number)))
        return self._open_children[ticket_id]

    def prefetch_open_children(self, ticket_ids):
        if not hasattr(self, "_open_children"):
            self._open_children = {}
        ids = [i for i in ticket_ids if i not in self._open_children]
        for i in ids:
            self._open_children[i] = []
        if ids:
            for c in self.db.scalars(select(Ticket).where(Ticket.parent_id.in_(ids), Ticket.status_code.in_(ACTIVE))
                                     .order_by(Ticket.number)):
                self._open_children[c.parent_id].append(c)

    def setting(self, key, project_id):
        k = (key, project_id)
        if k not in self._settings:
            self._settings[k] = settings.get(self.db, key, project_id)
        return self._settings[k]


def reopen_problem(ctx: Ctx, t: Ticket) -> str | None:
    """None if reopen is possible now, else the error code."""
    if t.status_code not in FINISHED:
        return "invalid_state"
    if t.reject_reason_code in REOPEN_BLOCKING:
        return "reopen_blocked"
    if not ctx.team(t.team_id).is_active or not ctx.type(t.type_id).is_active:
        return "reopen_blocked"
    days = ctx.setting("ticket.reopen_window_days", t.project_id)
    if t.completed_at is None or clock.now() > t.completed_at + timedelta(days=days):
        return "reopen_window_closed"
    return None


def state_allows(ctx: Ctx, action: str, t: Ticket) -> bool:
    if t.status_code not in FROM[action]:
        return False
    if action == "claim":
        return t.resolver_id is None and t.waiting_for_candidate and ctx.type(t.type_id).assignment_mode == "AUTO"
    if action == "reopen":
        return reopen_problem(ctx, t) is None
    if action in CHILD_BLOCKED and ctx.open_children(t.id):
        return False
    if action in SLA_ACTIONS:
        return sla.action_available(ctx.db, t, action, ctx.timers(t.id))
    return True


def allowed_actions(ctx: Ctx, p: Principal, t: Ticket) -> list[str]:
    if p.via == "CREDENTIAL" or (p.is_product_user):
        candidates_ = REQUESTER_ACTIONS
    else:
        candidates_ = ACTIONS
    return [a for a in candidates_ if role_allows(p, a, t) and state_allows(ctx, a, t)]


# ================================================================ intake (F3)
def _request_hash(data: dict) -> str:
    keys = ("project_id", "type_id", "priority_id", "title", "description")
    canon = json.dumps({k: data.get(k) for k in keys}, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


def caller_key(p: Principal) -> str:
    if p.via == "CREDENTIAL":
        return f"cred:{p.credential.project_id}:{p.user.id}"
    return f"user:{p.user.id}"


def create_ticket(db: Session, p: Principal, data: dict, source: str,
                  idempotency_key: str | None = None) -> tuple[Ticket, bool]:
    """Returns (ticket, created). A repeated Idempotency-Key with the same body returns the original."""
    if idempotency_key:
        idempotency_key = idempotency_key.strip()[:200]
        found = db.scalar(select(IdempotencyKey).where(IdempotencyKey.caller == caller_key(p),
                                                       IdempotencyKey.key == idempotency_key))
        if found is not None and found.created_at >= clock.now() - timedelta(hours=24):
            if found.request_hash != _request_hash(data):
                raise AppError("idempotency_conflict")
            return db.get(Ticket, found.ticket_id), False
        if found is not None:
            db.delete(found)
            db.flush()

    fields = {}
    # project (optional when the caller can raise in exactly one)
    raisable = p.raisable_project_ids()
    pid = data.get("project_id")
    if pid in (None, ""):
        if raisable is not None and len(raisable) == 1:
            pid = next(iter(raisable))
        else:
            fields["project_id"] = "required"
    raise_fields(fields)
    try:
        proj = db.get(Project, int(pid))
    except (TypeError, ValueError):
        proj = None
    if proj is None or not p.can_raise_in(proj):
        raise AppError("validation_failed", fields={"project_id": "unknown project or not allowed to raise there"})
    if not proj.is_active:
        raise AppError("inactive_reference", fields={"project_id": "inactive"})
    try:
        tt = db.get(TicketType, int(data.get("type_id")))
    except (TypeError, ValueError):
        tt = None
    if tt is None:
        fields["type_id"] = "required"
    title = note(fields, data, "title")
    if title and len(title) > 200:
        fields["title"] = "1 to 200 characters"
    desc = data.get("description")
    if desc is None or not str(desc).strip():
        fields["description"] = "required"
    elif len(str(desc)) > 10000:
        fields["description"] = "at most 10,000 characters"
    raise_fields(fields)
    team = db.get(Team, tt.team_id)
    if tt.project_id != proj.id or not tt.is_active or not team.is_active or team.project_id != proj.id:
        raise AppError("inactive_reference", fields={"type_id": "inactive or from another project"})
    # priority (F3.4)
    prio_id = data.get("priority_id")
    if prio_id not in (None, ""):
        if p.is_product_user and not settings.get(db, "product_user.can_set_priority", proj.id):
            raise AppError("forbidden", "Product users of this project may not choose the priority.")
        try:
            prio = db.get(Priority, int(prio_id))
        except (TypeError, ValueError):
            prio = None
        if prio is None or not prio.is_active:
            raise AppError("inactive_reference", fields={"priority_id": "unknown or inactive"})
    else:
        prio = cfg.default_priority(db)
        if prio is None:
            raise AppError("validation_failed", fields={"priority_id": "no default priority is configured"})

    # number (F3.5): per-project sequence under row lock
    locked = db.scalar(select(Project).where(Project.id == proj.id).with_for_update())
    locked.ticket_seq += 1
    now = clock.now()
    t = Ticket(project_id=proj.id, number=locked.ticket_seq, type_id=tt.id, team_id=team.id, priority_id=prio.id,
               status_code=NEW, title=title, description=str(desc).strip(), requester_id=p.id, source=source,
               created_at=now, updated_at=now, sla_cycle=1)
    db.add(t)
    db.flush()
    if idempotency_key:
        db.add(IdempotencyKey(caller=caller_key(p), key=idempotency_key, request_hash=_request_hash(data),
                              ticket_id=t.id, created_at=now))
    add_event(db, t, "created", p.id, None, NEW, data={"source": source})
    notify.notify(db, "ACKNOWLEDGEMENT", t, [t.requester_id])  # sent even when raised by self (F3.8)
    sla.on_created(db, t)
    routing.try_auto_route(db, t)
    db.flush()
    return t, True


# ================================================================ child tickets
def can_add_child(p: Principal, t: Ticket) -> bool:
    """The parent's own resolver, its team's managers and admins of its project split work into child tickets."""
    if p.via == "CREDENTIAL" or p.is_product_user:
        return False
    return (t.resolver_id == p.id and p.is_resolver(t.team_id)) or p.manager_or_above(t)


def children_block(ctx: Ctx, t: Ticket):
    kids = ctx.open_children(t.id)
    if kids:
        key = ctx.project(t.project_id).key
        raise AppError("children_open", "Finish its child tickets first: "
                       + ", ".join(f"{key}-{c.number}" for c in kids) + ".")


def create_child(db: Session, p: Principal, ref, data: dict, source: str) -> Ticket:
    """A child ticket: same project, any active type (so any team), one level deep, raised by the caller."""
    parent = load(db, p, ref, lock=True)
    if not can_add_child(p, parent):
        raise AppError("forbidden")
    if parent.parent_id is not None:
        raise AppError("invalid_state", "A child ticket cannot have child tickets of its own.")
    if parent.status_code not in ACTIVE:
        raise AppError("invalid_state", "Child tickets can be added only while the ticket is open.")
    body = dict(data)
    body["project_id"] = parent.project_id
    if body.get("priority_id") in (None, ""):
        body["priority_id"] = parent.priority_id
    child, _ = create_ticket(db, p, body, source)
    child.parent_id = parent.id
    key = db.scalar(select(Project.key).where(Project.id == parent.project_id))
    add_event(db, parent, "child_created", p.id, parent.status_code, parent.status_code,
              note_text=f"{key}-{child.number}: {child.title}", visibility="INTERNAL", data={"child_id": child.id})
    add_event(db, child, "parent_linked", p.id, child.status_code, child.status_code,
              note_text=f"{key}-{parent.number}: {parent.title}", visibility="INTERNAL", data={"parent_id": parent.id})
    parent.updated_at = clock.now()
    db.flush()
    return child


def relatives(ctx: Ctx, p: Principal, t: Ticket, staff: bool) -> dict:
    """Parent and children as small summaries; `visible` says whether the caller may open them."""
    db = ctx.db

    def brief(x: Ticket) -> dict:
        st = ctx.statuses.get(x.status_code)
        d = {"id": x.id, "number": ticket_number(ctx, x), "title": x.title,
             "status": {"code": x.status_code, "name": st.name if st else x.status_code}, "visible": p.can_see(x)}
        if staff:
            res = ctx.user(x.resolver_id)
            d["team"] = ctx.team(x.team_id).name
            d["resolver"] = res.name if res else None
        return d

    out = {"parent": brief(db.get(Ticket, t.parent_id)) if t.parent_id else None,
           "children": [brief(c) for c in db.scalars(select(Ticket).where(Ticket.parent_id == t.id)
                                                     .order_by(Ticket.number))]}
    return out


# ================================================================ assignment primitive
def apply_assignment(db: Session, t: Ticket, resolver_id: int, actor_id: int | None, event_type: str,
                     mode: str | None = None, note_text: str | None = None):
    old = t.status_code
    prev = t.resolver_id
    t.resolver_id = resolver_id
    t.assigned_at = clock.now()
    t.waiting_for_candidate = False
    if old == NEW:
        set_status(db, t, ASSIGNED, event_type)
    elif old == IN_PROGRESS:  # reassign: not paused -> ASSIGNED
        set_status(db, t, ASSIGNED, event_type)
    t.updated_at = clock.now()
    data = {"resolver_id": resolver_id}
    if prev:
        data["previous_resolver_id"] = prev
    if mode:
        data["mode"] = mode
    add_event(db, t, event_type, actor_id, old, t.status_code, note_text=note_text, note_public=False, data=data)
    if event_type != "claimed":
        notify.notify(db, "ASSIGNMENT", t, [resolver_id], actor_id=actor_id)


def _resolver_of_team(db: Session, team_id: int, user_id) -> User:
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise AppError("validation_failed", fields={"resolver_id": "required"})
    ok = any(u.id == uid for _, u in routing.team_people(db, team_id))
    if not ok:
        raise AppError("resolver_not_in_team")
    return db.get(User, uid)


# ================================================================ the actions
def perform(db: Session, p: Principal, ref, action: str, body: dict | None = None) -> Ticket:
    body = body or {}
    if action not in FROM:
        raise not_found()
    t = load(db, p, ref, lock=True, action=action)
    ctx = Ctx(db)
    if p.via == "CREDENTIAL" and action not in REQUESTER_ACTIONS:
        raise AppError("forbidden")
    if not role_allows(p, action, t):
        raise AppError("forbidden")
    if t.status_code not in FROM[action]:
        if action == "claim" and t.resolver_id is not None:
            raise AppError("already_claimed")
        if action == "reopen" and t.status_code == CLOSED:
            raise AppError("reopen_window_closed")
        raise AppError("invalid_state")
    if action in CHILD_BLOCKED:
        children_block(ctx, t)
    fn = _ACTIONS[action]
    fn(db, ctx, p, t, body)
    t.updated_at = clock.now()
    db.flush()
    return t


def _claim(db, ctx, p, t, body):
    if t.resolver_id is not None:
        raise AppError("already_claimed")
    if not state_allows(ctx, "claim", t):
        raise AppError("invalid_state", "Only NEW tickets of auto-assigned types waiting for a resolver can be "
                       "claimed. A manager assigns manual types.")
    apply_assignment(db, t, p.id, p.id, "claimed")


def _assign(db, ctx, p, t, body):
    if t.resolver_id is not None:
        raise AppError("invalid_state")
    user = _resolver_of_team(db, t.team_id, body.get("resolver_id"))
    apply_assignment(db, t, user.id, p.id, "assigned", mode="manual")


def _start(db, ctx, p, t, body):
    old = set_status(db, t, IN_PROGRESS, "start")
    add_event(db, t, "started", p.id, old, IN_PROGRESS)


def _hold(db, ctx, p, t, body):
    fields = {}
    reason = note(fields, body, "reason")
    raise_fields(fields)
    old = set_status(db, t, ON_HOLD, "hold")
    t.hold_reason = reason
    add_event(db, t, "held", p.id, old, ON_HOLD, note_text=reason, note_public=False)


def _request_info(db, ctx, p, t, body):
    fields = {}
    question = note(fields, body, "question")
    raise_fields(fields)
    old = set_status(db, t, AWAITING, "request-info")
    t.info_requested_at = clock.now()
    add_event(db, t, "info_requested", p.id, old, AWAITING, note_text=question, note_public=True)
    notify.notify(db, "INFO_REQUEST", t, [t.requester_id], actor_id=p.id)


def _resume(db, ctx, p, t, body):
    old = set_status(db, t, IN_PROGRESS, "resume")
    t.hold_reason = None
    t.info_requested_at = None
    add_event(db, t, "resumed", p.id, old, IN_PROGRESS)


def _respond(db, ctx, p, t, body):
    fields = {}
    answer = note(fields, body, "answer")
    raise_fields(fields)
    old = set_status(db, t, IN_PROGRESS, "respond")
    t.info_requested_at = None
    add_event(db, t, "responded", p.id, old, IN_PROGRESS, note_text=answer, note_public=True)
    notify.notify(db, "REQUESTER_RESPONSE", t, [t.resolver_id], actor_id=p.id)


def resolution_minutes(db: Session, t: Ticket) -> int:
    spec = cfg.calendar_spec(db, sla.sla_calendar_id(db, t))
    return worktime.working_minutes(spec, t.created_at, t.completed_at)


def _resolve(db, ctx, p, t, body):
    fields = {}
    text = note(fields, body, "note")
    effort = body.get("actual_effort_minutes")
    if effort not in (None, ""):
        try:
            effort = int(effort)
            if effort < 0 or effort > 1_000_000:
                raise ValueError
        except (TypeError, ValueError):
            fields["actual_effort_minutes"] = "0 or more"
    else:
        effort = None
    raise_fields(fields)
    old = set_status(db, t, RESOLVED, "resolve")
    t.completed_at = clock.now()
    t.actual_effort_minutes = effort
    t.resolution_minutes = resolution_minutes(db, t)
    t.hold_reason = None
    add_event(db, t, "resolved", p.id, old, RESOLVED, note_text=text, note_public=True,
              data={"actual_effort_minutes": effort})
    notify.notify(db, "RESOLUTION", t, [t.requester_id], actor_id=p.id)


def _reject(db, ctx, p, t, body, system_code: str | None = None):
    fields = {}
    code = system_code or body.get("reason_code")
    if not system_code and code not in REJECT_CODES:
        fields["reason_code"] = f"one of {', '.join(REJECT_CODES)}"
    reason = body.get("reason") if system_code else note(fields, body, "reason")
    raise_fields(fields)
    old = set_status(db, t, REJECTED, "reject")
    t.completed_at = clock.now()
    t.reject_reason_code = code
    t.hold_reason = None
    t.info_requested_at = None
    t.waiting_for_candidate = False
    add_event(db, t, "rejected", p.id if p.via != "SYSTEM" else None, old, REJECTED, note_text=reason,
              note_public=True, reason_code=code)
    notify.notify(db, "NO_REPLY" if system_code == "NO_REPLY" else "REJECTION", t, [t.requester_id],
                  actor_id=p.id if p.via != "SYSTEM" else None)


def _withdraw(db, ctx, p, t, body):
    fields = {}
    reason = note(fields, body, "reason")
    raise_fields(fields)
    old = set_status(db, t, WITHDRAWN, "withdraw")
    t.completed_at = clock.now()
    t.hold_reason = None
    t.info_requested_at = None
    t.waiting_for_candidate = False
    add_event(db, t, "withdrawn", p.id, old, WITHDRAWN, note_text=reason, note_public=True)


def _release(db, ctx, p, t, body):
    prev = t.resolver_id
    old = set_status(db, t, NEW, "release")
    t.resolver_id = None
    t.hold_reason = None
    t.info_requested_at = None
    t.skip_resolver_id = prev  # D30: auto routing skips the releasing resolver once
    add_event(db, t, "released", p.id, old, NEW, data={"previous_resolver_id": prev})
    routing.try_auto_route(db, t)


def _reassign(db, ctx, p, t, body):
    fields = {}
    reason = note(fields, body, "reason")
    if body.get("resolver_id") in (None, ""):
        fields["resolver_id"] = "required"
    raise_fields(fields)
    user = _resolver_of_team(db, t.team_id, body.get("resolver_id"))
    if user.id == t.resolver_id:
        raise AppError("no_change")
    apply_assignment(db, t, user.id, p.id, "reassigned", note_text=reason)


def _reopen(db, ctx, p, t, body):
    fields = {}
    reason = note(fields, body, "reason")
    raise_fields(fields)
    problem = reopen_problem(ctx, t)
    if problem:
        raise AppError(problem)
    old = t.status_code
    t.sla_cycle += 1
    t.resolver_id = None
    t.completed_at = None
    t.resolution_minutes = None
    t.actual_effort_minutes = None
    t.reject_reason_code = None
    t.skip_resolver_id = None
    set_status(db, t, NEW, "reopen")
    add_event(db, t, "reopened", p.id, old, NEW, note_text=reason, note_public=True)
    routing.try_auto_route(db, t)


def _priority(db, ctx, p, t, body):
    try:
        prio = db.get(Priority, int(body.get("priority_id")))
    except (TypeError, ValueError):
        raise AppError("validation_failed", fields={"priority_id": "required"})
    if prio is None or not prio.is_active:
        raise AppError("inactive_reference", fields={"priority_id": "unknown or inactive"})
    if prio.id == t.priority_id:
        raise AppError("no_change")
    change_priority(db, t, prio, p.id)


def change_priority(db: Session, t: Ticket, prio: Priority, actor_id: int | None, rule_id: int | None = None):
    old = t.priority_id
    t.priority_id = prio.id
    add_event(db, t, "priority_changed", actor_id, t.status_code, t.status_code, visibility="INTERNAL",
              rule_id=rule_id, data={"from_priority_id": old, "to_priority_id": prio.id})
    sla.on_priority_change(db, t, old)


def _estimate(db, ctx, p, t, body):
    fields = {}
    try:
        minutes = int(body.get("minutes"))
        if not 1 <= minutes <= 100_000:
            raise ValueError
    except (TypeError, ValueError):
        fields["minutes"] = "1 to 100,000"
    raise_fields(fields)
    old = t.estimate_minutes
    t.estimate_minutes = minutes
    add_event(db, t, "estimate_set", p.id, t.status_code, t.status_code, visibility="INTERNAL",
              data={"from": old, "to": minutes})


def _sla_action(name):
    def fn(db, ctx, p, t, body):
        sla.manual_action(db, p, t, name, body)
    return fn


_ACTIONS = {
    "claim": _claim, "assign": _assign, "start": _start, "hold": _hold, "request-info": _request_info,
    "resume": _resume, "respond": _respond, "resolve": _resolve, "reject": _reject, "withdraw": _withdraw,
    "release": _release, "reassign": _reassign, "reopen": _reopen, "priority": _priority, "estimate": _estimate,
    "sla-pause": _sla_action("sla-pause"), "sla-resume": _sla_action("sla-resume"),
    "sla-extend": _sla_action("sla-extend"),
}


# ================================================================ system actions (worker)
@jobs.sweep("auto_close")
def auto_close(db: Session) -> int:
    from ..permissions import system_principal  # noqa: F401
    now = clock.now()
    closed = 0
    rows = db.execute(select(Ticket.id, Ticket.project_id, Ticket.completed_at).where(
        Ticket.status_code.in_(FINISHED), Ticket.completed_at.isnot(None))).all()
    days_cache = {}
    for tid, pid, completed in rows:
        if pid not in days_cache:
            days_cache[pid] = settings.get(db, "ticket.auto_close_days", pid)
        if completed + timedelta(days=days_cache[pid]) > now:
            continue
        t = db.scalar(select(Ticket).where(Ticket.id == tid).with_for_update(skip_locked=True))
        if t is None or t.status_code not in FINISHED:
            continue
        old = set_status(db, t, CLOSED, "auto-close")
        add_event(db, t, "auto_closed", None, old, CLOSED)
        closed += 1
    return closed


@jobs.sweep("no_reply")
def no_reply(db: Session) -> int:
    from ..permissions import system_principal
    sysp = system_principal(db)
    now = clock.now()
    n = 0
    rows = db.execute(select(Ticket.id, Ticket.project_id, Ticket.info_requested_at).where(
        Ticket.status_code == AWAITING, Ticket.info_requested_at.isnot(None))).all()
    for tid, pid, asked in rows:
        if asked + timedelta(days=settings.get(db, "ticket.requester_noreply_days", pid)) > now:
            continue
        t = db.scalar(select(Ticket).where(Ticket.id == tid).with_for_update(skip_locked=True))
        if t is None or t.status_code != AWAITING or Ctx(db).open_children(t.id):
            continue
        _reject(db, Ctx(db), sysp, t, {"reason": "No reply from the requester in time."}, system_code="NO_REPLY")
        n += 1
    return n


@jobs.handler("route_retry")
def route_retry(db: Session, payload: dict):
    t = db.scalar(select(Ticket).where(Ticket.id == payload.get("ticket_id")).with_for_update())
    if t is None or t.status_code != NEW or t.resolver_id is not None:
        return
    routing.try_auto_route(db, t)


@jobs.sweep("routing_retry")
def routing_sweep(db: Session) -> int:
    return routing.retry_waiting(db)


# ================================================================ views (A10.5, A11.3)
def ticket_number(ctx: Ctx, t: Ticket) -> str:
    return f"{ctx.project(t.project_id).key}-{t.number}"


def view(ctx: Ctx, p: Principal, t: Ticket, due: dict | None = None, with_actions: bool = True,
         with_relatives: bool = False) -> dict:
    staff = p.is_staff_on(t) if p.via != "CREDENTIAL" else False
    users = ctx.users([t.requester_id, t.resolver_id])
    st = ctx.statuses.get(t.status_code)
    proj, tt, team, prio = ctx.project(t.project_id), ctx.type(t.type_id), ctx.team(t.team_id), ctx.priority(t.priority_id)
    req, res = users.get(t.requester_id), users.get(t.resolver_id)
    d = {
        "id": t.id, "number": f"{proj.key}-{t.number}",
        "project": {"id": proj.id, "key": proj.key, "name": proj.name},
        "type": {"id": tt.id, "name": tt.name},
        "team": {"id": team.id, "name": team.name} if staff else {"name": team.name},
        "priority": {"id": prio.id, "name": prio.name, "rank": prio.rank},
        "status": {"code": t.status_code, "name": st.name if st else t.status_code,
                   "category": st.category if st else None},
        "title": t.title, "description": t.description,
        "requester": {"id": req.id, "name": req.name} if req else None,
        "resolver": {"id": res.id, "name": res.name} if res else None,
        "source": t.source, "created_at": t.created_at, "updated_at": t.updated_at, "completed_at": t.completed_at,
    }
    if staff:
        d.update({
            "estimate_minutes": t.estimate_minutes, "actual_effort_minutes": t.actual_effort_minutes,
            "resolution_minutes": t.resolution_minutes, "hold_reason": t.hold_reason,
            "reject_reason_code": t.reject_reason_code,
            "assignment_mode": tt.assignment_mode, "waiting_for_candidate": t.waiting_for_candidate,
            "resolver_active": res.is_active if res else None,
            "manual_sla_pause": t.manual_paused,
            "nearest_due": due if due is not None else sla.nearest_due(ctx.db, t),
        })
    elif t.reject_reason_code:
        d["reject_reason_code"] = t.reject_reason_code
    if with_relatives:  # one ticket: parent and children too (lists skip them)
        d.update(relatives(ctx, p, t, staff))
    if with_actions:
        d["allowed_actions"] = allowed_actions(ctx, p, t)
        d["can_add_child"] = (can_add_child(p, t) and t.parent_id is None and t.status_code in ACTIVE)
    return d


def events(db: Session, p: Principal, ref, page=1, page_size=50):
    t = load(db, p, ref)
    staff = p.is_staff_on(t) if p.via != "CREDENTIAL" else False
    q = select(TicketEvent).where(TicketEvent.ticket_id == t.id)
    if not staff:
        q = q.where(TicketEvent.visibility == "PUBLIC")
    rows, meta = paginate(db, q, page, page_size, None, None, [TicketEvent.created_at.desc(), TicketEvent.id.desc()])
    ctx = Ctx(db)
    names = ctx.users([e.actor_id for e in rows])
    rule_names = {}
    if staff:
        from ..models import EscalationRule
        ids = {e.rule_id for e in rows if e.rule_id}
        if ids:
            rule_names = dict(db.execute(select(EscalationRule.id, EscalationRule.name).where(
                EscalationRule.id.in_(ids))).all())
    out = []
    for e in rows:
        actor = names.get(e.actor_id)
        item = {"id": e.id, "type": e.type, "actor": {"id": actor.id, "name": actor.name} if actor else "System",
                "from_status": e.from_status, "to_status": e.to_status, "created_at": e.created_at}
        if staff:
            item["note"] = e.note
            item["visibility"] = e.visibility
            item["reason_code"] = e.reason_code
            item["rule"] = {"id": e.rule_id, "name": rule_names.get(e.rule_id)} if e.rule_id else None
            item["data"] = e.data
        else:
            item["note"] = e.note if e.note_public else None
            if e.reason_code:
                item["reason_code"] = e.reason_code
        out.append(item)
    return out, meta, t


# ================================================================ lists (F10.1, F10.2) and counts (F10.3)
VIEWS = ("mine", "work", "queue", "team", "all")


def list_query(db: Session, p: Principal, params: dict):
    q = select(Ticket).where(p.ticket_filter())
    view_ = params.get("view")
    me = p.id
    if view_ and view_ not in VIEWS:
        raise AppError("validation_failed", fields={"view": f"one of {', '.join(VIEWS)}"})
    if view_ == "mine":
        q = q.where(Ticket.requester_id == me)
    elif view_ == "work":
        if not p.teams():
            raise AppError("forbidden")
        q = q.where(Ticket.resolver_id == me, Ticket.status_code.in_(ACTIVE))
    elif view_ == "queue":
        if not p.teams():
            raise AppError("forbidden")
        q = q.where(Ticket.team_id.in_(p.teams()), Ticket.status_code == NEW, Ticket.resolver_id.is_(None))
    elif view_ == "team":
        if not p.teams("TEAM_MANAGER"):
            raise AppError("forbidden")
        q = q.where(Ticket.team_id.in_(p.teams("TEAM_MANAGER")))
    elif view_ == "all":
        if not (p.teams("TEAM_MANAGER") or p.any_admin()):
            raise AppError("forbidden")
    statuses = params.get("status") or []
    if isinstance(statuses, str):
        statuses = [statuses]
    statuses = [s for s in statuses if s]
    groups = {"open": ACTIVE, "active": ACTIVE, "waiting": (AWAITING,), "done": DONE, "finished": FINISHED}
    codes = []
    for s in statuses:
        codes.extend(groups.get(s.lower(), (s.upper(),)))
    if codes:
        valid = set(cfg.status_map(db))
        bad = [c for c in codes if c not in valid]
        if bad:
            raise AppError("validation_failed", fields={"status": f"unknown status {bad[0]}"})
        q = q.where(Ticket.status_code.in_(codes))

    def int_param(name):
        v = params.get(name)
        if v in (None, ""):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise AppError("validation_failed", fields={name: "must be an id"})

    for name, col in (("priority", Ticket.priority_id), ("type", Ticket.type_id), ("team", Ticket.team_id),
                      ("requester", Ticket.requester_id), ("project", Ticket.project_id)):
        v = int_param(name)
        if v is not None:
            q = q.where(col == v)
    res = params.get("resolver")
    if res not in (None, ""):
        if res == "me":
            q = q.where(Ticket.resolver_id == me)
        elif res == "none":
            q = q.where(Ticket.resolver_id.is_(None))
        else:
            q = q.where(Ticket.resolver_id == int_param("resolver"))
    text = (params.get("q") or "").strip()
    if text:
        m = NUMBER_RE.match(text)
        if m:
            q = q.where(Ticket.project_id.in_(select(Project.id).where(Project.key == m.group(1).upper())),
                        Ticket.number == int(m.group(2)))
        elif text.isdigit():
            q = q.where(or_(Ticket.number == int(text), Ticket.title.ilike(f"%{text}%")))
        else:
            q = q.where(Ticket.title.ilike(f"%{text}%"))
    for name, op in (("created_from", ">="), ("created_to", "<")):
        v = params.get(name)
        if v:
            from datetime import datetime
            try:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError:
                raise AppError("validation_failed", fields={name: "ISO 8601 date"})
            if dt.tzinfo is None:
                from datetime import timezone
                dt = dt.replace(tzinfo=timezone.utc)
            q = q.where(Ticket.created_at >= dt if op == ">=" else Ticket.created_at < dt)
    overdue = params.get("overdue")
    if overdue in (True, "true", "1", "yes", "on"):
        q = q.where(sla.overdue_clause())
    return q


def nearest_due_col():
    return (select(func.min(SlaTimer.due_at)).where(
        SlaTimer.ticket_id == Ticket.id, SlaTimer.is_current.is_(True), SlaTimer.state == "RUNNING")
        .correlate(Ticket).scalar_subquery())


def list_tickets(db: Session, p: Principal, params: dict):
    q = list_query(db, p, params)
    sorts = {"number": Ticket.number, "title": Ticket.title, "created": Ticket.created_at,
             "updated": Ticket.updated_at, "status": Ticket.status_code, "priority": Ticket.priority_id,
             "type": Ticket.type_id, "team": Ticket.team_id, "resolver": Ticket.resolver_id,
             "requester": Ticket.requester_id, "nearest_due": nearest_due_col()}
    if (params.get("sort") or "").lstrip("-") == "priority":
        sorts["priority"] = select(Priority.rank).where(Priority.id == Ticket.priority_id).correlate(Ticket).scalar_subquery()
    rows, meta = paginate(db, q, params.get("page") or 1, params.get("page_size") or 25, params.get("sort"), sorts,
                          [Ticket.created_at.desc(), Ticket.id.desc()])
    ctx = Ctx(db)
    ctx.prefetch_timers([t.id for t in rows])
    ctx.prefetch_open_children([t.id for t in rows])
    ctx.users([u for t in rows for u in (t.requester_id, t.resolver_id)])
    dues = sla.nearest_due_many(db, [t.id for t in rows])
    return [view(ctx, p, t, due=dues.get(t.id)) for t in rows], meta


def counts(db: Session, p: Principal) -> dict:
    base = select(func.count()).select_from(Ticket)
    waiting = db.scalar(base.where(Ticket.requester_id == p.id, Ticket.status_code == AWAITING)) or 0
    if p.via == "CREDENTIAL" or not p.teams():
        return {"queue": 0, "my_work": 0, "waiting_on_me": waiting}
    queue = db.scalar(base.where(Ticket.team_id.in_(p.teams()), Ticket.status_code == NEW,
                                 Ticket.resolver_id.is_(None))) or 0
    work = db.scalar(base.where(Ticket.resolver_id == p.id, Ticket.status_code.in_(ACTIVE))) or 0
    return {"queue": queue, "my_work": work, "waiting_on_me": waiting}
