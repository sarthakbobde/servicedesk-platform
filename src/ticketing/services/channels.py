"""Notification means (F8.3-F8.6): channel adapters (base class, registry, SPEC), outbox delivery after commit with
retries and back-off, channel and template administration, test send and preview.

Secrets are never stored: a channel's settings name an environment variable that holds the secret (SMTP password,
webhook URL). Delivery never touches the ticket, so a failing channel cannot block or roll back ticket work."""
from __future__ import annotations

import os
import smtplib
from datetime import timedelta
from email.message import EmailMessage

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit, clock, settings
from ..errors import AppError
from ..models import Notification, NotificationChannel, NotificationOutbox, NotificationTemplate, User
from ..permissions import Principal
from . import jobs, notify
from .common import bool_field, get_or_404, raise_if, text_field


class DeliveryError(Exception):
    pass


class Channel:
    """Adapter base class. SPEC maps setting name -> (type, required, help)."""
    type = ""
    SPEC: dict[str, tuple[type, bool, str]] = {}

    def __init__(self, row: NotificationChannel):
        self.row = row
        self.settings = row.settings or {}

    @classmethod
    def validate(cls, data: dict) -> dict:
        fields = {}
        clean = {}
        for k, v in (data or {}).items():
            if k not in cls.SPEC:
                fields[f"settings.{k}"] = "unknown setting" + (
                    "; secrets are referenced by an environment variable name (…_env)" if k in (
                        "password", "url", "token", "secret") else "")
                continue
            typ = cls.SPEC[k][0]
            try:
                clean[k] = typ(v) if typ is not bool else (v if isinstance(v, bool) else str(v).lower() == "true")
            except (TypeError, ValueError):
                fields[f"settings.{k}"] = f"must be {typ.__name__}"
        for k, (typ, required, _) in cls.SPEC.items():
            if required and k not in clean:
                fields[f"settings.{k}"] = "required"
        return clean, fields

    def secret(self, key: str) -> str | None:
        name = self.settings.get(key)
        return os.environ.get(name) if name else None

    def send(self, subject: str | None, body: str, recipient: User | None) -> None:  # pragma: no cover
        raise NotImplementedError


class InAppChannel(Channel):
    type = "IN_APP"
    SPEC = {}

    def send(self, subject, body, recipient):  # in-app rows are written in the action's transaction
        return None


class EmailChannel(Channel):
    type = "EMAIL"
    SPEC = {
        "host": (str, True, "SMTP host"),
        "port": (int, False, "SMTP port, default 587"),
        "from_address": (str, True, "Sender address"),
        "username": (str, False, "SMTP user name"),
        "password_env": (str, False, "Name of the environment variable holding the SMTP password"),
        "starttls": (bool, False, "Use STARTTLS (default true)"),
        "timeout_seconds": (int, False, "Connection timeout, default 10"),
    }
    smtp_factory = smtplib.SMTP  # replaced in tests

    def send(self, subject, body, recipient):
        if recipient is None or not recipient.email:
            raise DeliveryError("recipient has no email address")
        msg = EmailMessage()
        msg["From"] = self.settings["from_address"]
        msg["To"] = recipient.email
        msg["Subject"] = subject or "Ticket notification"
        msg.set_content(body)
        try:
            with self.smtp_factory(self.settings["host"], int(self.settings.get("port", 587)),
                                   timeout=int(self.settings.get("timeout_seconds", 10))) as smtp:
                if self.settings.get("starttls", True):
                    smtp.starttls()
                if self.settings.get("username"):
                    smtp.login(self.settings["username"], self.secret("password_env") or "")
                smtp.send_message(msg)
        except (OSError, smtplib.SMTPException) as e:
            raise DeliveryError(f"SMTP: {e}") from e


class ChatWebhookChannel(Channel):
    type = "CHAT_WEBHOOK"
    SPEC = {
        "url_env": (str, True, "Name of the environment variable holding the webhook URL"),
        "timeout_seconds": (int, False, "Request timeout, default 10"),
    }
    transport = None  # httpx transport; replaced in tests

    def send(self, subject, body, recipient):
        url = self.secret("url_env")
        if not url:
            raise DeliveryError(f"environment variable {self.settings.get('url_env')} is not set")
        text = f"*{subject}*\n{body}" if subject else body
        try:
            with httpx.Client(transport=self.transport, timeout=int(self.settings.get("timeout_seconds", 10))) as c:
                r = c.post(url, json={"text": text})
            if r.status_code >= 300:
                raise DeliveryError(f"webhook answered HTTP {r.status_code}")
        except httpx.HTTPError as e:
            raise DeliveryError(f"webhook: {type(e).__name__}") from e


REGISTRY: dict[str, type[Channel]] = {c.type: c for c in (InAppChannel, EmailChannel, ChatWebhookChannel)}


def adapter(row: NotificationChannel) -> Channel:
    return REGISTRY[row.type](row)


# ================================================================ outbox delivery (F8.5)
def backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(60, 2 ** max(0, attempts - 1)))


@jobs.sweep("outbox")
def deliver_outbox(db: Session, limit: int = 200) -> dict:
    now = clock.now()
    retry_max = settings.get(db, "notification.retry_max")
    rows = list(db.scalars(select(NotificationOutbox).where(NotificationOutbox.status == "PENDING",
                                                            NotificationOutbox.next_attempt_at <= now)
                           .order_by(NotificationOutbox.id).limit(limit).with_for_update(skip_locked=True)))
    sent = failed = 0
    for ob in rows:
        ch = db.get(NotificationChannel, ob.channel_id)
        n = db.get(Notification, ob.notification_id)
        recipient = db.get(User, n.user_id) if n else None
        if ch is None or not ch.enabled:
            ob.status, ob.last_error = "FAILED", "channel disabled"
            failed += 1
            continue
        ob.attempts += 1
        try:
            with db.begin_nested():
                adapter(ch).send(ob.subject, ob.body, recipient)
            ob.status, ob.sent_at, ob.last_error = "SENT", now, None
            sent += 1
        except Exception as e:  # never blocks the ticket; retried with back-off
            ob.last_error = str(e)[:1000] or type(e).__name__
            ch.last_failure_at, ch.last_failure_text = now, ob.last_error
            if ob.attempts >= retry_max:
                ob.status = "FAILED"
                failed += 1
            else:
                ob.next_attempt_at = now + backoff(ob.attempts)
    return {"sent": sent, "failed": failed, "picked": len(rows)}


# ================================================================ administration (API-100..108)
def _platform(p: Principal):
    if not p.is_platform_admin:
        raise AppError("forbidden")


def list_channels(db: Session, p: Principal) -> list[NotificationChannel]:
    _platform(p)
    return list(db.scalars(select(NotificationChannel).order_by(NotificationChannel.id)))


def get_channel(db: Session, p: Principal, cid) -> NotificationChannel:
    _platform(p)
    return get_or_404(db, NotificationChannel, cid)


def channel_stats(db: Session, ch: NotificationChannel) -> dict:
    rows = dict(db.execute(select(NotificationOutbox.status, func.count()).where(
        NotificationOutbox.channel_id == ch.id).group_by(NotificationOutbox.status)).all())
    return {"pending": rows.get("PENDING", 0), "sent": rows.get("SENT", 0), "failed": rows.get("FAILED", 0)}


def create_channel(db: Session, p: Principal, data: dict) -> NotificationChannel:
    _platform(p)
    fields = {}
    typ = data.get("type")
    if typ not in REGISTRY or typ == "IN_APP":
        fields["type"] = "EMAIL or CHAT_WEBHOOK (the in-app channel exists already)"
    name = text_field(fields, data, "name")
    raise_if(fields)
    clean, f2 = REGISTRY[typ].validate(data.get("settings") or {})
    raise_if(f2)
    if db.scalar(select(NotificationChannel.id).where(func.lower(NotificationChannel.name) == name.lower())):
        raise AppError("validation_failed", fields={"name": "already exists"})
    ch = NotificationChannel(type=typ, name=name, settings=clean, enabled=data.get("enabled", True) is not False)
    db.add(ch)
    db.flush()
    audit.record(db, p.id, "create", "notification_channel", ch.id, None, audit.snapshot(ch))
    return ch


def update_channel(db: Session, p: Principal, cid, data: dict) -> NotificationChannel:
    ch = get_channel(db, p, cid)
    old = audit.snapshot(ch)
    fields = {}
    if "name" in data:
        name = text_field(fields, data, "name")
        if name:
            ch.name = name
    if "settings" in data:
        clean, f2 = REGISTRY[ch.type].validate(data.get("settings") or {})
        fields.update(f2)
        if not f2:
            ch.settings = clean
    if "enabled" in data:
        v = bool_field(fields, data, "enabled")
        if v is False and ch.type == "IN_APP":
            fields["enabled"] = "the in-app channel is always on"
        elif v is not None:
            ch.enabled = v
    raise_if(fields)
    audit.record(db, p.id, "update", "notification_channel", ch.id, old, audit.snapshot(ch))
    return ch


def test_channel(db: Session, p: Principal, cid) -> dict:
    ch = get_channel(db, p, cid)
    if ch.type == "IN_APP":
        return {"ok": True, "detail": "in-app alerts are written directly"}
    try:
        adapter(ch).send("Test message", "This is a test message from the ticketing system.", p.user)
        return {"ok": True, "detail": None}
    except Exception as e:
        ch.last_failure_at, ch.last_failure_text = clock.now(), str(e)[:1000]
        return {"ok": False, "detail": str(e)[:1000]}


def list_templates(db: Session, p: Principal, channel_id=None, notification_type=None) -> list[NotificationTemplate]:
    _platform(p)
    q = select(NotificationTemplate).order_by(NotificationTemplate.notification_type, NotificationTemplate.channel_id)
    if channel_id:
        q = q.where(NotificationTemplate.channel_id == int(channel_id))
    if notification_type:
        q = q.where(NotificationTemplate.notification_type == notification_type)
    return list(db.scalars(q))


def _check_texts(fields, subject, body):
    unknown = notify.check_placeholders(subject or "", body or "")
    if unknown:
        fields["body"] = "unknown placeholders: " + ", ".join(unknown) + "; allowed: " + ", ".join(notify.PLACEHOLDERS)


def create_template(db: Session, p: Principal, data: dict) -> NotificationTemplate:
    _platform(p)
    fields = {}
    ntype = data.get("notification_type")
    if ntype not in notify.TYPES:
        fields["notification_type"] = f"one of {', '.join(notify.TYPES)}"
    ch = db.get(NotificationChannel, data.get("channel_id")) if data.get("channel_id") else None
    if ch is None:
        fields["channel_id"] = "unknown channel"
    body = text_field(fields, data, "body", 5000)
    subject = text_field(fields, data, "subject", 300, required=False)
    _check_texts(fields, subject, body)
    raise_if(fields)
    if db.scalar(select(NotificationTemplate.id).where(NotificationTemplate.notification_type == ntype,
                                                       NotificationTemplate.channel_id == ch.id)):
        raise AppError("validation_failed", fields={"notification_type": "a template for this type and channel exists"})
    tpl = NotificationTemplate(notification_type=ntype, channel_id=ch.id, subject=subject, body=body,
                               enabled=data.get("enabled", True) is not False)
    db.add(tpl)
    db.flush()
    audit.record(db, p.id, "create", "notification_template", tpl.id, None, audit.snapshot(tpl))
    return tpl


def update_template(db: Session, p: Principal, tid, data: dict) -> NotificationTemplate:
    _platform(p)
    tpl = get_or_404(db, NotificationTemplate, tid)
    old = audit.snapshot(tpl)
    fields = {}
    subject = tpl.subject
    body = tpl.body
    if "subject" in data:
        subject = text_field(fields, data, "subject", 300, required=False)
    if "body" in data:
        body = text_field(fields, data, "body", 5000)
    _check_texts(fields, subject, body)
    if "enabled" in data:
        v = bool_field(fields, data, "enabled")
        ch = db.get(NotificationChannel, tpl.channel_id)
        if v is False and ch.type == "IN_APP":
            fields["enabled"] = "in-app alerts are always on; edit the text instead"
        elif v is not None:
            tpl.enabled = v
    raise_if(fields)
    tpl.subject, tpl.body = subject, body
    audit.record(db, p.id, "update", "notification_template", tpl.id, old, audit.snapshot(tpl))
    return tpl


SAMPLE = {"ticket_number": "ACM-42", "title": "Cannot sign in to the portal", "status": "In progress",
          "priority": "High", "resolver": "Rob Resolver", "due_time": "2026-11-17 10:00 Europe/London",
          "link": "https://tickets.example.com/tickets/42"}


def preview_template(db: Session, p: Principal, tid, data: dict | None = None) -> dict:
    _platform(p)
    tpl = get_or_404(db, NotificationTemplate, tid)
    subject = (data or {}).get("subject", tpl.subject)
    body = (data or {}).get("body", tpl.body)
    unknown = notify.check_placeholders(subject or "", body or "")
    return {"subject": notify.render(subject, SAMPLE), "body": notify.render(body, SAMPLE), "sample": SAMPLE,
            "unknown_placeholders": unknown}
