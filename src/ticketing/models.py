"""All 29 tables (A4): the 18 of the design plus 11 new ones."""
from datetime import date, datetime

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

JSONType = JSON().with_variant(JSONB(), "postgresql")
TS = DateTime(timezone=True)


def now_default():
    return mapped_column(TS, server_default=func.now(), nullable=False)


def active_col():
    return mapped_column(Boolean, default=True, server_default=text("true"))


# ---------------------------------------------------------------- structure
class Organization(Base):
    __tablename__ = "organization"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    type: Mapped[str] = mapped_column(String(10))  # INTERNAL | CLIENT
    is_active: Mapped[bool] = active_col()
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (CheckConstraint("type in ('INTERNAL','CLIENT')", name="ck_org_type"),)


class Project(Base):
    __tablename__ = "project"
    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(10), unique=True)
    ticket_seq: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    is_active: Mapped[bool] = active_col()
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_project_org_name"),)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_type: Mapped[str] = mapped_column(String(15), default="MEMBER")  # MEMBER | PRODUCT_USER
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id"), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id"), index=True)  # product users
    external_id: Mapped[str | None] = mapped_column(String(200))
    password_hash: Mapped[str | None] = mapped_column(String(300))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(TS)
    last_login_at: Mapped[datetime | None] = mapped_column(TS)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_active: Mapped[bool] = active_col()
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (
        CheckConstraint("user_type in ('MEMBER','PRODUCT_USER')", name="ck_user_type"),
        CheckConstraint(
            "(user_type = 'MEMBER' AND organization_id IS NOT NULL AND project_id IS NULL AND external_id IS NULL)"
            " OR (user_type = 'PRODUCT_USER' AND project_id IS NOT NULL AND external_id IS NOT NULL"
            " AND is_platform_admin = false)",
            name="ck_user_kind_fields",
        ),
        Index("uq_users_email", func.lower(text("email")), unique=True, postgresql_where=text("email IS NOT NULL")),
        Index("uq_users_project_external", "project_id", "external_id", unique=True,
              postgresql_where=text("external_id IS NOT NULL")),
    )


class Team(Base):
    __tablename__ = "team"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = active_col()
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_team_project_name"),)


class RoleAssignment(Base):
    """One row grants one role at one scope. Team and project roles also carry project and organisation
    (denormalised for scope queries). Team roles carry the person's shift calendar (D22)."""
    __tablename__ = "role_assignment"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    scope: Mapped[str] = mapped_column(String(15))  # TEAM | PROJECT | ORGANIZATION
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id"), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id"), index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("team.id"), index=True)
    calendar_id: Mapped[int | None] = mapped_column(ForeignKey("calendar.id"))
    is_active: Mapped[bool] = active_col()
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (
        CheckConstraint(
            "(scope='TEAM' AND role IN ('RESOLVER','TEAM_MANAGER') AND team_id IS NOT NULL AND project_id IS NOT NULL)"
            " OR (scope='PROJECT' AND role='PROJECT_ADMIN' AND team_id IS NULL AND project_id IS NOT NULL"
            " AND calendar_id IS NULL)"
            " OR (scope='ORGANIZATION' AND role='ORG_ADMIN' AND team_id IS NULL AND project_id IS NULL"
            " AND calendar_id IS NULL)",
            name="ck_role_scope",
        ),
        Index("uq_ra_team", "user_id", "role", "team_id", unique=True,
              postgresql_where=text("is_active AND scope = 'TEAM'")),
        Index("uq_ra_project", "user_id", "role", "project_id", unique=True,
              postgresql_where=text("is_active AND scope = 'PROJECT'")),
        Index("uq_ra_org", "user_id", "role", "organization_id", unique=True,
              postgresql_where=text("is_active AND scope = 'ORGANIZATION'")),
    )


# ---------------------------------------------------------------- reference data
class TicketType(Base):
    __tablename__ = "ticket_type"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    assignment_mode: Mapped[str] = mapped_column(String(10), default="MANUAL")  # MANUAL | AUTO
    is_active: Mapped[bool] = active_col()
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_type_project_name"),
        CheckConstraint("assignment_mode in ('MANUAL','AUTO')", name="ck_type_mode"),
    )


class Priority(Base):
    __tablename__ = "priority"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    rank: Mapped[int] = mapped_column(Integer, unique=True)  # 1 = most urgent
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_active: Mapped[bool] = active_col()
    __table_args__ = (Index("uq_priority_default", "is_default", unique=True, postgresql_where=text("is_default")),)


class TicketStatus(Base):
    __tablename__ = "ticket_status"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(10))  # OPEN | ACTIVE | PAUSED | DONE
    sort_order: Mapped[int] = mapped_column(Integer)
    is_core: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class Calendar(Base):
    __tablename__ = "calendar"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    time_zone: Mapped[str] = mapped_column(String(64))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_active: Mapped[bool] = active_col()
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (Index("uq_calendar_default", "is_default", unique=True, postgresql_where=text("is_default")),)


class CalendarHours(Base):
    __tablename__ = "calendar_hours"
    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("calendar.id"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)  # 1 = Monday .. 7 = Sunday
    start_minute: Mapped[int] = mapped_column(Integer)  # minutes after local midnight
    end_minute: Mapped[int] = mapped_column(Integer)  # 1440 = 24:00; end < start runs past midnight
    __table_args__ = (CheckConstraint("weekday between 1 and 7", name="ck_hours_weekday"),)


class CalendarHoliday(Base):
    __tablename__ = "calendar_holiday"
    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_id: Mapped[int] = mapped_column(ForeignKey("calendar.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    name: Mapped[str] = mapped_column(String(200))
    start_minute: Mapped[int | None] = mapped_column(Integer)  # empty = whole day off
    end_minute: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = active_col()


# ---------------------------------------------------------------- SLA configuration
class SlaPolicy(Base):
    __tablename__ = "sla_policy"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    priority_id: Mapped[int] = mapped_column(ForeignKey("priority.id"))
    ticket_type_id: Mapped[int | None] = mapped_column(ForeignKey("ticket_type.id"))  # empty = default policy
    calendar_id: Mapped[int | None] = mapped_column(ForeignKey("calendar.id"))  # empty = default calendar (D23)
    is_active: Mapped[bool] = active_col()
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (
        Index("uq_sla_policy_match", "priority_id", func.coalesce(text("ticket_type_id"), 0), unique=True,
              postgresql_where=text("is_active")),
    )


class SlaMetric(Base):
    __tablename__ = "sla_metric"
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("sla_policy.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    minutes: Mapped[int] = mapped_column(Integer)  # cumulative from SLA start (D29)
    stop_status_code: Mapped[str] = mapped_column(String(30))
    is_active: Mapped[bool] = active_col()


class EscalationRule(Base):
    __tablename__ = "escalation_rule"
    id: Mapped[int] = mapped_column(primary_key=True)
    metric_id: Mapped[int] = mapped_column(ForeignKey("sla_metric.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    level: Mapped[int] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(String(30))
    threshold: Mapped[int] = mapped_column(Integer, default=0)
    action: Mapped[str] = mapped_column(String(30))
    target_type: Mapped[str | None] = mapped_column(String(20))
    target_id: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = active_col()


# ---------------------------------------------------------------- tickets
class Ticket(Base):
    __tablename__ = "ticket"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    type_id: Mapped[int] = mapped_column(ForeignKey("ticket_type.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id"), index=True)
    priority_id: Mapped[int] = mapped_column(ForeignKey("priority.id"))
    status_code: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    resolver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    source: Mapped[str] = mapped_column(String(10))  # PORTAL | API
    created_at: Mapped[datetime] = now_default()
    updated_at: Mapped[datetime] = now_default()
    completed_at: Mapped[datetime | None] = mapped_column(TS)
    assigned_at: Mapped[datetime | None] = mapped_column(TS)
    info_requested_at: Mapped[datetime | None] = mapped_column(TS)
    estimate_minutes: Mapped[int | None] = mapped_column(Integer)
    actual_effort_minutes: Mapped[int | None] = mapped_column(Integer)
    resolution_minutes: Mapped[int | None] = mapped_column(Integer)
    hold_reason: Mapped[str | None] = mapped_column(Text)
    reject_reason_code: Mapped[str | None] = mapped_column(String(20))
    waiting_for_candidate: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    skip_resolver_id: Mapped[int | None] = mapped_column(Integer)  # D30: skipped once after a release
    sla_policy_id: Mapped[int | None] = mapped_column(ForeignKey("sla_policy.id"))
    sla_calendar_id: Mapped[int | None] = mapped_column(ForeignKey("calendar.id"))
    sla_started_at: Mapped[datetime | None] = mapped_column(TS)
    sla_cycle: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    manual_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    manual_pause_reason: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("ticket.id"), index=True)  # child tickets, one level
    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_ticket_number"),
        CheckConstraint("source in ('PORTAL','API')", name="ck_ticket_source"),
    )


class SlaTimer(Base):
    __tablename__ = "sla_timer"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("ticket.id"), index=True)
    metric_id: Mapped[int | None] = mapped_column(ForeignKey("sla_metric.id"))
    cycle: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    stop_status_code: Mapped[str] = mapped_column(String(30))
    minutes_target: Mapped[int] = mapped_column(Integer)  # copied from the step at creation (F6.10)
    extension_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    state: Mapped[str] = mapped_column(String(10), index=True)  # RUNNING | PAUSED | STOPPED | SKIPPED
    seconds_used: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    running_since: Mapped[datetime | None] = mapped_column(TS)
    due_at: Mapped[datetime | None] = mapped_column(TS, index=True)
    stopped_at: Mapped[datetime | None] = mapped_column(TS)
    breached_at: Mapped[datetime | None] = mapped_column(TS)
    warned_at: Mapped[datetime | None] = mapped_column(TS)  # SLA_WARNING sent (once per timer)
    paused_reason: Mapped[str | None] = mapped_column(String(10))  # STATUS | MANUAL
    paused_since: Mapped[datetime | None] = mapped_column(TS)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    __table_args__ = (
        Index("ix_sla_timer_open", "state", postgresql_where=text("is_current AND state IN ('RUNNING','PAUSED')")),
    )


class TicketEvent(Base):
    __tablename__ = "ticket_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("ticket.id"), index=True)
    type: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # empty = System
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str | None] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(Text)
    note_public: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    visibility: Mapped[str] = mapped_column(String(10), default="PUBLIC")  # PUBLIC | INTERNAL
    reason_code: Mapped[str | None] = mapped_column(String(20))
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("escalation_rule.id"))
    data: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = now_default()


class Notification(Base):
    __tablename__ = "notification"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("ticket.id"))
    type: Mapped[str] = mapped_column(String(30))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("escalation_rule.id"))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (Index("ix_notification_unread", "user_id", postgresql_where=text("NOT is_read")),)


# ---------------------------------------------------------------- new tables (11)
class Setting(Base):
    __tablename__ = "setting"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(100))
    value: Mapped[object] = mapped_column(JSONType)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("project.id"))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = now_default()
    __table_args__ = (Index("uq_setting_key_scope", "key", func.coalesce(text("project_id"), 0), unique=True),)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(30))
    entity: Mapped[str] = mapped_column(String(50), index=True)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    organization_id: Mapped[int | None] = mapped_column(Integer, index=True)
    project_id: Mapped[int | None] = mapped_column(Integer, index=True)
    old_value: Mapped[dict | None] = mapped_column(JSONType)
    new_value: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = now_default()


class NotificationChannel(Base):
    __tablename__ = "notification_channel"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(20))  # IN_APP | EMAIL | CHAT_WEBHOOK
    name: Mapped[str] = mapped_column(String(200), unique=True)
    settings: Mapped[dict] = mapped_column(JSONType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    last_failure_at: Mapped[datetime | None] = mapped_column(TS)
    last_failure_text: Mapped[str | None] = mapped_column(Text)


class NotificationTemplate(Base):
    __tablename__ = "notification_template"
    id: Mapped[int] = mapped_column(primary_key=True)
    notification_type: Mapped[str] = mapped_column(String(30))
    channel_id: Mapped[int] = mapped_column(ForeignKey("notification_channel.id"))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    __table_args__ = (UniqueConstraint("notification_type", "channel_id", name="uq_template_type_channel"),)


class ApiCredential(Base):
    __tablename__ = "api_credential"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    key_prefix: Mapped[str] = mapped_column(String(40), unique=True)
    secret_hash: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = active_col()
    last_used_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = now_default()


class Job(Base):
    __tablename__ = "job"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    run_at: Mapped[datetime] = now_default()
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[datetime | None] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(10), default="PENDING")  # PENDING | DONE | FAILED
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (
        Index("ix_job_due", "run_at", postgresql_where=text("status = 'PENDING'")),
        Index("uq_job_dedupe", "dedupe_key", unique=True,
              postgresql_where=text("status = 'PENDING' AND dedupe_key IS NOT NULL")),
    )


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(ForeignKey("notification.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("notification_channel.id"))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="PENDING")  # PENDING | SENT | FAILED
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = now_default()
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(TS)
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (Index("ix_outbox_pending", "next_attempt_at", postgresql_where=text("status = 'PENDING'")),)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_key"
    id: Mapped[int] = mapped_column(primary_key=True)
    caller: Mapped[str] = mapped_column(String(200))
    key: Mapped[str] = mapped_column(String(200))
    request_hash: Mapped[str] = mapped_column(String(64))
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("ticket.id"))
    created_at: Mapped[datetime] = now_default()
    __table_args__ = (UniqueConstraint("caller", "key", name="uq_idempotency_caller_key"),)


class AuthSession(Base):
    __tablename__ = "auth_session"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # SESSION | API_TOKEN
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(TS)
    last_seen_at: Mapped[datetime] = mapped_column(TS)
    expires_at: Mapped[datetime] = mapped_column(TS)
    revoked_at: Mapped[datetime | None] = mapped_column(TS)


class EscalationFiring(Base):
    __tablename__ = "escalation_firing"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("ticket.id"), index=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("escalation_rule.id"))
    cycle: Mapped[int] = mapped_column(Integer)
    fired_at: Mapped[datetime] = mapped_column(TS)
    __table_args__ = (UniqueConstraint("ticket_id", "rule_id", "cycle", name="uq_firing_once"),)


class SlaAdjustment(Base):
    __tablename__ = "sla_adjustment"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("ticket.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # PAUSE | RESUME | EXTEND
    position: Mapped[int | None] = mapped_column(Integer)
    minutes: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    at: Mapped[datetime] = mapped_column(TS)
