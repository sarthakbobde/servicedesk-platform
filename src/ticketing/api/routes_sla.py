"""G11 Ticket SLA (API-088..091) and G12 SLA configuration, policies and steps (API-092..097)."""
from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import Calendar, Priority, TicketType
from ..permissions import Principal
from ..services import sla, tickets
from . import serialize as S
from .deps import TxRoute, get_db, person, staff_only
from .routes_tickets import clean, guard

router = APIRouter(prefix="/api/v1", route_class=TxRoute)


class PauseIn(BaseModel):
    reason: str = Field(max_length=5000)


class ExtendIn(BaseModel):
    step: int = Field(description="Step position")
    minutes: int = Field(ge=1, le=10_000)
    reason: str = Field(max_length=5000)


def _timers(db, p, t):
    return clean(sla.read_timers(db, p, t))


@router.get("/tickets/{ref}/sla", tags=["G11 Ticket SLA"], summary="API-088 Timers: step, due time, state, time left")
def get_sla(ref: str, p: Principal = Depends(person), db: Session = Depends(get_db)):
    t = tickets.load(db, staff_only(p), ref)
    return _timers(db, p, t)


@router.post("/tickets/{ref}/sla/pause", tags=["G11 Ticket SLA"], summary="API-089 Manual pause with a reason")
def pause(ref: str, body: PauseIn, p: Principal = Depends(guard("sla-pause")), db: Session = Depends(get_db)):
    t = tickets.perform(db, p, ref, "sla-pause", body.model_dump())
    return _timers(db, p, t)


@router.post("/tickets/{ref}/sla/resume", tags=["G11 Ticket SLA"], summary="API-090 Resume a manual pause")
def resume(ref: str, p: Principal = Depends(guard("sla-resume")), db: Session = Depends(get_db)):
    t = tickets.perform(db, p, ref, "sla-resume", {})
    return _timers(db, p, t)


@router.post("/tickets/{ref}/sla/extend", tags=["G11 Ticket SLA"], summary="API-091 Add minutes to a step")
def extend(ref: str, body: ExtendIn, p: Principal = Depends(guard("sla-extend")), db: Session = Depends(get_db)):
    t = tickets.perform(db, p, ref, "sla-extend", body.model_dump())
    return _timers(db, p, t)


# ---------------------------------------------------------------- policies (G12)
G12 = "G12 SLA configuration"


def admin(p: Principal = Depends(person)) -> Principal:
    return staff_only(p)


def policy_view(db: Session, pol, with_steps: bool = True) -> dict:
    prio = db.get(Priority, pol.priority_id)
    tt = db.get(TicketType, pol.ticket_type_id) if pol.ticket_type_id else None
    cal = db.get(Calendar, pol.calendar_id) if pol.calendar_id else None
    steps = sla.policy_metrics(db, pol.id)
    d = {"id": pol.id, "name": pol.name, "priority": {"id": prio.id, "name": prio.name},
         "ticket_type": {"id": tt.id, "name": tt.name, "project_id": tt.project_id} if tt else None,
         "is_default": tt is None,
         "calendar": {"id": cal.id, "name": cal.name, "time_zone": cal.time_zone} if cal else None,
         "total_minutes": steps[-1].minutes if steps else None, "is_active": pol.is_active,
         "created_at": S.ts(pol.created_at)}
    if with_steps:
        d["steps"] = [sla.metric_view(m) for m in steps]
    return d


@router.get("/admin/sla-policies", tags=[G12], summary="API-092 List SLA policies (missing=true lists gaps)")
def list_policies(priority_id: int | None = None, ticket_type_id: int | None = None, project_id: int | None = None,
                  active: bool | None = None, defaults: bool | None = None, missing: bool = False, page: int = 1,
                  page_size: int = 100, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    if missing:
        rows = sla.missing_combinations(db, p)
        return {"items": rows, "page": 1, "page_size": len(rows), "total": len(rows)}
    rows, meta = sla.list_policies(db, p, priority_id, ticket_type_id, project_id, active, defaults, page, page_size)
    return S.page([policy_view(db, x, with_steps=False) for x in rows], meta)


@router.post("/admin/sla-policies", status_code=201, tags=[G12], summary="API-093 Create a policy")
def create_policy(body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return policy_view(db, sla.create_policy(db, p, body))


@router.get("/admin/sla-policies/{pid}", tags=[G12], summary="API-094 One SLA policy")
def get_policy(pid: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return policy_view(db, sla.get_policy(db, p, pid))


@router.patch("/admin/sla-policies/{pid}", tags=[G12], summary="API-095 Change or deactivate (new tickets only)")
def patch_policy(pid: int, body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    return policy_view(db, sla.update_policy(db, p, pid, body))


@router.get("/admin/sla-policies/{pid}/metrics", tags=[G12], summary="API-096 Ordered steps")
def get_metrics(pid: int, p: Principal = Depends(admin), db: Session = Depends(get_db)):
    pol = sla.get_policy(db, p, pid)
    return {"steps": [sla.metric_view(m) for m in sla.policy_metrics(db, pol.id)]}


@router.put("/admin/sla-policies/{pid}/metrics", tags=[G12], summary="API-097 Replace the steps; validated as a whole")
def put_metrics(pid: int, body: dict = Body(...), p: Principal = Depends(admin), db: Session = Depends(get_db)):
    if not isinstance(body, dict):
        raise AppError("validation_failed", fields={"steps": "required"})
    return {"steps": [sla.metric_view(m) for m in sla.put_metrics(db, p, pid, body)]}
