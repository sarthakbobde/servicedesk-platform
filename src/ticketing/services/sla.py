"""SLA timers (F6). Phase 2 provides the hooks the lifecycle calls; Phase 3 fills them in."""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import Ticket


def on_created(db: Session, ticket: Ticket) -> None:
    """Post-create hook: start the SLA (Phase 3)."""


def on_transition(db: Session, ticket: Ticket, old: str, new: str, action: str) -> None:
    """Status-change hook: pause, resume, stop, skip, re-arm (Phase 3)."""


def on_priority_change(db: Session, ticket: Ticket, old_priority_id: int) -> None:
    """Replace timers under the new priority's policy (Phase 3, D28)."""


def nearest_due(db: Session, ticket: Ticket) -> dict | None:
    return None


def sla_calendar_id(db: Session, ticket: Ticket) -> int | None:
    return ticket.sla_calendar_id


def action_available(db: Session, ticket: Ticket, action: str) -> bool:
    """Manual SLA actions arrive in Phase 3."""
    return False


def manual_action(db: Session, p, ticket: Ticket, action: str, body: dict) -> None:
    from ..errors import AppError
    raise AppError("invalid_state", "The ticket has no SLA timers.")


def overdue_clause():
    from sqlalchemy import false
    return false()


def nearest_due_many(db: Session, ticket_ids) -> dict:
    return {}
