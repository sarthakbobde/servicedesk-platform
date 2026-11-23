"""Everyday API: G5 reference data, G6 tickets, G7 ticket actions, G8 notifications, G9 product integration."""
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..errors import AppError
from ..models import ApiCredential, Ticket
from ..permissions import Principal
from ..services import configuration as cfg
from ..services import notify, reference, tickets
from . import serialize as S
from .deps import TxRoute, credential, get_db, person, person_or_product, staff_only

router = APIRouter(prefix="/api/v1", route_class=TxRoute)


def clean(obj):
    """Datetimes to ISO 8601 UTC with Z (A11.1)."""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    if isinstance(obj, datetime):
        return S.ts(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


# ================================================================ G5 reference data
@router.get("/projects", tags=["G5 Reference data"], summary="API-036 Projects the caller can raise in")
def projects(p: Principal = Depends(person_or_product), db: Session = Depends(get_db)):
    return {"items": reference.projects_for(db, p)}


@router.get("/projects/{project_id}/ticket-types", tags=["G5 Reference data"],
            summary="API-037 Active ticket types with owning team")
def ticket_types(project_id: int, p: Principal = Depends(person_or_product), db: Session = Depends(get_db)):
    return {"items": reference.ticket_types_for(db, p, project_id)}


@router.get("/priorities", tags=["G5 Reference data"], summary="API-038 Active priorities and the default")
def priorities(p: Principal = Depends(person_or_product), db: Session = Depends(get_db)):
    return {"items": [{"id": x.id, "name": x.name, "rank": x.rank, "is_default": x.is_default}
                      for x in cfg.list_priorities(db, active_only=True)]}


@router.get("/statuses", tags=["G5 Reference data"], summary="API-039 Statuses with name and category")
def statuses(p: Principal = Depends(person_or_product), db: Session = Depends(get_db)):
    return {"items": [{"code": s.code, "name": s.name, "category": s.category, "order": s.sort_order}
                      for s in cfg.list_statuses(db)]}


@router.get("/teams/{team_id}/members", tags=["G5 Reference data"],
            summary="API-040 Active resolvers and managers with on-shift flag")
def team_members(team_id: int, p: Principal = Depends(person), db: Session = Depends(get_db)):
    return {"items": reference.team_members(db, staff_only(p), team_id)}


# ================================================================ G6 tickets
class TicketIn(BaseModel):
    project_id: int | None = Field(None, description="Optional when the caller can raise in exactly one project")
    type_id: int
    priority_id: int | None = Field(None, description="Default priority when empty")
    title: str = Field(max_length=1000)
    description: str = Field(max_length=20000)


@router.post("/tickets", status_code=201, tags=["G6 Tickets"], summary="API-041 Raise a ticket")
def raise_ticket(body: TicketIn, response: Response, p: Principal = Depends(person_or_product),
                 db: Session = Depends(get_db), idempotency_key: str | None = Header(None)):
    source = "API" if p.via in ("TOKEN", "CREDENTIAL") else "PORTAL"
    t, created = tickets.create_ticket(db, p, body.model_dump(), source, idempotency_key)
    if not created:
        response.status_code = 200
    return clean(tickets.view(tickets.Ctx(db), p, t, with_relatives=True))


@router.get("/tickets", tags=["G6 Tickets"], summary="API-042 List within the caller's scope")
def list_tickets(request: Request, view: str | None = None, status: list[str] = Query(default=[]),
                 priority: int | None = None, type: int | None = None, team: int | None = None,
                 resolver: str | None = None, requester: int | None = None, overdue: bool | None = None,
                 q: str | None = None, created_from: str | None = None, created_to: str | None = None,
                 project: int | None = None, page: int = 1, page_size: int = 25, sort: str | None = None,
                 p: Principal = Depends(person_or_product), db: Session = Depends(get_db)):
    params = dict(view=view, status=status, priority=priority, type=type, team=team, resolver=resolver,
                  requester=requester, overdue=overdue, q=q, created_from=created_from, created_to=created_to,
                  project=project, page=page, page_size=page_size, sort=sort)
    items, meta = tickets.list_tickets(db, p, params)
    return clean(S.page(items, meta))


@router.get("/tickets/counts", tags=["G6 Tickets"], summary="API-045 Menu badge numbers")
def ticket_counts(p: Principal = Depends(person), db: Session = Depends(get_db)):
    return tickets.counts(db, p)


@router.get("/tickets/{ref}", tags=["G6 Tickets"], summary="API-043 One ticket with allowed actions")
def get_ticket(ref: str, p: Principal = Depends(person_or_product), db: Session = Depends(get_db)):
    t = tickets.load(db, p, ref)
    return clean(tickets.view(tickets.Ctx(db), p, t, with_relatives=True))


@router.get("/tickets/{ref}/events", tags=["G6 Tickets"], summary="API-044 History, newest first")
def ticket_events(ref: str, page: int = 1, page_size: int = 50, p: Principal = Depends(person_or_product),
                  db: Session = Depends(get_db)):
    items, meta, _ = tickets.events(db, p, ref, page, page_size)
    return clean(S.page(items, meta))


# ================================================================ G7 ticket actions
class ResolverIn(BaseModel):
    resolver_id: int


class ReasonIn(BaseModel):
    reason: str = Field(max_length=5000)


class QuestionIn(BaseModel):
    question: str = Field(max_length=5000)


class AnswerIn(BaseModel):
    answer: str = Field(max_length=5000)


class ResolveIn(BaseModel):
    note: str = Field(max_length=5000)
    actual_effort_minutes: int | None = Field(None, ge=0)


class RejectIn(BaseModel):
    reason_code: Literal["WRONG_TEAM", "DUPLICATE", "NOT_A_TICKET", "INFO_MISSING", "OTHER"]
    reason: str = Field(max_length=5000)


class ReassignIn(BaseModel):
    resolver_id: int
    reason: str = Field(max_length=5000)


class PriorityIn(BaseModel):
    priority_id: int


class EstimateIn(BaseModel):
    minutes: int = Field(ge=1, le=100_000)


def guard(action: str, auth=person):
    """Visibility and role are checked in a dependency, so they win over body validation (404/403 before 422)."""
    def dep(ref: str, p: Principal = Depends(auth), db: Session = Depends(get_db)) -> Principal:
        t = tickets.load(db, p, ref, action=action)
        if p.via == "CREDENTIAL" and action not in tickets.REQUESTER_ACTIONS:
            raise AppError("forbidden")
        if not tickets.role_allows(p, action, t):
            raise AppError("forbidden")
        return p
    return dep


def _act(db, p, ref, action, body: dict | None):
    t = tickets.perform(db, p, ref, action, body or {})
    return clean(tickets.view(tickets.Ctx(db), p, t, with_relatives=True))


G7 = "G7 Ticket actions"


@router.post("/tickets/{ref}/claim", tags=[G7], summary="API-046 Take an unassigned ticket from the queue")
def claim(ref: str, p: Principal = Depends(guard("claim")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "claim", None)


@router.post("/tickets/{ref}/assign", tags=[G7], summary="API-047 Assign a resolver to a NEW ticket")
def assign(ref: str, body: ResolverIn, p: Principal = Depends(guard("assign")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "assign", body.model_dump())


@router.post("/tickets/{ref}/start", tags=[G7], summary="API-048 ASSIGNED to IN_PROGRESS")
def start(ref: str, p: Principal = Depends(guard("start")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "start", None)


@router.post("/tickets/{ref}/hold", tags=[G7], summary="API-049 To ON_HOLD with a reason")
def hold(ref: str, body: ReasonIn, p: Principal = Depends(guard("hold")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "hold", body.model_dump())


@router.post("/tickets/{ref}/request-info", tags=[G7], summary="API-050 To AWAITING_REQUESTER with a question")
def request_info(ref: str, body: QuestionIn, p: Principal = Depends(guard("request-info")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "request-info", body.model_dump())


@router.post("/tickets/{ref}/resume", tags=[G7], summary="API-051 Back to IN_PROGRESS")
def resume(ref: str, p: Principal = Depends(guard("resume")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "resume", None)


@router.post("/tickets/{ref}/respond", tags=[G7], summary="API-052 Answer the question")
def respond(ref: str, body: AnswerIn, p: Principal = Depends(guard("respond", person_or_product)), db: Session = Depends(get_db)):
    return _act(db, p, ref, "respond", body.model_dump())


@router.post("/tickets/{ref}/resolve", tags=[G7], summary="API-053 To RESOLVED with a note and actual effort")
def resolve(ref: str, body: ResolveIn, p: Principal = Depends(guard("resolve")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "resolve", body.model_dump())


@router.post("/tickets/{ref}/reject", tags=[G7], summary="API-054 Decline with a reason")
def reject(ref: str, body: RejectIn, p: Principal = Depends(guard("reject")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "reject", body.model_dump())


@router.post("/tickets/{ref}/withdraw", tags=[G7], summary="API-055 Cancel with a reason")
def withdraw(ref: str, body: ReasonIn, p: Principal = Depends(guard("withdraw", person_or_product)), db: Session = Depends(get_db)):
    return _act(db, p, ref, "withdraw", body.model_dump())


@router.post("/tickets/{ref}/release", tags=[G7], summary="API-056 Give the ticket back to the queue")
def release(ref: str, p: Principal = Depends(guard("release")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "release", None)


@router.post("/tickets/{ref}/reassign", tags=[G7], summary="API-057 Move to another resolver of the same team")
def reassign(ref: str, body: ReassignIn, p: Principal = Depends(guard("reassign")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "reassign", body.model_dump())


@router.post("/tickets/{ref}/reopen", tags=[G7], summary="API-058 Back to the queue inside the reopen window")
def reopen(ref: str, body: ReasonIn, p: Principal = Depends(guard("reopen", person_or_product)), db: Session = Depends(get_db)):
    return _act(db, p, ref, "reopen", body.model_dump())


class ChildIn(BaseModel):
    type_id: int = Field(description="Any active type of the parent's project; it decides the child's team")
    priority_id: int | None = Field(None, description="The parent's priority when empty")
    title: str = Field(max_length=1000)
    description: str = Field(max_length=20000)


def child_guard(ref: str, p: Principal = Depends(person), db: Session = Depends(get_db)) -> Principal:
    """Visibility and role before body validation (404/403 before 422)."""
    t = tickets.load(db, p, ref)
    if not tickets.can_add_child(p, t):
        raise AppError("forbidden")
    return p


@router.post("/tickets/{ref}/children", status_code=201, tags=[G7],
             summary="API-111 Split work into a child ticket (one level, same project)")
def create_child(ref: str, body: ChildIn, p: Principal = Depends(child_guard), db: Session = Depends(get_db)):
    t = tickets.create_child(db, p, ref, body.model_dump(), "API" if p.via == "TOKEN" else "PORTAL")
    return clean(tickets.view(tickets.Ctx(db), p, t, with_relatives=True))


@router.post("/tickets/{ref}/priority", tags=[G7], summary="API-059 Change priority")
def priority(ref: str, body: PriorityIn, p: Principal = Depends(guard("priority")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "priority", body.model_dump())


@router.post("/tickets/{ref}/estimate", tags=[G7], summary="API-060 Set the estimated effort in minutes")
def estimate(ref: str, body: EstimateIn, p: Principal = Depends(guard("estimate")), db: Session = Depends(get_db)):
    return _act(db, p, ref, "estimate", body.model_dump())


# ================================================================ G8 notifications
G8 = "G8 Notifications"


@router.get("/notifications", tags=[G8], summary="API-061 Own alerts, newest first")
def notifications(unread: bool | None = None, page: int = 1, page_size: int = 25,
                  p: Principal = Depends(person_or_product), db: Session = Depends(get_db)):
    rows, meta = notify.inbox(db, p, unread, page, page_size)
    refs = {}
    for n in rows:
        if n.ticket_id and n.ticket_id not in refs:
            t = db.get(Ticket, n.ticket_id)
            refs[n.ticket_id] = {"id": t.id, "number": notify.ticket_number(db, t), "title": t.title}
    items = [{"id": n.id, "type": n.type, "ticket": refs.get(n.ticket_id), "subject": n.subject, "body": n.body,
              "is_read": n.is_read, "created_at": n.created_at} for n in rows]
    return clean(S.page(items, meta))


@router.get("/notifications/unread-count", tags=[G8], summary="API-062 For the bell")
def unread_count(p: Principal = Depends(person_or_product), db: Session = Depends(get_db)):
    return {"unread": notify.unread_count(db, p)}


@router.post("/notifications/read-all", status_code=204, tags=[G8], summary="API-064 Mark all read")
def read_all(p: Principal = Depends(person_or_product), db: Session = Depends(get_db)):
    notify.mark_all_read(db, p)
    return Response(status_code=204)


@router.post("/notifications/{nid}/read", status_code=204, tags=[G8], summary="API-063 Mark one read")
def read_one(nid: int, p: Principal = Depends(person_or_product), db: Session = Depends(get_db)):
    notify.mark_read(db, p, nid)
    return Response(status_code=204)


# ================================================================ G9 product integration
class ProductUserIn(BaseModel):
    """A product cannot set a password or a role through the API (F9.4): unknown fields are refused."""
    model_config = {"extra": "forbid"}

    name: str | None = Field(None, max_length=200)
    email: str | None = Field(None, max_length=320)
    is_active: bool | None = None


@router.put("/product-users/{external_id}", tags=["G9 Product integration"],
            summary="API-065 Create or update a product user by product ID")
def put_product_user(external_id: str, response: Response, body: ProductUserIn = Body(default=ProductUserIn()),
                     cred: ApiCredential = Depends(credential), db: Session = Depends(get_db)):
    user, created = reference.upsert_product_user(db, cred, external_id, body.model_dump(exclude_unset=True))
    response.status_code = 201 if created else 200
    return reference.product_user_view(db, user)


@router.get("/product-users/{external_id}", tags=["G9 Product integration"], summary="API-066 Look one up")
def get_product_user(external_id: str, cred: ApiCredential = Depends(credential), db: Session = Depends(get_db)):
    return reference.product_user_view(db, reference.get_product_user(db, cred, external_id))
