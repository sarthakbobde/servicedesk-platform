"""Phase 3: SLA timers on the clock-driven harness. Default calendar Mon-Fri 09:00-17:00 UTC; T0 Monday 10:00."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from ticketing import clock
from ticketing.models import TicketEvent, User
from ticketing.services import jobs
from tests import desk as D

STEPS = [{"name": "Assignment", "minutes": 30, "stop_status_code": "ASSIGNED"},
         {"name": "First response", "minutes": 60, "stop_status_code": "IN_PROGRESS"},
         {"name": "Resolution", "minutes": 480, "stop_status_code": "RESOLVED"}]


def u(*a):
    return datetime(*a, tzinfo=timezone.utc)


def z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_policy(root, priority_id, steps=STEPS, type_id=None, name="Default", calendar_id=None):
    r = root.post("/api/v1/admin/sla-policies", json={"name": name, "priority_id": priority_id,
                                                      "ticket_type_id": type_id, "calendar_id": calendar_id})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = root.put(f"/api/v1/admin/sla-policies/{pid}/metrics", json={"steps": steps})
    assert r.status_code == 200, r.text
    return pid


@pytest.fixture
def d(f, root):
    desk = D.build(f)
    make_policy(root, f.priority("Medium").id)
    return desk


def timers(caller, tid):
    r = caller.get(f"/api/v1/tickets/{tid}/sla")
    assert r.status_code == 200, r.text
    return {x["step"]: x for x in r.json()["items"]}


def act(caller, tid, action, body=None):
    r = caller.post(f"/api/v1/tickets/{tid}/{action}", json=body)
    assert r.status_code == 200, (action, r.text)
    return r.json()


def test_due_times_from_the_policy_calendar(f, d):
    mgr = f.as_(d.manager)
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)  # manual type: stays NEW
    tm = timers(mgr, t["id"])
    assert tm["Assignment"]["due_at"] == z(u(2026, 11, 16, 10, 30))
    assert tm["First response"]["due_at"] == z(u(2026, 11, 16, 11, 0))
    assert tm["Resolution"]["due_at"] == z(u(2026, 11, 17, 10, 0))  # 7 h Monday + 1 h Tuesday
    assert tm["Resolution"]["team_time_zone"] == "UTC" and tm["Resolution"]["state"] == "RUNNING"
    view = mgr.get(f"/api/v1/tickets/{t['id']}").json()
    assert view["nearest_due"]["step"] == "Assignment"


def test_holiday_moves_due_time(f, d, root):
    cal = root.get("/api/v1/admin/calendars").json()["items"][0]
    root.post(f"/api/v1/admin/calendars/{cal['id']}/holidays", json={"date": "2026-11-17", "name": "Holiday"})
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    assert timers(f.as_(d.manager), t["id"])["Resolution"]["due_at"] == z(u(2026, 11, 18, 10, 0))


def test_overnight_raise_after_hours(f, d):
    clock.set_now(u(2026, 11, 16, 16, 45))
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    tm = timers(f.as_(d.manager), t["id"])
    assert tm["Assignment"]["due_at"] == z(u(2026, 11, 17, 9, 15))  # 15 min Monday + 15 min Tuesday


def test_assignment_met_hold_pauses_and_resume_keeps_remaining(f, d):
    mgr = f.as_(d.manager)
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    clock.advance(minutes=10)
    act(mgr, t["id"], "assign", {"resolver_id": d.rob.id})
    rob = f.as_(d.rob)
    clock.advance(minutes=10)  # 10:20
    act(rob, t["id"], "hold", {"reason": "vendor"})
    tm = timers(mgr, t["id"])
    assert tm["Assignment"]["state"] == "MET"
    assert tm["First response"]["state"] == "PAUSED" and tm["First response"]["paused_reason"] == "STATUS"
    assert tm["Resolution"]["due_at"] is None and tm["Resolution"]["minutes_used"] == 20
    clock.advance(hours=2)  # 12:20: paused time does not count
    act(rob, t["id"], "resume")  # resume counts as starting: first response stops (MET)
    tm = timers(mgr, t["id"])
    assert tm["First response"]["state"] == "MET"
    assert tm["Resolution"]["state"] == "RUNNING"
    assert tm["Resolution"]["due_at"] == z(u(2026, 11, 17, 12, 0))  # 460 min left from Mon 12:20


def test_manual_pause_and_status_pause_together(f, d):
    mgr = f.as_(d.manager)
    t = D.raise_ticket(f.as_(d.quinn), d)  # auto-assigned at 10:00
    who = f.as_(f.db.get(User, t["resolver"]["id"]))
    r = mgr.post(f"/api/v1/tickets/{t['id']}/sla/pause", json={"reason": "customer away"})
    assert r.status_code == 200 and r.json()["manual_pause"] is True
    assert mgr.post(f"/api/v1/tickets/{t['id']}/sla/pause", json={"reason": "again"}).status_code == 409
    allowed = mgr.get(f"/api/v1/tickets/{t['id']}").json()["allowed_actions"]
    assert "sla-resume" in allowed and "sla-pause" not in allowed
    clock.advance(minutes=30)
    act(who, t["id"], "hold", {"reason": "x"})
    act(who, t["id"], "resume")  # status pause ends; manual pause still holds the clock
    tm = timers(mgr, t["id"])
    assert tm["Resolution"]["state"] == "PAUSED" and tm["Resolution"]["paused_reason"] == "MANUAL"
    clock.advance(minutes=30)
    r = mgr.post(f"/api/v1/tickets/{t['id']}/sla/resume")
    assert r.status_code == 200
    res = {x["step"]: x for x in r.json()["items"]}["Resolution"]
    assert res["minutes_used"] == 0 and res["due_at"] == z(u(2026, 11, 17, 11, 0))
    assert mgr.post(f"/api/v1/tickets/{t['id']}/sla/resume").status_code == 409


def test_breach_set_once_and_kept_after_stop(f, d):
    mgr = f.as_(d.manager)
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    clock.advance(minutes=31)
    jobs.run_cycle()
    jobs.run_cycle()
    tm = timers(mgr, t["id"])
    assert tm["Assignment"]["state"] == "BREACHED"
    assert tm["Assignment"]["breached_at"] == z(u(2026, 11, 16, 10, 30))
    events = f.db.scalars(select(TicketEvent).where(TicketEvent.type == "sla_breached")).all()
    assert len(events) == 1 and events[0].visibility == "INTERNAL"
    assert mgr.get("/api/v1/tickets?overdue=true").json()["total"] == 1
    act(mgr, t["id"], "assign", {"resolver_id": d.rob.id})
    assert timers(mgr, t["id"])["Assignment"]["state"] == "BREACHED"  # the outcome stays BREACHED
    assert mgr.get("/api/v1/tickets?overdue=true").json()["total"] == 0  # no unfinished step is overdue now
    requester_view = f.as_(d.quinn).get(f"/api/v1/tickets/{t['id']}/events").json()["items"]
    assert all(e["type"] != "sla_breached" for e in requester_view)


def test_quick_fix_closes_earlier_steps(f, d):
    t = D.raise_ticket(f.as_(d.quinn), d)
    who = f.as_(f.db.get(User, t["resolver"]["id"]))
    clock.advance(minutes=5)
    act(who, t["id"], "resolve", {"note": "trivial"})
    tm = timers(f.as_(d.manager), t["id"])
    assert {k: v["state"] for k, v in tm.items()} == {"Assignment": "MET", "First response": "MET",
                                                      "Resolution": "MET"}


def test_reject_and_withdraw_skip_unreached_steps(f, d):
    mgr = f.as_(d.manager)
    t = D.raise_ticket(f.as_(d.quinn), d)
    act(mgr, t["id"], "reject", {"reason_code": "DUPLICATE", "reason": "see ACM-9"})
    tm = timers(mgr, t["id"])
    assert tm["Assignment"]["state"] == "MET"
    assert tm["First response"]["state"] == "SKIPPED" and tm["Resolution"]["state"] == "SKIPPED"
    t2 = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    act(f.as_(d.quinn), t2["id"], "withdraw", {"reason": "nvm"})
    assert {v["state"] for v in timers(mgr, t2["id"]).values()} == {"SKIPPED"}


def test_release_rearms_and_reopen_restarts(f, d):
    mgr = f.as_(d.manager)
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    clock.advance(minutes=10)
    act(mgr, t["id"], "assign", {"resolver_id": d.rob.id})
    assert timers(mgr, t["id"])["Assignment"]["state"] == "MET"
    clock.advance(minutes=10)
    act(f.as_(d.rob), t["id"], "release")
    tm = timers(mgr, t["id"])
    assert tm["Assignment"]["state"] == "RUNNING" and tm["Assignment"]["minutes_used"] == 20
    assert tm["Assignment"]["due_at"] == z(u(2026, 11, 16, 10, 30))
    act(mgr, t["id"], "assign", {"resolver_id": d.rae.id})
    rae = f.as_(d.rae)
    act(rae, t["id"], "start")
    act(rae, t["id"], "resolve", {"note": "done"})
    clock.advance(days=1)  # Tuesday 10:20
    act(f.as_(d.quinn), t["id"], "reopen", {"reason": "back again"})
    tm = timers(mgr, t["id"])
    assert all(v["state"] == "RUNNING" for v in tm.values())
    assert tm["Assignment"]["due_at"] == z(u(2026, 11, 17, 10, 50))


def test_priority_change_replaces_timers_d28(f, d, root):
    high = f.priority("High")
    make_policy(root, high.id, [{"name": "Assignment", "minutes": 15, "stop_status_code": "ASSIGNED"},
                                {"name": "First response", "minutes": 30, "stop_status_code": "IN_PROGRESS"},
                                {"name": "Resolution", "minutes": 240, "stop_status_code": "RESOLVED"}], name="High")
    mgr = f.as_(d.manager)
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    clock.advance(minutes=10)
    act(mgr, t["id"], "assign", {"resolver_id": d.rob.id})
    clock.advance(minutes=10)  # 10:20, 20 working minutes since the SLA start
    act(mgr, t["id"], "priority", {"priority_id": high.id})
    tm = timers(mgr, t["id"])
    assert tm["Assignment"]["state"] == "MET"  # stays MET under the stricter policy
    assert tm["First response"]["minutes_total"] == 30
    assert tm["First response"]["due_at"] == z(u(2026, 11, 16, 10, 30))  # counted from the original start
    assert tm["Resolution"]["due_at"] == z(u(2026, 11, 16, 14, 0))
    clock.advance(minutes=15)
    act(mgr, t["id"], "priority", {"priority_id": f.priority("Medium").id})
    tm = timers(mgr, t["id"])
    assert tm["First response"]["minutes_total"] == 60 and tm["First response"]["state"] == "RUNNING"


def test_extend_adds_to_due_time(f, d):
    mgr = f.as_(d.manager)
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    r = mgr.post(f"/api/v1/tickets/{t['id']}/sla/extend", json={"step": 1, "minutes": 15, "reason": "VIP"})
    assert r.status_code == 200
    a = {x["step"]: x for x in r.json()["items"]}["Assignment"]
    assert a["due_at"] == z(u(2026, 11, 16, 10, 45)) and a["minutes_total"] == 45
    act(mgr, t["id"], "assign", {"resolver_id": d.rob.id})
    r = mgr.post(f"/api/v1/tickets/{t['id']}/sla/extend", json={"step": 1, "minutes": 15, "reason": "late"})
    assert r.status_code == 409
    assert mgr.post(f"/api/v1/tickets/{t['id']}/sla/extend", json={"step": 9, "minutes": 5,
                                                                   "reason": "x"}).status_code == 422


def test_policy_edit_leaves_running_timers(f, d, root):
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    pid = root.get("/api/v1/admin/sla-policies").json()["items"][0]["id"]
    root.put(f"/api/v1/admin/sla-policies/{pid}/metrics", json={"steps": [
        {"name": "Assignment", "minutes": 5, "stop_status_code": "ASSIGNED"},
        {"name": "Resolution", "minutes": 100, "stop_status_code": "RESOLVED"}]})
    tm = timers(f.as_(d.manager), t["id"])
    assert tm["Assignment"]["minutes_total"] == 30 and len(tm) == 3
    t2 = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    tm2 = timers(f.as_(d.manager), t2["id"])
    assert tm2["Assignment"]["minutes_total"] == 5 and len(tm2) == 2


def test_type_policy_beats_default_and_no_policy(f, d, root):
    make_policy(root, f.priority("Medium").id, [{"name": "Resolution", "minutes": 60, "stop_status_code": "RESOLVED"}],
                type_id=d.req_type.id, name="Requests")
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    assert list(timers(f.as_(d.manager), t["id"])) == ["Resolution"]
    low = D.raise_ticket(f.as_(d.quinn), d, d.req_type, priority_id=f.priority("Low").id)
    r = f.as_(d.manager).get(f"/api/v1/tickets/{low['id']}/sla").json()
    assert r["has_policy"] is False and r["items"] == []
    missing = root.get("/api/v1/admin/sla-policies?missing=true").json()["items"]
    assert {"Low", "High", "Critical"} <= {m["priority"]["name"] for m in missing}


def test_requester_cannot_read_sla(f, d):
    t = D.raise_ticket(f.as_(d.quinn), d)
    assert f.as_(d.quinn).get(f"/api/v1/tickets/{t['id']}/sla").status_code == 403
    assert f.as_(f.member("X")).get(f"/api/v1/tickets/{t['id']}/sla").status_code == 404


def test_policy_validation_and_scope(f, d, root):
    pa = f.as_(d.padmin)
    assert pa.post("/api/v1/admin/sla-policies", json={"name": "x", "priority_id": f.priority("Low").id}).status_code == 403
    r = pa.post("/api/v1/admin/sla-policies", json={"name": "Bugs", "priority_id": f.priority("Low").id,
                                                    "ticket_type_id": d.bug.id})
    assert r.status_code == 201
    pid = r.json()["id"]
    bad = pa.put(f"/api/v1/admin/sla-policies/{pid}/metrics", json={"steps": [
        {"name": "A", "minutes": 60, "stop_status_code": "ASSIGNED"},
        {"name": "B", "minutes": 30, "stop_status_code": "IN_PROGRESS"}]})
    assert bad.status_code == 422
    fields = bad.json()["error"]["fields"]
    assert "never decrease" in fields["steps.1.minutes"] and "RESOLVED" in fields["steps"]
    assert pa.put(f"/api/v1/admin/sla-policies/{pid}/metrics", json={"steps": [
        {"name": "A", "minutes": 0, "stop_status_code": "RESOLVED"}]}).status_code == 422
    dup = pa.post("/api/v1/admin/sla-policies", json={"name": "Again", "priority_id": f.priority("Low").id,
                                                      "ticket_type_id": d.bug.id})
    assert dup.status_code == 422
    assert pa.patch(f"/api/v1/admin/sla-policies/{pid}", json={"priority_id": 1}).status_code == 422
    listed = pa.get("/api/v1/admin/sla-policies").json()["items"]
    assert [x["name"] for x in listed] == ["Bugs"]  # the platform default is not in a project admin's scope


SLA_ENDPOINTS = [
    ("GET", "/api/v1/tickets/{t}/sla"), ("POST", "/api/v1/tickets/{t}/sla/pause"),
    ("POST", "/api/v1/tickets/{t}/sla/resume"), ("POST", "/api/v1/tickets/{t}/sla/extend"),
    ("GET", "/api/v1/admin/sla-policies"), ("POST", "/api/v1/admin/sla-policies"),
    ("GET", "/api/v1/admin/sla-policies/{p}"), ("PATCH", "/api/v1/admin/sla-policies/{p}"),
    ("GET", "/api/v1/admin/sla-policies/{p}/metrics"), ("PUT", "/api/v1/admin/sla-policies/{p}/metrics"),
]


def test_phase3_endpoint_count():
    assert len(SLA_ENDPOINTS) == 10


@pytest.mark.parametrize("method,path", SLA_ENDPOINTS)
def test_phase3_role_denial(f, d, client, root, method, path):
    t = D.raise_ticket(f.as_(d.quinn), d)
    pid = root.get("/api/v1/admin/sla-policies").json()["items"][0]["id"]
    url = path.format(t=t["id"], p=pid)
    assert client.request(method, url, json={}).status_code == 401
    for who in (d.quinn, d.rob, f.member("Outsider")):
        r = f.as_(who).client.request(method, url, json={"reason": "x", "step": 1, "minutes": 5},
                                      headers=f.headers(who))
        if method == "GET" and path == "/api/v1/tickets/{t}/sla" and who.id == t["resolver"]["id"]:
            assert r.status_code == 200  # the assigned resolver may read the timers
            continue
        assert r.status_code in (403, 404), (who.name, method, path, r.status_code, r.text)
