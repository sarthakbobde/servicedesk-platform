"""G10 Core configuration (API-067..087, API-076 removed): ticket types, priorities, statuses, calendars."""
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends
from sqlalchemy.orm import Session

from .. import clock, worktime
from ..errors import AppError
from ..permissions import Principal
from ..services import configuration as cfg
from . import serialize as S
from .deps import TxRoute, get_db, person, staff_only

router = APIRouter(prefix="/api/v1/admin", route_class=TxRoute, tags=["G10 Core configuration"])


def admin(p: Principal = Depends(person)) -> Principal:
    return staff_only(p)


def platform(p: Principal = Depends(person)) -> Principal:
    staff_only(p)
    if not p.is_platform_admin:
        raise AppError("forbidden")
    return p


def _bool(v):
    if v in (None, ""):
        return None
    return str(v).lower() in ("1", "true", "yes", "on")


def ticket_type(tt) -> dict:
    return {"id": tt.id, "project_id": tt.project_id, "team_id": tt.team_id, "name": tt.name,
            "description": tt.description, "assignment_mode": tt.assignment_mode, "is_active": tt.is_active}


def priority(x) -> dict:
    return {"id": x.id, "name": x.name, "rank": x.rank, "is_default": x.is_default, "is_active": x.is_active}


def status(x) -> dict:
    return {"id": x.id, "code": x.code, "name": x.name, "category": x.category, "order": x.sort_order,
            "is_core": x.is_core}


def calendar(c, usage=None) -> dict:
    d = {"id": c.id, "name": c.name, "time_zone": c.time_zone, "is_default": c.is_default, "is_active": c.is_active}
    if usage is not None:
        d["usage"] = usage
    return d


def hours(rows) -> dict:
    return {"windows": [{"weekday": h.weekday, "start": worktime.fmt_hhmm(h.start_minute),
                         "end": worktime.fmt_hhmm(h.end_minute)} for h in rows]}


def holiday(h) -> dict:
    return {"id": h.id, "calendar_id": h.calendar_id, "date": h.day.isoformat(), "name": h.name,
            "start": worktime.fmt_hhmm(h.start_minute), "end": worktime.fmt_hhmm(h.end_minute),
            "whole_day": h.start_minute is None, "is_active": h.is_active}


def listing(rows, fn):
    return {"items": [fn(x) for x in rows], "page": 1, "page_size": len(rows), "total": len(rows)}


# ---------------------------------------------------------------- ticket types
@router.get("/ticket-types", summary="API-067 List ticket types")
def list_types(project_id: int | None = None, active: str | None = None, page: int = 1, page_size: int = 100,
               p: Principal = Depends(admin), db: Session = Depends(get_db)):
    rows, meta = cfg.list_ticket_types(db, p, project_id, _bool(active), page, page_size)
    return S.page([ticket_type(x) for x in rows], meta)


@router.post("/ticket-types", status_code=201, summary="API-068 Create a ticket type")
def create_type(body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return ticket_type(cfg.create_ticket_type(db, p, body))


@router.get("/ticket-types/{type_id}", summary="API-069 One ticket type")
def get_type(type_id: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return ticket_type(cfg.get_ticket_type(db, p, type_id))


@router.patch("/ticket-types/{type_id}", summary="API-070 Change or deactivate (applies to new tickets only)")
def patch_type(type_id: int, body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return ticket_type(cfg.update_ticket_type(db, p, type_id, body))


# ---------------------------------------------------------------- priorities
@router.get("/priorities", summary="API-071 List priorities")
def list_priorities(p: Principal = Depends(platform), db: Session = Depends(get_db)):
    return listing(cfg.list_priorities(db), priority)


@router.post("/priorities", status_code=201, summary="API-072 Create a priority")
def create_priority(body: dict = Body(...), p: Principal = Depends(platform), db: Session = Depends(get_db)):
    return priority(cfg.create_priority(db, p, body))


@router.get("/priorities/{pid}", summary="API-073 One priority")
def get_priority(pid: int, p: Principal = Depends(platform), db: Session = Depends(get_db)):
    return priority(cfg.get_priority(db, p, pid))


@router.patch("/priorities/{pid}", summary="API-074 Change, set default or deactivate")
def patch_priority(pid: int, body: dict = Body(...), p: Principal = Depends(platform),
                   db: Session = Depends(get_db)):
    return priority(cfg.update_priority(db, p, pid, body))


# ---------------------------------------------------------------- statuses (API-076 removed, D26)
@router.get("/statuses", summary="API-075 List statuses")
def list_statuses(p: Principal = Depends(platform), db: Session = Depends(get_db)):
    return listing(cfg.list_statuses(db), status)


@router.get("/statuses/{sid}", summary="API-077 One status")
def get_status(sid: int, p: Principal = Depends(platform), db: Session = Depends(get_db)):
    return status(cfg.get_status(db, p, sid))


@router.patch("/statuses/{sid}", summary="API-078 Rename only")
def patch_status(sid: int, body: dict = Body(...), p: Principal = Depends(platform), db: Session = Depends(get_db)):
    return status(cfg.update_status(db, p, sid, body))


# ---------------------------------------------------------------- calendars
@router.get("/calendars", summary="API-079 List calendars")
def list_calendars(active: str | None = None, page: int = 1, page_size: int = 100,
                   p: Principal = Depends(platform), db: Session = Depends(get_db)):
    rows, meta = cfg.list_calendars(db, p, _bool(active), page, page_size)
    return S.page([calendar(c, cfg.calendar_usage(db, c)) for c in rows], meta)


@router.post("/calendars", status_code=201, summary="API-080 Create a calendar")
def create_calendar(body: dict = Body(...), p: Principal = Depends(platform), db: Session = Depends(get_db)):
    return calendar(cfg.create_calendar(db, p, body))


@router.get("/calendars/{cid}", summary="API-081 One calendar, with a two-week working-time preview")
def get_calendar(cid: int, p: Principal = Depends(platform), db: Session = Depends(get_db)):
    c = cfg.get_calendar(db, p, cid)
    out = calendar(c, cfg.calendar_usage(db, c))
    if c.is_active:
        spec = cfg.calendar_spec(db, c.id)
        today = clock.now().astimezone(ZoneInfo(c.time_zone)).date()
        out["preview"] = worktime.preview(spec, today, 14)
    return out


@router.patch("/calendars/{cid}", summary="API-082 Change, set default or deactivate")
def patch_calendar(cid: int, body: dict = Body(...), p: Principal = Depends(platform),
                   db: Session = Depends(get_db)):
    return calendar(cfg.update_calendar(db, p, cid, body))


@router.get("/calendars/{cid}/hours", summary="API-083 Weekly working windows")
def get_hours(cid: int, p: Principal = Depends(platform), db: Session = Depends(get_db)):
    return hours(cfg.get_hours(db, p, cid))


@router.put("/calendars/{cid}/hours", summary="API-084 Replace the weekly pattern")
def put_hours(cid: int, body: dict = Body(...), p: Principal = Depends(platform), db: Session = Depends(get_db)):
    return hours(cfg.put_hours(db, p, cid, body))


@router.get("/calendars/{cid}/holidays", summary="API-085 Holiday list")
def list_holidays(cid: int, include_inactive: bool = False, p: Principal = Depends(platform),
                  db: Session = Depends(get_db)):
    return listing(cfg.list_holidays(db, p, cid, include_inactive), holiday)


@router.post("/calendars/{cid}/holidays", status_code=201, summary="API-086 Add a holiday or partial day")
def add_holiday(cid: int, body: dict = Body(...), p: Principal = Depends(platform), db: Session = Depends(get_db)):
    return holiday(cfg.add_holiday(db, p, cid, body))


@router.patch("/calendar-holidays/{hid}", summary="API-087 Change or remove (is_active false)")
def patch_holiday(hid: int, body: dict = Body(...), p: Principal = Depends(platform),
                  db: Session = Depends(get_db)):
    return holiday(cfg.update_holiday(db, p, hid, body))
