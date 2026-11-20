"""Core configuration (G10): ticket types, priorities, statuses (rename only), calendars with hours and holidays.
Also the loader that turns a calendar row into a `worktime.CalendarSpec`."""
from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .. import audit, worktime
from ..errors import AppError, not_found
from ..models import (
    Calendar, CalendarHoliday, CalendarHours, Priority, Project, RoleAssignment, SlaPolicy, Team, TicketStatus,
    TicketType,
)
from ..permissions import Principal
from .common import bool_field, get_or_404, int_field, paginate, raise_if, text_field


def _platform(p: Principal):
    if not p.is_platform_admin:
        raise AppError("forbidden")


# ================================================================ ticket types
def list_ticket_types(db: Session, p: Principal, project_id=None, active=None, page=1, page_size=100, sort=None):
    if not p.any_admin():
        raise AppError("forbidden")
    q = select(TicketType)
    ids = p.admin_project_ids()
    if ids is not None:
        q = q.where(TicketType.project_id.in_(ids or {-1}))
    if project_id:
        q = q.where(TicketType.project_id == int(project_id))
    if active is not None:
        q = q.where(TicketType.is_active.is_(active))
    return paginate(db, q, page, page_size, sort, {"name": TicketType.name, "id": TicketType.id},
                    [TicketType.project_id, TicketType.name])


def get_ticket_type(db: Session, p: Principal, type_id) -> TicketType:
    tt = get_or_404(db, TicketType, type_id)
    if not p.admin_of_project(tt.project_id):
        raise not_found()
    return tt


def _team_in_project(db: Session, fields: dict, team_id, project_id: int):
    team = db.get(Team, team_id) if team_id else None
    if team is None or team.project_id != project_id:
        fields["team_id"] = "must be a team of the same project"
        return None
    if not team.is_active:
        fields["team_id"] = "the team is inactive"
        return None
    return team


def create_ticket_type(db: Session, p: Principal, data: dict) -> TicketType:
    if not p.any_admin():
        raise AppError("forbidden")
    fields = {}
    proj = db.get(Project, data.get("project_id")) if data.get("project_id") else None
    if proj is None:
        raise AppError("validation_failed", fields={"project_id": "required"})
    if not p.admin_of_project(proj.id):
        raise AppError("forbidden")
    name = text_field(fields, data, "name")
    team = _team_in_project(db, fields, data.get("team_id"), proj.id)
    mode = data.get("assignment_mode")
    if mode not in ("MANUAL", "AUTO"):
        fields["assignment_mode"] = "MANUAL or AUTO"
    desc = text_field(fields, data, "description", 2000, required=False)
    raise_if(fields)
    if db.scalar(select(TicketType.id).where(TicketType.project_id == proj.id,
                                             func.lower(TicketType.name) == name.lower())):
        raise AppError("validation_failed", fields={"name": "already exists in this project"})
    tt = TicketType(project_id=proj.id, team_id=team.id, name=name, assignment_mode=mode, description=desc)
    db.add(tt)
    db.flush()
    audit.record(db, p.id, "create", "ticket_type", tt.id, None, audit.snapshot(tt), proj.organization_id, proj.id)
    return tt


def update_ticket_type(db: Session, p: Principal, type_id, data: dict) -> TicketType:
    tt = get_ticket_type(db, p, type_id)
    old = audit.snapshot(tt)
    fields = {}
    if "name" in data:
        name = text_field(fields, data, "name")
        if name and db.scalar(select(TicketType.id).where(TicketType.project_id == tt.project_id,
                                                         func.lower(TicketType.name) == name.lower(),
                                                         TicketType.id != tt.id)):
            fields["name"] = "already exists in this project"
        elif name:
            tt.name = name
    if "team_id" in data:
        team = _team_in_project(db, fields, data.get("team_id"), tt.project_id)
        if team:
            tt.team_id = team.id
    if "assignment_mode" in data:
        if data["assignment_mode"] not in ("MANUAL", "AUTO"):
            fields["assignment_mode"] = "MANUAL or AUTO"
        else:
            tt.assignment_mode = data["assignment_mode"]
    if "description" in data:
        tt.description = text_field(fields, data, "description", 2000, required=False)
    if "is_active" in data:
        v = bool_field(fields, data, "is_active")
        if v is not None:
            tt.is_active = v
    raise_if(fields)
    audit.record(db, p.id, "update", "ticket_type", tt.id, old, audit.snapshot(tt), p.org_of(tt.project_id),
                 tt.project_id)
    return tt


# ================================================================ priorities
def default_priority(db: Session) -> Priority | None:
    return db.scalar(select(Priority).where(Priority.is_default.is_(True), Priority.is_active.is_(True)))


def list_priorities(db: Session, active_only: bool = False) -> list[Priority]:
    q = select(Priority).order_by(Priority.rank)
    if active_only:
        q = q.where(Priority.is_active.is_(True))
    return list(db.scalars(q))


def get_priority(db: Session, p: Principal, pid) -> Priority:
    _platform(p)
    return get_or_404(db, Priority, pid)


def _set_default_priority(db: Session, pr: Priority):
    db.execute(update(Priority).where(Priority.id != pr.id, Priority.is_default.is_(True)).values(is_default=False))
    db.flush()
    pr.is_default = True


def create_priority(db: Session, p: Principal, data: dict) -> Priority:
    _platform(p)
    fields = {}
    name = text_field(fields, data, "name", 100)
    rank = int_field(fields, data, "rank", 1, 1000, required=True)
    raise_if(fields)
    if db.scalar(select(Priority.id).where(func.lower(Priority.name) == name.lower())):
        fields["name"] = "already exists"
    if db.scalar(select(Priority.id).where(Priority.rank == rank)):
        fields["rank"] = "already used"
    raise_if(fields)
    pr = Priority(name=name, rank=rank, is_default=False, is_active=True)
    db.add(pr)
    db.flush()
    if data.get("is_default") is True:
        _set_default_priority(db, pr)
    audit.record(db, p.id, "create", "priority", pr.id, None, audit.snapshot(pr))
    return pr


def update_priority(db: Session, p: Principal, pid, data: dict) -> Priority:
    pr = get_priority(db, p, pid)
    old = audit.snapshot(pr)
    fields = {}
    if "name" in data:
        name = text_field(fields, data, "name", 100)
        if name and db.scalar(select(Priority.id).where(func.lower(Priority.name) == name.lower(), Priority.id != pr.id)):
            fields["name"] = "already exists"
        elif name:
            pr.name = name
    if "rank" in data:
        rank = int_field(fields, data, "rank", 1, 1000, required=True)
        if rank is not None and db.scalar(select(Priority.id).where(Priority.rank == rank, Priority.id != pr.id)):
            fields["rank"] = "already used"
        elif rank is not None:
            pr.rank = rank
    if "is_active" in data:
        v = bool_field(fields, data, "is_active")
        if v is False and pr.is_default:
            fields["is_active"] = "the default priority cannot be deactivated; set another default first"
        elif v is not None:
            pr.is_active = v
    if data.get("is_default") is True:
        if not pr.is_active:
            fields["is_default"] = "an inactive priority cannot be the default"
        else:
            _set_default_priority(db, pr)
    elif data.get("is_default") is False and pr.is_default:
        fields["is_default"] = "exactly one default: set another priority as default instead"
    raise_if(fields)
    audit.record(db, p.id, "update", "priority", pr.id, old, audit.snapshot(pr))
    return pr


# ================================================================ statuses
def list_statuses(db: Session) -> list[TicketStatus]:
    return list(db.scalars(select(TicketStatus).order_by(TicketStatus.sort_order)))


def status_map(db: Session) -> dict[str, TicketStatus]:
    return {s.code: s for s in list_statuses(db)}


def get_status(db: Session, p: Principal, sid) -> TicketStatus:
    _platform(p)
    return get_or_404(db, TicketStatus, sid)


def update_status(db: Session, p: Principal, sid, data: dict) -> TicketStatus:
    st = get_status(db, p, sid)
    locked = set(data) - {"name"}
    if locked:
        raise AppError("validation_failed", "Only the name can change (D26).",
                       fields={k: "locked" for k in locked})
    fields = {}
    old = audit.snapshot(st)
    name = text_field(fields, data, "name", 100)
    raise_if(fields)
    st.name = name
    audit.record(db, p.id, "update", "ticket_status", st.id, old, audit.snapshot(st))
    return st


# ================================================================ calendars
def default_calendar(db: Session) -> Calendar | None:
    return db.scalar(select(Calendar).where(Calendar.is_default.is_(True)))


def calendar_spec(db: Session, calendar_id: int | None, _cache: dict | None = None) -> worktime.CalendarSpec:
    """Spec for a calendar id; None (or inactive) falls back to the default calendar."""
    key = calendar_id or 0
    if _cache is not None and key in _cache:
        return _cache[key]
    cal = db.get(Calendar, calendar_id) if calendar_id else None
    if cal is None or not cal.is_active:
        cal = default_calendar(db)
    if cal is None:
        spec = worktime.CalendarSpec(time_zone="UTC", windows={}, holidays={})
    else:
        windows: dict[int, list] = {}
        for h in db.scalars(select(CalendarHours).where(CalendarHours.calendar_id == cal.id)):
            windows.setdefault(h.weekday, []).append((h.start_minute, h.end_minute))
        holidays: dict[date, list | None] = {}
        for hol in db.scalars(select(CalendarHoliday).where(CalendarHoliday.calendar_id == cal.id,
                                                            CalendarHoliday.is_active.is_(True))):
            if hol.start_minute is None:
                holidays[hol.day] = None
            elif holidays.get(hol.day, ()) is not None:
                holidays.setdefault(hol.day, []).append((hol.start_minute, hol.end_minute))
        spec = worktime.CalendarSpec(
            time_zone=cal.time_zone,
            windows={k: tuple(sorted(v)) for k, v in windows.items()},
            holidays={k: (tuple(v) if v is not None else None) for k, v in holidays.items()})
    if _cache is not None:
        _cache[key] = spec
    return spec


def calendar_time_zone(db: Session, calendar_id: int | None) -> str:
    cal = db.get(Calendar, calendar_id) if calendar_id else None
    if cal is None or not cal.is_active:
        cal = default_calendar(db)
    return cal.time_zone if cal else "UTC"


def _tz(fields: dict, value):
    if not value:
        fields["time_zone"] = "required (IANA name, e.g. Europe/London)"
        return None
    try:
        ZoneInfo(str(value))
        return str(value)
    except (ZoneInfoNotFoundError, ValueError):
        fields["time_zone"] = "unknown IANA time zone"
        return None


def list_calendars(db: Session, p: Principal, active=None, page=1, page_size=100, sort=None):
    _platform(p)
    q = select(Calendar)
    if active is not None:
        q = q.where(Calendar.is_active.is_(active))
    return paginate(db, q, page, page_size, sort, {"name": Calendar.name, "id": Calendar.id}, Calendar.name)


def calendar_usage(db: Session, cal: Calendar) -> dict:
    people = db.scalar(select(func.count(func.distinct(RoleAssignment.user_id))).where(
        RoleAssignment.calendar_id == cal.id, RoleAssignment.is_active.is_(True)))
    if cal.is_default:
        people += db.scalar(select(func.count(func.distinct(RoleAssignment.user_id))).where(
            RoleAssignment.calendar_id.is_(None), RoleAssignment.scope == "TEAM", RoleAssignment.is_active.is_(True)))
    policies = db.scalar(select(func.count()).select_from(SlaPolicy).where(SlaPolicy.calendar_id == cal.id,
                                                                            SlaPolicy.is_active.is_(True)))
    return {"people": people, "sla_policies": policies}


def get_calendar(db: Session, p: Principal, cid) -> Calendar:
    _platform(p)
    return get_or_404(db, Calendar, cid)


def _set_default_calendar(db: Session, cal: Calendar):
    db.execute(update(Calendar).where(Calendar.id != cal.id, Calendar.is_default.is_(True)).values(is_default=False))
    db.flush()
    cal.is_default = True


def create_calendar(db: Session, p: Principal, data: dict) -> Calendar:
    _platform(p)
    fields = {}
    name = text_field(fields, data, "name")
    tz = _tz(fields, data.get("time_zone"))
    raise_if(fields)
    if db.scalar(select(Calendar.id).where(func.lower(Calendar.name) == name.lower())):
        raise AppError("validation_failed", fields={"name": "already exists"})
    cal = Calendar(name=name, time_zone=tz, is_default=False, is_active=True)
    db.add(cal)
    db.flush()
    if data.get("is_default") is True:
        _set_default_calendar(db, cal)
    audit.record(db, p.id, "create", "calendar", cal.id, None, audit.snapshot(cal))
    return cal


def update_calendar(db: Session, p: Principal, cid, data: dict) -> Calendar:
    cal = get_calendar(db, p, cid)
    old = audit.snapshot(cal)
    fields = {}
    if "name" in data:
        name = text_field(fields, data, "name")
        if name and db.scalar(select(Calendar.id).where(func.lower(Calendar.name) == name.lower(), Calendar.id != cal.id)):
            fields["name"] = "already exists"
        elif name:
            cal.name = name
    if "time_zone" in data:
        tz = _tz(fields, data.get("time_zone"))
        if tz:
            cal.time_zone = tz
    if "is_active" in data:
        v = bool_field(fields, data, "is_active")
        if v is False and cal.is_default:
            fields["is_active"] = "the default calendar cannot be deactivated"
        elif v is not None:
            cal.is_active = v
    if data.get("is_default") is True:
        if not cal.is_active:
            fields["is_default"] = "an inactive calendar cannot be the default"
        else:
            _set_default_calendar(db, cal)
    elif data.get("is_default") is False and cal.is_default:
        fields["is_default"] = "exactly one default: set another calendar as default instead"
    raise_if(fields)
    audit.record(db, p.id, "update", "calendar", cal.id, old, audit.snapshot(cal))
    return cal


def get_hours(db: Session, p: Principal, cid) -> list[CalendarHours]:
    cal = get_calendar(db, p, cid)
    return list(db.scalars(select(CalendarHours).where(CalendarHours.calendar_id == cal.id)
                           .order_by(CalendarHours.weekday, CalendarHours.start_minute)))


def put_hours(db: Session, p: Principal, cid, data) -> list[CalendarHours]:
    """Replace the weekly pattern as a whole. No overlaps within a day; end < start runs past midnight."""
    cal = get_calendar(db, p, cid)
    windows = data.get("windows") if isinstance(data, dict) else data
    if not isinstance(windows, list):
        raise AppError("validation_failed", fields={"windows": "a list of {weekday, start, end}"})
    fields, parsed = {}, []
    for i, w in enumerate(windows):
        try:
            wd = int(w.get("weekday"))
            s, e = worktime.parse_hhmm(w.get("start", "")), worktime.parse_hhmm(w.get("end", ""))
            if not 1 <= wd <= 7:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            fields[f"windows.{i}"] = "weekday 1 to 7, start and end as HH:MM"
            continue
        if s == e or (s == 1440):
            fields[f"windows.{i}"] = "start and end must differ (00:00 to 24:00 is a full day)"
            continue
        parsed.append((i, wd, s, e))
    # overlap check on a per-day minute line (overnight windows spill into the next weekday)
    occupied: dict[int, list] = {}
    for i, wd, s, e in parsed:
        segs = [(wd, s, e)] if e > s else [(wd, s, 1440), (wd % 7 + 1, 0, e)]
        for d, a, b in segs:
            for (a2, b2, j) in occupied.get(d, []):
                if a < b2 and a2 < b:
                    fields[f"windows.{i}"] = f"overlaps window {j}"
            occupied.setdefault(d, []).append((a, b, i))
    raise_if(fields)
    old = [{"weekday": h.weekday, "start": worktime.fmt_hhmm(h.start_minute), "end": worktime.fmt_hhmm(h.end_minute)}
           for h in get_hours(db, p, cid)]
    for h in db.scalars(select(CalendarHours).where(CalendarHours.calendar_id == cal.id)):
        db.delete(h)
    db.flush()
    for _, wd, s, e in parsed:
        db.add(CalendarHours(calendar_id=cal.id, weekday=wd, start_minute=s, end_minute=e))
    db.flush()
    new = [{"weekday": wd, "start": worktime.fmt_hhmm(s), "end": worktime.fmt_hhmm(e)} for _, wd, s, e in parsed]
    audit.record(db, p.id, "update", "calendar_hours", cal.id, {"windows": old}, {"windows": new})
    return get_hours(db, p, cid)


def list_holidays(db: Session, p: Principal, cid, include_inactive=False) -> list[CalendarHoliday]:
    cal = get_calendar(db, p, cid)
    q = select(CalendarHoliday).where(CalendarHoliday.calendar_id == cal.id).order_by(CalendarHoliday.day)
    if not include_inactive:
        q = q.where(CalendarHoliday.is_active.is_(True))
    return list(db.scalars(q))


def _holiday_fields(fields: dict, data: dict, partial: bool = False):
    out = {}
    if "date" in data or not partial:
        try:
            out["day"] = date.fromisoformat(str(data.get("date")))
        except (TypeError, ValueError):
            fields["date"] = "YYYY-MM-DD"
    if "name" in data or not partial:
        out["name"] = text_field(fields, data, "name")
    if "start" in data or "end" in data or not partial:
        s, e = data.get("start"), data.get("end")
        if s in (None, "") and e in (None, ""):
            out["start_minute"] = out["end_minute"] = None
        else:
            try:
                out["start_minute"], out["end_minute"] = worktime.parse_hhmm(s), worktime.parse_hhmm(e)
                if out["start_minute"] >= out["end_minute"]:
                    fields["end"] = "after start (a partial day stays within the date)"
            except (AttributeError, TypeError, ValueError):
                fields["start"] = "HH:MM for both start and end, or omit both for a whole day"
    return out


def add_holiday(db: Session, p: Principal, cid, data: dict) -> CalendarHoliday:
    cal = get_calendar(db, p, cid)
    fields = {}
    vals = _holiday_fields(fields, data)
    raise_if(fields)
    hol = CalendarHoliday(calendar_id=cal.id, is_active=True, **vals)
    db.add(hol)
    db.flush()
    audit.record(db, p.id, "create", "calendar_holiday", hol.id, None, audit.snapshot(hol))
    return hol


def update_holiday(db: Session, p: Principal, hid, data: dict) -> CalendarHoliday:
    _platform(p)
    hol = get_or_404(db, CalendarHoliday, hid)
    old = audit.snapshot(hol)
    fields = {}
    vals = _holiday_fields(fields, data, partial=True)
    if "is_active" in data:
        v = bool_field(fields, data, "is_active")
        if v is not None:
            vals["is_active"] = v
    raise_if(fields)
    for k, v in vals.items():
        setattr(hol, k, v)
    audit.record(db, p.id, "update", "calendar_holiday", hol.id, old, audit.snapshot(hol))
    return hol
