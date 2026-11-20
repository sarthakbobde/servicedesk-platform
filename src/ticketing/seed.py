"""Repeatable seed (Phase 1, task 3): statuses, priorities, default calendar, in-app channel, default in-app
templates, internal organisation and the first platform admin. Running it twice creates nothing new."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_config
from .models import (
    Calendar, CalendarHours, NotificationChannel, NotificationTemplate, Organization, Priority, TicketStatus, User,
)
from .security import hash_password

STATUSES = [
    ("NEW", "New", "OPEN"),
    ("ASSIGNED", "Assigned", "ACTIVE"),
    ("IN_PROGRESS", "In progress", "ACTIVE"),
    ("ON_HOLD", "On hold", "PAUSED"),
    ("AWAITING_REQUESTER", "Awaiting requester", "PAUSED"),
    ("RESOLVED", "Resolved", "DONE"),
    ("REJECTED", "Rejected", "DONE"),
    ("WITHDRAWN", "Withdrawn", "DONE"),
    ("CLOSED", "Closed", "DONE"),
]

PRIORITIES = [("Critical", 1, False), ("High", 2, False), ("Medium", 3, True), ("Low", 4, False)]

# F8.1 notification types with default in-app text. Placeholders per F8.4.
TEMPLATES = {
    "ACKNOWLEDGEMENT": ("{ticket_number} received", "We received your ticket {ticket_number}: {title}."),
    "ASSIGNMENT": ("{ticket_number} assigned to you", "{ticket_number} \"{title}\" ({priority}) is assigned to you."),
    "INFO_REQUEST": ("{ticket_number} needs your answer", "{resolver} asked for information on {ticket_number}: "
                     "{title}. Please respond."),
    "REQUESTER_RESPONSE": ("{ticket_number} answered", "The requester answered on {ticket_number}: {title}."),
    "REJECTION": ("{ticket_number} rejected", "{ticket_number} \"{title}\" was rejected."),
    "NO_REPLY": ("{ticket_number} closed for no reply", "{ticket_number} \"{title}\" was rejected because no "
                 "answer arrived in time."),
    "SLA_WARNING": ("{ticket_number} at risk", "{ticket_number} \"{title}\" is close to its due time {due_time}."),
    "SLA_BREACH": ("{ticket_number} breached", "{ticket_number} \"{title}\" passed its due time {due_time}."),
    "ESCALATION": ("{ticket_number} escalated", "{ticket_number} \"{title}\" ({priority}, {status}) was escalated."),
    "RESOLUTION": ("{ticket_number} resolved", "{ticket_number} \"{title}\" is resolved. You can reopen it if the "
                   "problem remains."),
}


def run(db: Session, admin_email: str | None = None, admin_password: str | None = None) -> dict:
    cfg = get_config()
    created = {}

    existing = {s.code: s for s in db.scalars(select(TicketStatus))}
    for order, (code, name, cat) in enumerate(STATUSES, start=1):
        if code not in existing:
            db.add(TicketStatus(code=code, name=name, category=cat, sort_order=order, is_core=True))
            created.setdefault("statuses", 0)
            created["statuses"] += 1
        else:  # order and category are locked (D26); the name may have been renamed and is kept
            existing[code].sort_order, existing[code].category, existing[code].is_core = order, cat, True

    if not db.scalar(select(Priority.id).limit(1)):
        for name, rank, default in PRIORITIES:
            db.add(Priority(name=name, rank=rank, is_default=default, is_active=True))
        created["priorities"] = len(PRIORITIES)

    if not db.scalar(select(Calendar.id).where(Calendar.is_default.is_(True))):
        cal = Calendar(name="Standard hours (UTC)", time_zone="UTC", is_default=True, is_active=True)
        db.add(cal)
        db.flush()
        for wd in range(1, 6):
            db.add(CalendarHours(calendar_id=cal.id, weekday=wd, start_minute=9 * 60, end_minute=17 * 60))
        created["calendar"] = 1

    channel = db.scalar(select(NotificationChannel).where(NotificationChannel.type == "IN_APP"))
    if channel is None:
        channel = NotificationChannel(type="IN_APP", name="In-app", settings={}, enabled=True)
        db.add(channel)
        db.flush()
        created["channel"] = 1
    have = set(db.scalars(select(NotificationTemplate.notification_type).where(
        NotificationTemplate.channel_id == channel.id)))
    for ntype, (subject, body) in TEMPLATES.items():
        if ntype not in have:
            db.add(NotificationTemplate(notification_type=ntype, channel_id=channel.id, subject=subject, body=body,
                                        enabled=True))
            created["templates"] = created.get("templates", 0) + 1

    org = db.scalar(select(Organization).where(Organization.type == "INTERNAL").order_by(Organization.id))
    if org is None:
        org = Organization(name="Internal", type="INTERNAL", is_active=True)
        db.add(org)
        db.flush()
        created["organization"] = 1

    if not db.scalar(select(User.id).where(User.is_platform_admin.is_(True)).limit(1)):
        email = (admin_email or cfg.bootstrap_admin_email).lower()
        db.add(User(user_type="MEMBER", name="Platform admin", email=email, organization_id=org.id,
                    is_platform_admin=True, password_hash=hash_password(admin_password or cfg.bootstrap_admin_password),
                    must_change_password=True, is_active=True))
        created["platform_admin"] = email
    db.flush()
    return created


def main():
    from .db import SessionLocal
    with SessionLocal() as db:
        result = run(db)
        db.commit()
    print("seed:", result or "nothing new")


if __name__ == "__main__":
    main()
