"""Server-rendered portal and admin portal (Phase 5, S01-S30). Every screen calls the same service functions and
the same Principal checks as the API, so their permission outcomes match (parity tests). No business rule here."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock, settings, worktime
from ..api.deps import SESSION_COOKIE, TxRoute, client_ip, csrf_token_for, get_db, login_limiter
from ..errors import AppError
from ..models import (
    Calendar, NotificationChannel, Organization, Priority, Project, Team, TicketStatus, TicketType, User,
)
from ..permissions import Principal, load_principal
from ..security import constant_eq
from ..services import auth as auth_service
from ..services import channels, escalation, notify, operations, reference, sla, structure as st, tickets
from ..services import configuration as cfg
from ..services.capabilities import capabilities

HERE = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
router = APIRouter(route_class=TxRoute, include_in_schema=False)


class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


# ================================================================ formatting helpers for templates
def fmt_time(value, tz: str | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    z = tz or "UTC"
    return value.astimezone(ZoneInfo(z)).strftime("%Y-%m-%d %H:%M ") + z


def fmt_minutes(m) -> str:
    if m is None:
        return ""
    h, mm = divmod(int(m), 60)
    return f"{h} h {mm} min" if h else f"{mm} min"


templates.env.filters["dt"] = fmt_time
templates.env.filters["mins"] = fmt_minutes
templates.env.globals["hhmm"] = worktime.fmt_hhmm


# ================================================================ auth for screens
def web_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    token = request.cookies.get(SESSION_COOKIE)
    found = auth_service.resolve_token(db, token, "SESSION") if token else None
    if not found:
        db.commit()
        raise Redirect("/login?" + urlencode({"next": request.url.path, **({"expired": 1} if token else {})}))
    user, row = found
    p = load_principal(db, user, via="SESSION", session=row)
    if user.must_change_password and request.url.path not in ("/account", "/logout"):
        raise Redirect("/account?force=1")
    if request.method == "POST":
        request.state.csrf_checked = False
    return p


async def check_csrf(request: Request, p: Principal) -> dict:
    form = dict(await request.form())
    sent = form.pop("csrf", "")
    if not sent or not constant_eq(sent, csrf_token_for(p.session.token_hash)):
        raise AppError("forbidden", "The form expired. Reload the page and try again.")
    return form


async def form_of(request: Request, p: Principal) -> dict:
    multi = await request.form()
    form = await check_csrf(request, p)
    form["_multi"] = multi
    return form


def current_project(request: Request, p: Principal, db: Session) -> int | None:
    v = request.query_params.get("project") or request.cookies.get("proj")
    try:
        return int(v) if v not in (None, "", "all") else None
    except ValueError:
        return None


def shell(request: Request, p: Principal, db: Session, **extra) -> dict:
    caps = capabilities(p)
    projects = reference.projects_for(db, p) if not p.is_product_user else []
    staff_projects = set()
    for a in p.assignments:
        if a.project_id:
            staff_projects.add(a.project_id)
    sel = current_project(request, p, db)
    role = "Requester"
    if p.is_platform_admin:
        role = "Platform admin"
    elif sel:
        roles = [a.role for a in p.assignments if a.project_id == sel or (a.role == "ORG_ADMIN" and
                                                                          a.organization_id == p.org_of(sel))]
        role = ", ".join(sorted({r.replace("_", " ").title() for r in roles})) or "Requester"
    elif p.assignments:
        role = ", ".join(sorted({a.role.replace("_", " ").title() for a in p.assignments}))
    ctx = {"request": request, "me": p.user, "caps": caps, "projects": projects, "selected_project": sel,
           "role_label": role, "unread": notify.unread_count(db, p),
           "counts": tickets.counts(db, p), "csrf": csrf_token_for(p.session.token_hash),
           "flash": request.query_params.get("msg"), "flash_error": request.query_params.get("err"),
           "now": clock.now()}
    ctx.update(extra)
    return ctx


def page(name: str, ctx: dict, status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(ctx["request"], name, ctx, status_code=status)


def back(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    q = {}
    if msg:
        q["msg"] = msg
    if err:
        q["err"] = err
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url + (sep + urlencode(q) if q else ""), status_code=303)


def err_text(e: AppError) -> str:
    if e.fields:
        return e.message + " " + "; ".join(f"{k}: {v}" for k, v in e.fields.items())
    return e.message


def render_error(request: Request, exc: AppError) -> HTMLResponse:
    """S12: one message for not found and no access."""
    if exc.code == "not_authenticated":
        return RedirectResponse("/login?expired=1", status_code=303)
    kind = "missing" if exc.status in (403, 404) else ("conflict" if exc.status == 409 else "error")
    msg = {"missing": "This page does not exist or you do not have access to it.",
           "conflict": exc.message, "error": exc.message}[kind]
    if exc.status == 422:
        kind = "error"
    return templates.TemplateResponse(request, "error.html", {"request": request, "message": msg, "kind": kind},
                                      status_code=exc.status if exc.status >= 400 else 400)


# ================================================================ S01 sign in, S02 account
@router.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request,
                                                              "expired": request.query_params.get("expired"),
                                                              "next": request.query_params.get("next", "/")})


@router.post("/login")
async def login_submit(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    email, password = form.get("email", ""), form.get("password", "")
    nxt = form.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    ip = client_ip(request)
    try:
        login_limiter.check(ip)
        user = auth_service.authenticate(db, email, password)
    except AppError as e:
        if e.code != "rate_limited":
            login_limiter.fail(ip)
        msg = "Try again in a few minutes." if e.code == "rate_limited" else e.message
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": msg, "email": email,
                                                                  "next": nxt}, status_code=e.status)
    token, row = auth_service.start_session(db, user, "SESSION")
    resp = RedirectResponse("/account?force=1" if user.must_change_password else nxt, status_code=303)
    from ..api.routes_auth import set_session_cookie
    set_session_cookie(resp, token, db)
    return resp


@router.post("/logout")
async def logout(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    await check_csrf(request, p)
    auth_service.logout(db, p.session)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/account")
def account(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    from ..api import serialize as S
    org = db.get(Organization, p.user.organization_id) if p.user.organization_id else None
    proj = db.get(Project, p.user.project_id) if p.user.project_id else None
    return page("account.html", shell(request, p, db, org=org, proj=proj, force=request.query_params.get("force"),
                                      assignments=[S.assignment(a, db) for a in p.assignments],
                                      min_len=settings.get(db, "auth.password_min_length")))


@router.post("/account")
async def account_submit(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    form = await check_csrf(request, p)
    if form.get("new_password") != form.get("repeat_password"):
        return back("/account" + ("?force=1" if p.user.must_change_password else ""), err="The two new passwords differ.")
    try:
        auth_service.change_password(db, p, form.get("current_password", ""), form.get("new_password", ""))
    except AppError as e:
        return back("/account" + ("?force=1" if p.user.must_change_password else ""), err=err_text(e))
    return back("/account", msg="Password changed. Your other sessions were signed out.")


# ================================================================ S03 home
@router.get("/")
def home(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    tiles = []
    c = tickets.counts(db, p)
    mine_open = tickets.list_tickets(db, p, {"view": "mine", "status": ["open"], "page_size": 1})[1]["total"]
    tiles.append(("My open tickets", mine_open, "/tickets?status=open"))
    tiles.append(("Waiting on me", c["waiting_on_me"], "/tickets?status=waiting"))
    if p.teams() and not p.is_product_user:
        tiles.append(("My work", c["my_work"], "/work"))
        tiles.append(("Team queue", c["queue"], "/queue"))
        work = tickets.list_tickets(db, p, {"view": "work", "sort": "nearest_due", "page_size": 1})[0]
        nearest = work[0]["nearest_due"] if work and work[0].get("nearest_due") else None
        tiles.append(("Nearest due", fmt_time(nearest["due_at"], nearest["time_zone"]) if nearest and
                      nearest.get("due_at") else "none", "/work"))
    if p.teams("TEAM_MANAGER"):
        team_q = {"view": "team", "page_size": 1}
        tiles.append(("Needs assignment", tickets.list_tickets(db, p, {**team_q, "status": ["NEW"],
                                                                     "resolver": "none"})[1]["total"], "/team"))
        tiles.append(("Overdue", tickets.list_tickets(db, p, {**team_q, "overdue": True})[1]["total"],
                      "/tickets/all?overdue=true"))
    if p.any_admin():
        dash = operations.dashboard(db, p)
        tiles.append(("Overdue in my scope", dash["overdue"]["count"], "/admin"))
        tiles.append(("At risk", dash["at_risk"]["count"], "/admin"))
        tiles.append(("Oldest unassigned", dash["oldest_unassigned"]["count"], "/admin"))
    recent = tickets.list_tickets(db, p, {"view": "mine", "page_size": 5})[0]
    return page("home.html", shell(request, p, db, tiles=tiles, recent=recent))


# ================================================================ S04 new ticket
@router.get("/tickets/new")
def new_ticket(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    projects = reference.projects_for(db, p)
    sel = current_project(request, p, db)
    if sel not in [x["id"] for x in projects]:
        sel = projects[0]["id"] if projects else None
    types = reference.ticket_types_for(db, p, sel) if sel else []
    choose_priority = not p.is_product_user or (sel and settings.get(db, "product_user.can_set_priority", sel))
    return page("new_ticket.html", shell(request, p, db, raise_projects=projects, project_id=sel, types=types,
                                         priorities=cfg.list_priorities(db, active_only=True),
                                         choose_priority=choose_priority, key=str(uuid.uuid4()),
                                         values=dict(request.query_params)))


@router.post("/tickets/new")
async def new_ticket_submit(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    form = await check_csrf(request, p)
    data = {k: form.get(k) or None for k in ("project_id", "type_id", "priority_id", "title", "description")}
    try:
        t, _ = tickets.create_ticket(db, p, data, "PORTAL", form.get("idempotency_key"))
    except AppError as e:
        q = {k: v for k, v in data.items() if v}
        q["err"] = err_text(e)
        return RedirectResponse("/tickets/new?" + urlencode(q), status_code=303)
    return back(f"/tickets/{t.id}", msg="Ticket raised.")


# ================================================================ lists S05 S07 S08 S09 S10
LIST_PARAMS = ("status", "priority", "type", "team", "resolver", "overdue", "q", "created_from", "created_to",
               "page", "sort")


def _list(request: Request, p: Principal, db: Session, view: str | None, **forced):
    qp = request.query_params
    params = {k: qp.get(k) for k in LIST_PARAMS if qp.get(k)}
    params["status"] = qp.getlist("status")
    params["view"] = view
    sel = current_project(request, p, db)
    if sel:
        params["project"] = sel
    params.update(forced)
    try:
        items, meta = tickets.list_tickets(db, p, params)
    except AppError as e:
        if e.code == "forbidden":
            raise
        items, meta = [], {"page": 1, "page_size": 25, "total": 0}
        params["error"] = err_text(e)
    return items, meta, params


@router.get("/tickets")
def my_tickets(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    items, meta, params = _list(request, p, db, "mine")
    if not request.query_params.get("sort"):
        items.sort(key=lambda t: t["status"]["code"] != "AWAITING_REQUESTER")  # waiting on me first
    return page("list.html", shell(request, p, db, title="My tickets", items=items, meta=meta, params=params,
                                   view="mine", priorities=cfg.list_priorities(db, True)))


@router.get("/tickets/all")
def all_tickets(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    if not ("tickets.all" in capabilities(p)):
        raise AppError("forbidden")
    items, meta, params = _list(request, p, db, "all")
    return page("list.html", shell(request, p, db, title="All tickets", items=items, meta=meta, params=params,
                                   view="all", priorities=cfg.list_priorities(db, True), full=True))


@router.get("/work")
def my_work(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    if not p.teams():
        raise AppError("forbidden")
    items, meta, params = _list(request, p, db, "work", sort=request.query_params.get("sort") or "nearest_due",
                                page_size=100)
    groups = [("Assigned", "ASSIGNED"), ("In progress", "IN_PROGRESS"), ("Paused", "ON_HOLD"),
              ("Waiting on requester", "AWAITING_REQUESTER")]
    grouped = [(label, [t for t in items if t["status"]["code"] == code]) for label, code in groups]
    return page("work.html", shell(request, p, db, grouped=grouped, total=meta["total"]))


@router.get("/queue")
def queue(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    if not p.teams():
        raise AppError("forbidden")
    team_ids = sorted(p.teams())
    teams = [db.get(Team, t) for t in team_ids]
    team = request.query_params.get("team")
    forced = {"page_size": 100, "sort": request.query_params.get("sort") or "created"}
    if team:
        forced["team"] = team
    items, meta, params = _list(request, p, db, "queue", **forced)
    members = {}
    for t in teams:
        if p.is_manager(t.id):
            members[t.id] = reference.team_members(db, p, t.id)
    return page("queue.html", shell(request, p, db, items=items, teams=teams, team=team, members=members))


@router.get("/team")
def team_board(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    mgr = sorted(p.teams("TEAM_MANAGER"))
    if not mgr:
        raise AppError("forbidden")
    team_id = int(request.query_params.get("team") or mgr[0])
    if team_id not in mgr:
        raise AppError("not_found")
    items, _, _ = _list(request, p, db, "team", team=team_id, status=["open"], page_size=100, sort="nearest_due")
    lists = {
        "Needs assignment": [t for t in items if t["status"]["code"] == "NEW" and not t["resolver"]],
        "In progress": [t for t in items if t["status"]["code"] in ("ASSIGNED", "IN_PROGRESS")],
        "Paused": [t for t in items if t["status"]["code"] in ("ON_HOLD", "AWAITING_REQUESTER")],
        "At risk and overdue": [t for t in items if t.get("nearest_due") and t["nearest_due"].get("due_at") and (
            t["nearest_due"]["state"] == "BREACHED" or (t["nearest_due"]["due_at"] - clock.now()).total_seconds() < 3600)],
    }
    members = reference.team_members(db, p, team_id)
    flagged = [t for t in items if t.get("resolver_active") is False]
    return page("team.html", shell(request, p, db, lists=lists, members=members, teams=[db.get(Team, t) for t in mgr],
                                   team_id=team_id, flagged=flagged, priorities=cfg.list_priorities(db, True)))


# ================================================================ S06 ticket detail and dialogs
DIALOGS = {  # A9.4: action -> fields (name, label, kind, required)
    "assign": [("resolver_id", "Resolver", "resolver", True)],
    "reassign": [("resolver_id", "Resolver", "resolver", True), ("reason", "Reason", "text", True)],
    "hold": [("reason", "Reason", "text", True)],
    "request-info": [("question", "Question", "text", True)],
    "respond": [("answer", "Answer", "text", True)],
    "resolve": [("note", "Resolution note", "text", True), ("actual_effort_minutes", "Actual effort (minutes)",
                                                           "number", False)],
    "reject": [("reason_code", "Reason code", "reject_code", True), ("reason", "Reason", "text", True)],
    "withdraw": [("reason", "Reason", "text", True)],
    "reopen": [("reason", "Reason", "text", True)],
    "priority": [("priority_id", "Priority", "priority", True)],
    "sla-pause": [("reason", "Reason", "text", True)],
    "sla-extend": [("step", "Step", "step", True), ("minutes", "Minutes", "number", True),
                   ("reason", "Reason", "text", True)],
    "estimate": [("minutes", "Estimated effort (minutes)", "number", True)],
    "release": [],
}
ONE_CLICK = ("claim", "start", "resume", "sla-resume")
LABELS = {"claim": "Claim", "assign": "Assign", "start": "Start", "hold": "Hold", "request-info": "Request info",
          "resume": "Resume", "respond": "Respond", "resolve": "Resolve", "reject": "Reject", "withdraw": "Withdraw",
          "release": "Release", "reassign": "Reassign", "reopen": "Reopen", "priority": "Change priority",
          "estimate": "Set estimate", "sla-pause": "Pause SLA", "sla-resume": "Resume SLA", "sla-extend": "Extend SLA"}


def ticket_page(request, p, db, ref, error=None, open_dialog=None, typed=None, status=200):
    t = tickets.load(db, p, ref)
    ctx = tickets.Ctx(db)
    v = tickets.view(ctx, p, t)
    evs, _, _ = tickets.events(db, p, t.id, 1, 100)
    staff = "nearest_due" in v
    timers = sla.read_timers(db, p, t) if staff else None
    members = []
    if any(a in v["allowed_actions"] for a in ("assign", "reassign")):
        members = [m for m in routing_members(db, t.team_id)]
    dialog_actions = [a for a in v["allowed_actions"] if a not in ONE_CLICK]
    if open_dialog in DIALOGS and open_dialog not in dialog_actions:
        dialog_actions.append(open_dialog)  # 409: keep the dialog and the typed text after the ticket moved on
    return page("ticket.html", shell(request, p, db, t=v, events=evs, dialog_actions=dialog_actions, staff=staff, timers=timers, dialogs=DIALOGS,
                                     one_click=ONE_CLICK, labels=LABELS, members=members,
                                     priorities=cfg.list_priorities(db, True), reject_codes=tickets.REJECT_CODES,
                                     error=error, open_dialog=open_dialog, typed=typed or {}), status=status)


def routing_members(db, team_id):
    from ..services import routing
    cache, now = {}, clock.now()
    for ra, u in routing.team_people(db, team_id):
        yield {"id": u.id, "name": u.name, "on_shift": routing.on_shift(db, ra, now, cache)}


@router.get("/tickets/{ref}")
def ticket_detail(ref: str, request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    return ticket_page(request, p, db, ref)


@router.post("/tickets/{ref}/act/{action}")
async def ticket_action(ref: str, action: str, request: Request, p: Principal = Depends(web_principal),
                        db: Session = Depends(get_db)):
    form = await check_csrf(request, p)
    nxt = form.pop("next", None)
    body = {k: v for k, v in form.items() if v != ""}
    try:
        t = tickets.perform(db, p, ref, action, body)
    except AppError as e:
        if e.code in ("not_found",):
            raise
        db.rollback()
        msg = ("This ticket was updated. Reload to see the latest." if e.status == 409 and e.code == "invalid_state"
               else err_text(e))
        if nxt and nxt.startswith("/") and not nxt.startswith("//"):
            return back(nxt, err=msg)
        # keep the typed text (A9.4)
        return ticket_page(request, p, db, ref, error=msg, open_dialog=action, typed=body, status=e.status)
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return back(nxt, msg=f"{LABELS.get(action, action)} done.")
    return back(f"/tickets/{t.id}", msg=f"{LABELS.get(action, action)} done.")


# ================================================================ S11 notifications
@router.get("/notifications")
def notifications_page(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    rows, meta = notify.inbox(db, p, None, int(request.query_params.get("page") or 1), 25)
    refs = {}
    from ..models import Ticket
    for n in rows:
        if n.ticket_id and n.ticket_id not in refs:
            t = db.get(Ticket, n.ticket_id)
            refs[n.ticket_id] = {"id": t.id, "number": notify.ticket_number(db, t), "title": t.title}
    return page("notifications.html", shell(request, p, db, rows=rows, refs=refs, meta=meta))


@router.post("/notifications/{nid}/open")
async def open_notification(nid: int, request: Request, p: Principal = Depends(web_principal),
                            db: Session = Depends(get_db)):
    await check_csrf(request, p)
    from ..models import Notification
    notify.mark_read(db, p, nid)
    n = db.get(Notification, nid)
    return RedirectResponse(f"/tickets/{n.ticket_id}" if n.ticket_id else "/notifications", status_code=303)


@router.post("/notifications/read-all")
async def read_all(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    await check_csrf(request, p)
    notify.mark_all_read(db, p)
    return back("/notifications", msg="All alerts marked read.")


@router.get("/error")
def error_page(request: Request):
    return templates.TemplateResponse(request, "error.html", {"request": request, "kind": request.query_params.get(
        "kind", "missing"), "message": "This page does not exist or you do not have access to it."})


@router.get("/project-select")
def project_select(request: Request, p: Principal = Depends(web_principal)):
    """Shell project selector: remembered in a cookie; lists and menus follow it."""
    proj = request.query_params.get("project", "all")
    dest = request.query_params.get("back", "/")
    if not dest.startswith("/") or dest.startswith("//"):
        dest = "/"
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie("proj", proj if proj.isdigit() else "all", httponly=True, samesite="lax", path="/")
    return resp


@router.get("/healthz")
def healthz():
    return {"status": "ok"}


def mount(app: FastAPI):
    from . import admin  # registers admin screens on the same router
    app.include_router(router)
    app.include_router(admin.router)
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

    @app.exception_handler(Redirect)
    async def _redirect(request: Request, exc: Redirect):
        return RedirectResponse(exc.url, status_code=303)
