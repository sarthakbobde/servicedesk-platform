"""G10 core configuration and Phase 2 role-denial on all 51 endpoints."""
import pytest
from sqlalchemy import select

from ticketing.models import AuditLog, Priority
from tests import desk as D


def test_ticket_types_scope_and_rules(f, root):
    d = D.build(f)
    pa = f.as_(d.padmin)
    other = f.project(d.org, "AC2")
    other_team = f.team(other)
    r = pa.post("/api/v1/admin/ticket-types", json={"project_id": d.proj.id, "name": "Access", "team_id": d.team.id,
                                                    "assignment_mode": "AUTO"})
    assert r.status_code == 201
    r = pa.post("/api/v1/admin/ticket-types", json={"project_id": d.proj.id, "name": "X", "team_id": other_team.id,
                                                    "assignment_mode": "AUTO"})
    assert r.status_code == 422  # team from another project
    assert pa.post("/api/v1/admin/ticket-types", json={"project_id": other.id, "name": "Y", "team_id": other_team.id,
                                                       "assignment_mode": "AUTO"}).status_code == 403
    names = [t["name"] for t in pa.get("/api/v1/admin/ticket-types").json()["items"]]
    assert names == ["Access", "Bug", "Request"]
    assert pa.patch(f"/api/v1/admin/ticket-types/{d.bug.id}", json={"assignment_mode": "MANUAL"}).json()[
        "assignment_mode"] == "MANUAL"
    assert f.db.scalar(select(AuditLog).where(AuditLog.entity == "ticket_type", AuditLog.action == "update"))


def test_priorities_default_moves_and_is_kept(root, f):
    r = root.post("/api/v1/admin/priorities", json={"name": "Urgent", "rank": 0, "is_default": True})
    assert r.status_code == 422  # rank starts at 1
    r = root.post("/api/v1/admin/priorities", json={"name": "Trivial", "rank": 9, "is_default": True})
    assert r.status_code == 201 and r.json()["is_default"]
    f.db.expire_all()
    assert f.db.scalars(select(Priority).where(Priority.is_default.is_(True))).all()[0].name == "Trivial"
    tid = r.json()["id"]
    assert root.patch(f"/api/v1/admin/priorities/{tid}", json={"is_active": False}).status_code == 422
    assert root.patch(f"/api/v1/admin/priorities/{tid}", json={"is_default": False}).status_code == 422
    assert root.post("/api/v1/admin/priorities", json={"name": "Dup", "rank": 9}).status_code == 422


def test_statuses_rename_only(root):
    items = root.get("/api/v1/admin/statuses").json()["items"]
    assert [s["code"] for s in items] == ["NEW", "ASSIGNED", "IN_PROGRESS", "ON_HOLD", "AWAITING_REQUESTER",
                                          "RESOLVED", "REJECTED", "WITHDRAWN", "CLOSED"]
    sid = items[0]["id"]
    assert root.patch(f"/api/v1/admin/statuses/{sid}", json={"name": "Open"}).json()["name"] == "Open"
    assert root.patch(f"/api/v1/admin/statuses/{sid}", json={"category": "DONE"}).status_code == 422
    assert root.get("/api/v1/statuses").json()["items"][0]["name"] == "Open"


def test_calendar_hours_holidays_and_preview(root):
    r = root.post("/api/v1/admin/calendars", json={"name": "London", "time_zone": "Europe/London"})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert root.post("/api/v1/admin/calendars", json={"name": "Bad", "time_zone": "Mars/Base"}).status_code == 422
    bad = root.put(f"/api/v1/admin/calendars/{cid}/hours", json={"windows": [
        {"weekday": 1, "start": "09:00", "end": "17:00"}, {"weekday": 1, "start": "16:00", "end": "18:00"}]})
    assert bad.status_code == 422 and "overlaps" in bad.text
    night = root.put(f"/api/v1/admin/calendars/{cid}/hours", json={"windows": [
        {"weekday": 1, "start": "22:00", "end": "06:00"}, {"weekday": 2, "start": "05:00", "end": "07:00"}]})
    assert night.status_code == 422  # Monday's overnight window spills into Tuesday 05:00
    ok = root.put(f"/api/v1/admin/calendars/{cid}/hours", json={"windows": [
        {"weekday": d, "start": "09:00", "end": "17:30"} for d in range(1, 6)]})
    assert ok.status_code == 200 and len(ok.json()["windows"]) == 5
    h = root.post(f"/api/v1/admin/calendars/{cid}/holidays", json={"date": "2026-11-17", "name": "Day off"})
    assert h.status_code == 201 and h.json()["whole_day"]
    p = root.post(f"/api/v1/admin/calendars/{cid}/holidays",
                  json={"date": "2026-11-18", "name": "Half day", "start": "13:00", "end": "17:30"})
    assert p.status_code == 201
    preview = root.get(f"/api/v1/admin/calendars/{cid}").json()["preview"]
    assert preview[0]["minutes"] == 510 and preview[1]["minutes"] == 0 and preview[2]["minutes"] == 240
    assert root.patch(f"/api/v1/admin/calendar-holidays/{h.json()['id']}", json={"is_active": False}).status_code == 200
    assert len(root.get(f"/api/v1/admin/calendars/{cid}/holidays").json()["items"]) == 1
    # default moves; the default cannot be deactivated
    assert root.patch(f"/api/v1/admin/calendars/{cid}", json={"is_default": True}).json()["is_default"]
    assert root.patch(f"/api/v1/admin/calendars/{cid}", json={"is_active": False}).status_code == 422


# ---------------------------------------------------------------- role denial: all 51 Phase 2 endpoints
PHASE2 = [
    ("GET", "/api/v1/projects", "any"), ("GET", "/api/v1/projects/{proj}/ticket-types", "member"),
    ("GET", "/api/v1/priorities", "any"), ("GET", "/api/v1/statuses", "any"),
    ("GET", "/api/v1/teams/{team}/members", "manager"),
    ("POST", "/api/v1/tickets", "member"), ("GET", "/api/v1/tickets", "any"), ("GET", "/api/v1/tickets/{t}", "member"),
    ("GET", "/api/v1/tickets/{t}/events", "member"), ("GET", "/api/v1/tickets/counts", "any"),
] + [("POST", f"/api/v1/tickets/{{t}}/{a}", "member") for a in (
    "claim", "assign", "start", "hold", "request-info", "resume", "respond", "resolve", "reject", "withdraw",
    "release", "reassign", "reopen", "priority", "estimate")] + [
    ("GET", "/api/v1/notifications", "any"), ("GET", "/api/v1/notifications/unread-count", "any"),
    ("POST", "/api/v1/notifications/{n}/read", "member"), ("POST", "/api/v1/notifications/read-all", "any"),
    ("PUT", "/api/v1/product-users/x1", "credential"), ("GET", "/api/v1/product-users/x1", "credential"),
] + [(m, "/api/v1/admin/" + path, "admin") for m, path in (
    ("GET", "ticket-types"), ("POST", "ticket-types"), ("GET", "ticket-types/{type}"),
    ("PATCH", "ticket-types/{type}"), ("GET", "priorities"), ("POST", "priorities"), ("GET", "priorities/{prio}"),
    ("PATCH", "priorities/{prio}"), ("GET", "statuses"), ("GET", "statuses/{status}"), ("PATCH", "statuses/{status}"),
    ("GET", "calendars"), ("POST", "calendars"), ("GET", "calendars/{cal}"), ("PATCH", "calendars/{cal}"),
    ("GET", "calendars/{cal}/hours"), ("PUT", "calendars/{cal}/hours"), ("GET", "calendars/{cal}/holidays"),
    ("POST", "calendars/{cal}/holidays"), ("PATCH", "calendar-holidays/{hol}"))]


def test_phase2_endpoint_count():
    assert len(PHASE2) == 51


@pytest.fixture
def world(f, root):
    d = D.build(f)
    t = D.raise_ticket(f.as_(d.quinn), d)
    cal = root.post("/api/v1/admin/calendars", json={"name": "C2", "time_zone": "UTC"}).json()
    hol = root.post(f"/api/v1/admin/calendars/{cal['id']}/holidays", json={"date": "2026-12-25", "name": "X"}).json()
    from ticketing.models import Notification
    n = f.db.scalar(select(Notification.id))
    status_id = root.get("/api/v1/admin/statuses").json()["items"][0]["id"]
    ids = {"proj": d.proj.id, "team": d.team.id, "t": t["id"], "n": n, "type": d.bug.id,
           "prio": f.priority("Low").id, "status": status_id, "cal": cal["id"], "hol": hol["id"]}
    return d, ids


@pytest.mark.parametrize("method,path,need", PHASE2)
def test_phase2_unauthenticated_is_401(client, world, method, path, need):
    _, ids = world
    r = client.request(method, path.format(**ids), json={})
    assert r.status_code == 401, (method, path, r.text)


@pytest.mark.parametrize("method,path,need", [x for x in PHASE2 if x[2] not in ("any",)])
def test_phase2_outsider_denied(f, world, method, path, need):
    """An active member of another organisation with no role sees nothing of the project."""
    _, ids = world
    outsider = f.member("Outsider")
    r = f.as_(outsider).client.request(method, path.format(**ids), json={}, headers=f.headers(outsider))
    if need == "credential":
        assert r.status_code == 401
    elif path == "/api/v1/tickets":
        assert r.status_code == 422  # F3.3: not a project the person may raise in (validated as input)
    else:
        assert r.status_code in (403, 404), (method, path, r.status_code, r.text)


@pytest.mark.parametrize("method,path,need", [x for x in PHASE2 if x[2] == "admin"])
def test_phase2_admin_endpoints_deny_resolver(f, world, method, path, need):
    d, ids = world
    r = f.as_(d.rob).client.request(method, path.format(**ids), json={}, headers=f.headers(d.rob))
    assert r.status_code in (403, 404), (method, path, r.status_code)


@pytest.mark.parametrize("method,path,need", [x for x in PHASE2 if x[2] == "admin"
                                              and "ticket-types" not in x[1]])
def test_platform_config_denies_project_admin(f, world, method, path, need):
    d, ids = world
    r = f.as_(d.padmin).client.request(method, path.format(**ids), json={}, headers=f.headers(d.padmin))
    assert r.status_code in (403, 404), (method, path, r.status_code)
