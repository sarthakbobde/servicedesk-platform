"""Admin portal screens S13-S30. Forms post to these routes, which call the same services as the API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock, worktime
from ..api import serialize as S
from ..api.deps import TxRoute, get_db
from ..errors import AppError
from ..models import Calendar, NotificationChannel, Organization, Priority, Project, Team, TicketType, User
from ..permissions import Principal
from ..services import channels, escalation, notify, operations, sla, structure as st
from ..services import configuration as cfg
from .portal import back, check_csrf, err_text, page, shell, web_principal

router = APIRouter(prefix="/admin", route_class=TxRoute, include_in_schema=False)


def need(cond: bool):
    if not cond:
        raise AppError("forbidden")


def tri(v):
    """Checkbox/select to bool; absent -> None."""
    if v is None or v == "":
        return None
    return str(v).lower() in ("1", "true", "on", "yes")


async def run(request, p, fn, ok_url, ok_msg, err_url=None):
    """Execute a service call from a form; errors return to the screen with the message (in a fresh transaction)."""
    db = request.state.db
    try:
        result = fn()
        db.flush()
    except AppError as e:
        if e.code == "not_found":
            raise
        db.rollback()
        return back(err_url or ok_url, err=err_text(e))
    url = ok_url(result) if callable(ok_url) else ok_url
    return back(url, msg=ok_msg)


# ================================================================ S13 dashboard
@router.get("")
def dashboard(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    need(p.any_admin())
    qp = request.query_params
    dash = operations.dashboard(db, p, qp.get("project") or None, qp.get("team") or None)
    status = operations.system_status(db, p) if p.is_platform_admin else None
    return page("admin/dashboard.html", shell(request, p, db, dash=dash, status=status))


# ================================================================ S14 organisations
@router.get("/organizations")
def organizations(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    rows, meta = st.list_organizations(db, p, request.query_params.get("type") or None, None, 1, 100)
    counts = {o.id: len(db.scalars(select(Project.id).where(Project.organization_id == o.id)).all()) for o in rows}
    return page("admin/organizations.html", shell(request, p, db, rows=rows, counts=counts))


@router.post("/organizations")
async def organizations_create(request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    return await run(request, p, lambda: st.create_organization(request.state.db, p, form),
                     "/admin/organizations", "Organisation created.")


@router.post("/organizations/{oid}")
async def organizations_update(oid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {k: form[k] for k in ("name", "type") if form.get(k)}
    if "is_active" in form:
        data["is_active"] = tri(form["is_active"])
    return await run(request, p, lambda: st.update_organization(request.state.db, p, oid, data),
                     "/admin/organizations", "Organisation saved.")


# ================================================================ S15 projects, S16 project detail
@router.get("/projects")
def projects(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    rows, meta = st.list_projects(db, p, request.query_params.get("organization_id") or None, None, 1, 100)
    orgs = st.list_organizations(db, p, None, True, 1, 100)[0] if (p.is_platform_admin or p.orgs_admin()) else []
    return page("admin/projects.html", shell(request, p, db, rows=[(x, st.project_counts(db, x)) for x in rows],
                                             orgs=orgs, org_names={o.id: o.name for o in db.scalars(select(Organization))}))


@router.post("/projects")
async def projects_create(request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    return await run(request, p, lambda: st.create_project(request.state.db, p, form),
                     lambda proj: f"/admin/projects/{proj.id}", "Project created.", "/admin/projects")


@router.get("/projects/{pid}")
def project_detail(pid: int, request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    return project_screen(pid, request, p, db)


def project_screen(pid, request, p, db, tab=None, new_key=None, new_secret=None):
    proj = st.get_project(db, p, pid)
    tab = tab or request.query_params.get("tab", "settings")
    ctx = {"proj": proj, "tab": tab, "counts": st.project_counts(db, proj)}
    ctx["settings_rows"] = st.get_project_settings(db, p, pid)
    teams = st.list_teams(db, p, pid, None, 1, 100)[0]
    ctx["teams"] = teams
    ra, _ = st.list_assignments(db, p, None, pid, None, None, True, 1, 100)
    ctx["assignments"] = [S.assignment(a, db) for a in ra]
    ctx["names"] = S.user_names(db, [a.user_id for a in ra])
    ctx["product_users"] = list(db.scalars(select(User).where(User.project_id == pid).order_by(User.name)))
    ctx["credentials"] = st.list_credentials(db, p, pid)
    ctx["calendars"] = list(db.scalars(select(Calendar).where(Calendar.is_active.is_(True)).order_by(Calendar.name)))
    ctx["new_secret"], ctx["new_key"] = new_secret, new_key  # shown once, never in a URL
    return page("admin/project.html", shell(request, p, db, **ctx))


@router.post("/projects/{pid}")
async def project_update(pid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {k: form[k] for k in ("name", "key") if form.get(k)}
    if "is_active" in form:
        data["is_active"] = tri(form["is_active"])
    return await run(request, p, lambda: st.update_project(request.state.db, p, pid, data),
                     f"/admin/projects/{pid}", "Project saved.")


@router.post("/projects/{pid}/settings")
async def project_settings(pid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    changes = {}
    for key, value in form.items():
        if not key.startswith("s:"):
            continue
        k = key[2:]
        if form.get(f"inherit:{k}"):
            changes[k] = None
        else:
            changes[k] = value
    for k in ("product_user.api_access", "product_user.portal_access", "product_user.can_set_priority"):
        if f"s:{k}" not in form and not form.get(f"inherit:{k}") and form.get(f"present:{k}"):
            changes[k] = False  # unchecked checkbox
    return await run(request, p, lambda: st.put_project_settings(request.state.db, p, pid, changes),
                     f"/admin/projects/{pid}?tab=settings", "Settings saved.")


@router.post("/projects/{pid}/teams")
async def project_team_create(pid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    return await run(request, p, lambda: st.create_team(request.state.db, p, pid, form),
                     f"/admin/projects/{pid}?tab=teams", "Team created.")


@router.post("/teams/{tid}")
async def team_update(tid: int, request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    form = await check_csrf(request, p)
    team = st.get_team(db, p, tid)
    data = {}
    if form.get("name"):
        data["name"] = form["name"]
    if "is_active" in form:
        data["is_active"] = tri(form["is_active"])
    return await run(request, p, lambda: st.update_team(request.state.db, p, tid, data),
                     f"/admin/projects/{team.project_id}?tab=teams", "Team saved.")


@router.post("/projects/{pid}/credentials")
async def credential_create(pid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    db = request.state.db
    try:
        cred, secret = st.create_credential(db, p, pid, form)
    except AppError as e:
        if e.code == "not_found":
            raise
        db.rollback()
        return back(f"/admin/projects/{pid}?tab=credentials", err=err_text(e))
    # shown once: rendered directly, never stored or put in a URL
    return project_screen(pid, request, p, db, "credentials", cred.key_prefix, secret)


@router.post("/credentials/{cid}")
async def credential_update(cid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {}
    if form.get("name"):
        data["name"] = form["name"]
    if "is_active" in form:
        data["is_active"] = tri(form["is_active"])
    return await run(request, p, lambda: st.update_credential(request.state.db, p, cid, data),
                     lambda c: f"/admin/projects/{c.project_id}?tab=credentials", "Credential saved.",
                     "/admin/projects")


# ================================================================ S17 users, S18 user detail, S19 bulk add
@router.get("/users")
def users(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    qp = request.query_params
    rows, meta = st.list_users(db, p, qp.get("q"), qp.get("user_type") or None, qp.get("organization_id") or None,
                               qp.get("project_id") or None, tri(qp.get("active")), int(qp.get("page") or 1), 25)
    orgs = list(db.scalars(select(Organization).where(Organization.is_active.is_(True)).order_by(Organization.name)))
    ids = p.admin_project_ids()
    projs = list(db.scalars(select(Project).where(Project.is_active.is_(True)).order_by(Project.key)))
    if ids is not None:
        projs = [x for x in projs if x.id in ids]
        allowed_orgs = p.orgs_admin() | {x.organization_id for x in projs}
        orgs = [o for o in orgs if o.id in allowed_orgs]
    return page("admin/users.html", shell(request, p, db, rows=rows, meta=meta, orgs=orgs, projs=projs, q=dict(qp),
                                          org_names={o.id: o.name for o in db.scalars(select(Organization))},
                                          proj_keys={x.id: x.key for x in db.scalars(select(Project))}))


@router.post("/users")
async def users_create(request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    db = request.state.db
    data = {k: v for k, v in form.items() if v != ""}
    if "is_platform_admin" in data:
        data["is_platform_admin"] = tri(data["is_platform_admin"])
    try:
        user, temp = st.create_user(db, p, data)
    except AppError as e:
        db.rollback()
        return back("/admin/users", err=err_text(e))
    return user_page(request, p, db, user.id, temp=temp, msg="User created.")


def user_page(request, p, db, uid, temp=None, msg=None):
    user = st.get_user(db, p, uid)
    assigns = [S.assignment(a, db) for a in st.user_assignments(db, user.id)]
    teams = []
    ids = p.admin_project_ids()
    for t in db.scalars(select(Team).where(Team.is_active.is_(True)).order_by(Team.name)):
        if ids is None or t.project_id in ids:
            teams.append((t, db.get(Project, t.project_id)))
    projs = [x for x in db.scalars(select(Project).where(Project.is_active.is_(True))) if ids is None or x.id in ids]
    orgs = [o for o in db.scalars(select(Organization).where(Organization.is_active.is_(True)))
            if p.admin_of_org(o.id)]
    cals = list(db.scalars(select(Calendar).where(Calendar.is_active.is_(True)).order_by(Calendar.name)))
    return page("admin/user.html", shell(request, p, db, user=user, assigns=assigns, temp=temp, teams=teams,
                                         projs=projs, orgs=orgs, calendars=cals, flash=msg or request.query_params.get("msg"),
                                         held=st.open_tickets_held(db, user.id)))


@router.get("/users/{uid}")
def user_detail(uid: int, request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    return user_page(request, p, db, uid)


@router.post("/users/{uid}")
async def user_update(uid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {k: form[k] for k in ("name", "email") if k in form}
    for k in ("is_active", "is_platform_admin"):
        if k in form:
            data[k] = tri(form[k])
    return await run(request, p, lambda: st.update_user(request.state.db, p, uid, data), f"/admin/users/{uid}",
                     "User saved.")


@router.post("/users/{uid}/reset-password")
async def user_reset(uid: int, request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    await check_csrf(request, p)
    try:
        _, temp = st.reset_password(db, p, uid)
    except AppError as e:
        if e.code == "not_found":
            raise
        db.rollback()
        return back(f"/admin/users/{uid}", err=err_text(e))
    return user_page(request, p, db, uid, temp=temp, msg="New temporary password issued.")


@router.post("/users/{uid}/assignments")
async def user_assign(uid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {"user_id": uid, "role": form.get("role")}
    target = form.get("target", "")
    if ":" in target:
        kind, tid = target.split(":", 1)
        data[{"team": "team_id", "project": "project_id", "org": "organization_id"}[kind]] = int(tid)
    if form.get("calendar_id"):
        data["calendar_id"] = int(form["calendar_id"])
    return await run(request, p, lambda: st.create_assignment(request.state.db, p, data), f"/admin/users/{uid}",
                     "Role given.")


@router.post("/role-assignments/{rid}")
async def assignment_update(rid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {}
    if "role" in form and form["role"]:
        data["role"] = form["role"]
    if "calendar_id" in form:
        data["calendar_id"] = int(form["calendar_id"]) if form["calendar_id"] else None
    if "is_active" in form:
        data["is_active"] = tri(form["is_active"])
    nxt = form.get("next") or "/admin/users"
    return await run(request, p, lambda: st.update_assignment(request.state.db, p, rid, data), nxt,
                     "Role assignment saved.")


@router.get("/role-assignments/bulk")
def bulk_page(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    need(p.any_admin())
    ids = p.admin_project_ids()
    teams = [(t, db.get(Project, t.project_id)) for t in db.scalars(select(Team).where(Team.is_active.is_(True)))
             if ids is None or t.project_id in ids]
    users_ = st.list_users(db, p, None, "MEMBER", None, None, True, 1, 100)[0]
    return page("admin/bulk.html", shell(request, p, db, teams=teams, users=users_, result=None))


@router.post("/role-assignments/bulk")
async def bulk_submit(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    multi = await request.form()
    await check_csrf(request, p)
    data = {"team_id": int(multi.get("team_id") or 0) or None, "role": multi.get("role"),
            "user_ids": [int(x) for x in multi.getlist("user_ids")]}
    try:
        result = st.bulk_assign(db, p, data)
    except AppError as e:
        db.rollback()
        return back("/admin/role-assignments/bulk", err=err_text(e))
    names = S.user_names(db, data["user_ids"])
    resp = bulk_page(request, p, db)
    resp.context.update(result=result, names=names)
    return page("admin/bulk.html", resp.context)


# ================================================================ S20 ticket types
@router.get("/ticket-types")
def ticket_types(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    rows, _ = cfg.list_ticket_types(db, p, request.query_params.get("project_id") or None, None, 1, 100)
    ids = p.admin_project_ids()
    teams = [(t, db.get(Project, t.project_id)) for t in db.scalars(select(Team).where(Team.is_active.is_(True)))
             if ids is None or t.project_id in ids]
    return page("admin/ticket_types.html", shell(request, p, db, rows=rows, teams=teams,
                                                 team_names={t.id: t.name for t in db.scalars(select(Team))},
                                                 proj_keys={x.id: x.key for x in db.scalars(select(Project))}))


@router.post("/ticket-types")
async def ticket_type_create(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    form = await check_csrf(request, p)
    team = db.get(Team, int(form.get("team_id") or 0))
    data = {**form, "project_id": team.project_id if team else None, "team_id": team.id if team else None}
    return await run(request, p, lambda: cfg.create_ticket_type(db, p, data), "/admin/ticket-types",
                     "Ticket type created.")


@router.post("/ticket-types/{tid}")
async def ticket_type_update(tid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {k: form[k] for k in ("name", "assignment_mode", "description") if k in form}
    if form.get("team_id"):
        data["team_id"] = int(form["team_id"])
    if "is_active" in form:
        data["is_active"] = tri(form["is_active"])
    return await run(request, p, lambda: cfg.update_ticket_type(request.state.db, p, tid, data),
                     "/admin/ticket-types", "Ticket type saved. The change applies to new tickets only.")


# ================================================================ S21 priorities, S22 statuses
@router.get("/priorities")
def priorities(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    need(p.is_platform_admin)
    return page("admin/priorities.html", shell(request, p, db, rows=cfg.list_priorities(db)))


@router.post("/priorities")
async def priority_create(request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {"name": form.get("name"), "rank": form.get("rank"), "is_default": tri(form.get("is_default")) or False}
    return await run(request, p, lambda: cfg.create_priority(request.state.db, p, data), "/admin/priorities",
                     "Priority created.")


@router.post("/priorities/{pid}")
async def priority_update(pid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {k: form[k] for k in ("name", "rank") if form.get(k)}
    for k in ("is_default", "is_active"):
        if k in form:
            data[k] = tri(form[k])
    return await run(request, p, lambda: cfg.update_priority(request.state.db, p, pid, data), "/admin/priorities",
                     "Priority saved.")


@router.get("/statuses")
def statuses(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    need(p.is_platform_admin)
    return page("admin/statuses.html", shell(request, p, db, rows=cfg.list_statuses(db)))


@router.post("/statuses/{sid}")
async def status_update(sid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    return await run(request, p, lambda: cfg.update_status(request.state.db, p, sid, {"name": form.get("name")}),
                     "/admin/statuses", "Status renamed.")


# ================================================================ S23 calendars, S24 calendar editor
@router.get("/calendars")
def calendars(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    need(p.is_platform_admin)
    rows = cfg.list_calendars(db, p, None, 1, 100)[0]
    return page("admin/calendars.html", shell(request, p, db, rows=[(c, cfg.calendar_usage(db, c)) for c in rows]))


@router.post("/calendars")
async def calendar_create(request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {"name": form.get("name"), "time_zone": form.get("time_zone"), "is_default": tri(form.get("is_default"))}
    return await run(request, p, lambda: cfg.create_calendar(request.state.db, p, data),
                     lambda c: f"/admin/calendars/{c.id}", "Calendar created.", "/admin/calendars")


@router.get("/calendars/{cid}")
def calendar_editor(cid: int, request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    from zoneinfo import ZoneInfo
    cal = cfg.get_calendar(db, p, cid)
    hours = cfg.get_hours(db, p, cid)
    grid = {d: [h for h in hours if h.weekday == d] for d in range(1, 8)}
    spec = cfg.calendar_spec(db, cal.id)
    today = clock.now().astimezone(ZoneInfo(cal.time_zone)).date()
    return page("admin/calendar.html", shell(request, p, db, cal=cal, grid=grid, usage=cfg.calendar_usage(db, cal),
                                             holidays=cfg.list_holidays(db, p, cid, True),
                                             preview=worktime.preview(spec, today, 14) if cal.is_active else []))


@router.post("/calendars/{cid}")
async def calendar_update(cid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {k: form[k] for k in ("name", "time_zone") if form.get(k)}
    for k in ("is_default", "is_active"):
        if k in form:
            data[k] = tri(form[k])
    return await run(request, p, lambda: cfg.update_calendar(request.state.db, p, cid, data),
                     f"/admin/calendars/{cid}", "Calendar saved.")


@router.post("/calendars/{cid}/hours")
async def calendar_hours(cid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    windows = []
    for d in range(1, 8):
        for i in range(3):
            s, e = form.get(f"d{d}s{i}", "").strip(), form.get(f"d{d}e{i}", "").strip()
            if s or e:
                windows.append({"weekday": d, "start": s, "end": e})
    return await run(request, p, lambda: cfg.put_hours(request.state.db, p, cid, {"windows": windows}),
                     f"/admin/calendars/{cid}", "Weekly pattern saved.")


@router.post("/calendars/{cid}/holidays")
async def calendar_holiday(cid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {k: form.get(k) or None for k in ("date", "name", "start", "end")}
    return await run(request, p, lambda: cfg.add_holiday(request.state.db, p, cid, data), f"/admin/calendars/{cid}",
                     "Holiday added.")


@router.post("/calendar-holidays/{hid}")
async def holiday_update(hid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {"is_active": tri(form.get("is_active"))}
    return await run(request, p, lambda: cfg.update_holiday(request.state.db, p, hid, data),
                     lambda h: f"/admin/calendars/{h.calendar_id}", "Holiday saved.", "/admin/calendars")


# ================================================================ S25 SLA policies, S26 SLA policy editor
@router.get("/sla-policies")
def sla_policies(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    from ..api.routes_sla import policy_view
    rows, _ = sla.list_policies(db, p, None, None, None, None, None, 1, 100)
    missing = sla.missing_combinations(db, p) if request.query_params.get("missing") else None
    ids = p.admin_project_ids()
    types = [(tt, db.get(Project, tt.project_id)) for tt in db.scalars(select(TicketType).where(
        TicketType.is_active.is_(True))) if ids is None or tt.project_id in ids]
    return page("admin/sla_policies.html", shell(request, p, db, rows=[policy_view(db, x) for x in rows],
                                                 missing=missing, types=types,
                                                 priorities=cfg.list_priorities(db, True),
                                                 calendars=cfg.list_calendars(db, p, True, 1, 100)[0]
                                                 if p.is_platform_admin else []))


@router.post("/sla-policies")
async def sla_policy_create(request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {"name": form.get("name"), "priority_id": int(form.get("priority_id") or 0) or None,
            "ticket_type_id": int(form.get("ticket_type_id") or 0) or None,
            "calendar_id": int(form.get("calendar_id") or 0) or None}
    return await run(request, p, lambda: sla.create_policy(request.state.db, p, data),
                     lambda pol: f"/admin/sla-policies/{pol.id}", "Policy created. Add its steps.", "/admin/sla-policies")


@router.get("/sla-policies/{pid}")
def sla_policy_editor(pid: int, request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    from ..api.routes_sla import policy_view
    pol = sla.get_policy(db, p, pid)
    steps = sla.policy_metrics(db, pol.id)
    rule_map = {m.id: [escalation.rule_view(r) for r in escalation.list_rules(db, p, m.id)] for m in steps}
    users_ = []
    teams = []
    if pol.ticket_type_id:
        tt = db.get(TicketType, pol.ticket_type_id)
        teams = list(db.scalars(select(Team).where(Team.project_id == tt.project_id, Team.is_active.is_(True))))
        users_ = st.list_users(db, p, None, "MEMBER", None, tt.project_id, True, 1, 100)[0]
    return page("admin/sla_policy.html", shell(request, p, db, pol=policy_view(db, pol), steps=steps,
                                               rule_map=rule_map, users=users_, teams=teams,
                                               stop_choices=sla.STOP_CHOICES, triggers=escalation.TRIGGERS,
                                               actions=escalation.ACTIONS, targets=escalation.TARGETS))


@router.post("/sla-policies/{pid}")
async def sla_policy_update(pid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {}
    if form.get("name"):
        data["name"] = form["name"]
    if "is_active" in form:
        data["is_active"] = tri(form["is_active"])
    return await run(request, p, lambda: sla.update_policy(request.state.db, p, pid, data),
                     f"/admin/sla-policies/{pid}", "Policy saved.")


@router.post("/sla-policies/{pid}/steps")
async def sla_policy_steps(pid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    steps = []
    for i in range(1, 11):
        name = form.get(f"name{i}", "").strip()
        if not name and not form.get(f"minutes{i}"):
            continue
        steps.append({"position": i, "name": name, "minutes": form.get(f"minutes{i}"),
                      "stop_status_code": form.get(f"stop{i}")})
    return await run(request, p, lambda: sla.put_metrics(request.state.db, p, pid, {"steps": steps}),
                     f"/admin/sla-policies/{pid}", "Steps saved. They apply to new tickets.")


@router.post("/sla-metrics/{mid}/rules")
async def sla_rules(mid: int, request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    from ..models import SlaMetric
    form = await check_csrf(request, p)
    m = db.get(SlaMetric, mid)
    rules = []
    for i in range(1, 6):
        if not form.get(f"trigger{i}"):
            continue
        rules.append({"level": form.get(f"level{i}") or i, "name": form.get(f"rname{i}"),
                      "trigger": form.get(f"trigger{i}"), "threshold": form.get(f"threshold{i}") or 0,
                      "action": form.get(f"action{i}"), "target_type": form.get(f"target{i}") or None,
                      "target_id": form.get(f"target_id{i}") or None})
    url = f"/admin/sla-policies/{m.policy_id}" if m else "/admin/sla-policies"
    return await run(request, p, lambda: escalation.put_rules(db, p, mid, {"rules": rules}), url,
                     "Escalation rules saved.")


# ================================================================ S27 channels, S28 templates
@router.get("/notification-channels")
def channels_page(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    rows = channels.list_channels(db, p)
    return page("admin/channels.html", shell(request, p, db, rows=rows, registry=channels.REGISTRY,
                                             test_result=None))


def _channel_settings(form, typ):
    spec = channels.REGISTRY.get(typ, channels.Channel).SPEC
    return {k: form[f"set:{k}"] for k in spec if form.get(f"set:{k}")}


@router.post("/notification-channels")
async def channel_create(request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {"type": form.get("type"), "name": form.get("name"), "settings": _channel_settings(form, form.get("type"))}
    return await run(request, p, lambda: channels.create_channel(request.state.db, p, data),
                     "/admin/notification-channels", "Channel added.")


@router.post("/notification-channels/{cid}")
async def channel_update(cid: int, request: Request, p: Principal = Depends(web_principal),
                         db: Session = Depends(get_db)):
    form = await check_csrf(request, p)
    ch = channels.get_channel(db, p, cid)
    data = {}
    if form.get("name"):
        data["name"] = form["name"]
    if "enabled" in form:
        data["enabled"] = tri(form["enabled"])
    if form.get("save_settings"):
        data["settings"] = _channel_settings(form, ch.type)
    return await run(request, p, lambda: channels.update_channel(db, p, cid, data), "/admin/notification-channels",
                     "Channel saved.")


@router.post("/notification-channels/{cid}/test")
async def channel_test(cid: int, request: Request, p: Principal = Depends(web_principal),
                       db: Session = Depends(get_db)):
    await check_csrf(request, p)
    result = channels.test_channel(db, p, cid)
    if result["ok"]:
        return back("/admin/notification-channels", msg="Test message sent.")
    return back("/admin/notification-channels", err="Test failed: " + (result["detail"] or "unknown error"))


@router.get("/notification-templates")
def templates_page(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    rows = channels.list_templates(db, p)
    chans = channels.list_channels(db, p)
    grid = {(t.notification_type, t.channel_id): t for t in rows}
    edit = None
    if request.query_params.get("edit"):
        edit = next((t for t in rows if str(t.id) == request.query_params["edit"]), None)
    preview = None
    if edit is not None:
        preview = channels.preview_template(db, p, edit.id)
    return page("admin/templates.html", shell(request, p, db, chans=chans, grid=grid, types=notify.TYPES, edit=edit,
                                              preview=preview, placeholders=notify.PLACEHOLDERS,
                                              new_type=request.query_params.get("type"),
                                              new_channel=request.query_params.get("channel")))


@router.post("/notification-templates")
async def template_create(request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {"notification_type": form.get("notification_type"), "channel_id": int(form.get("channel_id") or 0),
            "subject": form.get("subject") or None, "body": form.get("body")}
    return await run(request, p, lambda: channels.create_template(request.state.db, p, data),
                     lambda t: f"/admin/notification-templates?edit={t.id}", "Template added.",
                     "/admin/notification-templates")


@router.post("/notification-templates/{tid}")
async def template_update(tid: int, request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    data = {"subject": form.get("subject") or None, "body": form.get("body")}
    if "enabled" in form:
        data["enabled"] = tri(form["enabled"])
    return await run(request, p, lambda: channels.update_template(request.state.db, p, tid, data),
                     f"/admin/notification-templates?edit={tid}", "Template saved.")


# ================================================================ S29 settings, S30 audit
@router.get("/settings")
def settings_page(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    rows = st.get_global_settings(db, p)
    names = S.user_names(db, [r["updated_by"] for r in rows])
    return page("admin/settings.html", shell(request, p, db, rows=rows, names=names))


@router.post("/settings")
async def settings_submit(request: Request, p: Principal = Depends(web_principal)):
    form = await check_csrf(request, p)
    from .. import settings as reg
    changes = {}
    for k, spec in reg.REGISTRY.items():
        if spec.kind is bool:
            if form.get(f"present:{k}"):
                changes[k] = bool(form.get(f"s:{k}"))
        elif f"s:{k}" in form and form[f"s:{k}"] != "":
            changes[k] = form[f"s:{k}"]
    return await run(request, p, lambda: st.put_global_settings(request.state.db, p, changes), "/admin/settings",
                     "Settings saved.")


@router.get("/audit")
def audit_page(request: Request, p: Principal = Depends(web_principal), db: Session = Depends(get_db)):
    from datetime import datetime
    qp = request.query_params

    def d(v):
        try:
            return datetime.fromisoformat(v).replace(tzinfo=__import__("datetime").timezone.utc) if v else None
        except ValueError:
            return None
    rows, meta = st.list_audit(db, p, qp.get("entity") or None, qp.get("entity_id") or None, qp.get("actor") or None,
                               d(qp.get("from")), d(qp.get("to")), int(qp.get("page") or 1), 50)
    names = S.user_names(db, [r.actor_id for r in rows])
    return page("admin/audit.html", shell(request, p, db, rows=rows, meta=meta, names=names, q=dict(qp)))
