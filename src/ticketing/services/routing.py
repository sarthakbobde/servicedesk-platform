"""Queue and routing (F4): shifts from each person's own calendar, auto routing (fewest open, ties to the longest
idle), retry when nobody is on shift, claim fallback, re-routing after release and reopen."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock, settings, worktime
from ..models import RoleAssignment, Ticket, TicketType, User
from . import configuration as cfg
from . import jobs

DONE = ("RESOLVED", "REJECTED", "WITHDRAWN", "CLOSED")


def team_people(db: Session, team_id: int, roles=("RESOLVER",)) -> list[tuple[RoleAssignment, User]]:
    return db.execute(select(RoleAssignment, User).join(User, User.id == RoleAssignment.user_id).where(
        RoleAssignment.team_id == team_id, RoleAssignment.role.in_(roles), RoleAssignment.is_active.is_(True),
        User.is_active.is_(True)).order_by(User.name)).all()


def on_shift(db: Session, ra: RoleAssignment, at=None, cache: dict | None = None) -> bool:
    spec = cfg.calendar_spec(db, ra.calendar_id, cache)
    return worktime.is_working(spec, at or clock.now())


def open_counts(db: Session, user_ids) -> dict[int, int]:
    if not user_ids:
        return {}
    rows = db.execute(select(Ticket.resolver_id, func.count()).where(
        Ticket.resolver_id.in_(user_ids), Ticket.status_code.notin_(DONE)).group_by(Ticket.resolver_id)).all()
    return {uid: n for uid, n in rows}


def last_assigned(db: Session, user_ids) -> dict[int, object]:
    if not user_ids:
        return {}
    rows = db.execute(select(Ticket.resolver_id, func.max(Ticket.assigned_at)).where(
        Ticket.resolver_id.in_(user_ids)).group_by(Ticket.resolver_id)).all()
    return {uid: at for uid, at in rows}


def candidates(db: Session, team_id: int, exclude: int | None = None, cache: dict | None = None) -> list[int]:
    """Active resolvers of the team who are on shift now, best first."""
    now = clock.now()
    cache = {} if cache is None else cache
    people = [u.id for ra, u in team_people(db, team_id) if u.id != exclude and on_shift(db, ra, now, cache)]
    if not people:
        return []
    counts = open_counts(db, people)
    idle = last_assigned(db, people)
    epoch = now - timedelta(days=36500)
    # fewest open tickets first, then the longest idle (oldest last assignment; never assigned first), then id
    return sorted(people, key=lambda uid: (counts.get(uid, 0), idle.get(uid) or epoch, uid))


def is_auto(db: Session, ticket: Ticket) -> bool:
    return db.scalar(select(TicketType.assignment_mode).where(TicketType.id == ticket.type_id)) == "AUTO"


def try_auto_route(db: Session, ticket: Ticket, cache: dict | None = None) -> int | None:
    """Assign an on-shift resolver to a NEW unassigned AUTO ticket. On no candidate, mark the ticket as waiting
    (claimable) and schedule a retry. Returns the resolver id or None. Caller holds the ticket row lock."""
    from . import tickets  # cycle: tickets -> routing -> tickets
    if ticket.status_code != "NEW" or ticket.resolver_id is not None or not is_auto(db, ticket):
        return None
    skip = ticket.skip_resolver_id
    found = candidates(db, ticket.team_id, exclude=skip, cache=cache)
    if not found:
        ticket.waiting_for_candidate = True
        minutes = settings.get(db, "routing.retry_minutes")
        jobs.enqueue(db, "route_retry", {"ticket_id": ticket.id}, run_at=clock.now() + timedelta(minutes=minutes),
                     dedupe_key=f"route:{ticket.id}")
        return None
    ticket.skip_resolver_id = None  # D30: skipped once only
    tickets.apply_assignment(db, ticket, found[0], actor_id=None, event_type="assigned", mode="auto")
    return found[0]


def retry_waiting(db: Session) -> int:
    """Worker: route waiting AUTO tickets whose team now has someone on shift (covers 'when a shift starts')."""
    cache: dict = {}
    routed = 0
    team_has: dict[int, bool] = {}
    ids = list(db.scalars(select(Ticket.id).where(Ticket.waiting_for_candidate.is_(True), Ticket.status_code == "NEW",
                                                  Ticket.resolver_id.is_(None)).order_by(Ticket.id)))
    for tid in ids:
        t = db.scalar(select(Ticket).where(Ticket.id == tid).with_for_update(skip_locked=True))
        if t is None or t.status_code != "NEW" or t.resolver_id is not None:
            continue
        if t.team_id not in team_has:
            team_has[t.team_id] = bool(candidates(db, t.team_id, cache=cache))
        if team_has[t.team_id] and try_auto_route(db, t, cache) is not None:
            routed += 1
    return routed
