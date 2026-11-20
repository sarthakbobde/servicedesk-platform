"""Phase 2 acceptance scenarios: intake, routing, lifecycle, system actions, races, idempotency."""
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from ticketing import clock
from ticketing.db import SessionLocal
from ticketing.errors import AppError
from ticketing.models import Notification, Ticket, TicketEvent, User
from ticketing.permissions import load_principal
from ticketing.services import jobs, tickets
from tests import desk as D


@pytest.fixture
def d(f):
    return D.build(f)


def notes_for(f, user, ntype=None):
    f.db.expire_all()
    q = select(Notification).where(Notification.user_id == user.id)
    if ntype:
        q = q.where(Notification.type == ntype)
    return list(f.db.scalars(q))


def act(caller, ref, action, body=None):
    return caller.post(f"/api/v1/tickets/{ref}/{action}", json=body)


def test_raise_auto_assign_work_resolve_and_auto_close(f, d):
    quinn, rob, rae = f.as_(d.quinn), f.as_(d.rob), f.as_(d.rae)
    t = D.raise_ticket(quinn, d)
    assert t["number"] == "ACM-1" and t["source"] == "API"
    assert t["status"]["code"] == "ASSIGNED"  # auto routing at creation
    assert notes_for(f, d.quinn, "ACKNOWLEDGEMENT")
    assignee = rob if t["resolver"]["id"] == d.rob.id else rae
    assert notes_for(f, f.db.get(User, t["resolver"]["id"]), "ASSIGNMENT")
    assert act(assignee, t["id"], "start").json()["status"]["code"] == "IN_PROGRESS"
    assert act(assignee, t["id"], "hold", {"reason": "waiting for vendor"}).json()["status"]["code"] == "ON_HOLD"
    assert act(assignee, t["id"], "resume").json()["status"]["code"] == "IN_PROGRESS"
    r = act(assignee, t["id"], "resolve", {"note": "Fixed the config", "actual_effort_minutes": 45})
    body = r.json()
    assert body["status"]["code"] == "RESOLVED" and body["completed_at"]
    assert body["actual_effort_minutes"] == 45 and body["resolution_minutes"] == 0
    assert notes_for(f, d.quinn, "RESOLUTION")
    # requester view hides effort
    seen = quinn.get(f"/api/v1/tickets/{t['id']}").json()
    assert "actual_effort_minutes" not in seen and seen["allowed_actions"] == ["reopen"]
    clock.advance(days=6)
    jobs.run_cycle()
    assert quinn.get(f"/api/v1/tickets/ACM-1").json()["status"]["code"] == "RESOLVED"
    clock.advance(days=1, minutes=1)
    jobs.run_cycle()
    final = quinn.get("/api/v1/tickets/ACM-1").json()
    assert final["status"]["code"] == "CLOSED" and final["allowed_actions"] == []
    types = [e["type"] for e in rob.client.get(f"/api/v1/tickets/{t['id']}/events",
                                               headers=f.headers(d.manager)).json()["items"]]
    assert types == ["auto_closed", "resolved", "resumed", "held", "started", "assigned", "created"]


def test_resolution_time_in_working_minutes(f, d):
    quinn = f.as_(d.quinn)
    t = D.raise_ticket(quinn, d)
    who = f.as_(f.db.get(User, t["resolver"]["id"]))
    clock.advance(hours=8)  # Monday 18:00: 7 working hours elapsed (10:00-17:00)
    r = act(who, t["id"], "resolve", {"note": "done"})
    assert r.json()["resolution_minutes"] == 420


def test_manual_type_waits_for_manager(f, d):
    quinn, mgr, rob = f.as_(d.quinn), f.as_(d.manager), f.as_(d.rob)
    t = D.raise_ticket(quinn, d, d.req_type)
    assert t["status"]["code"] == "NEW" and t["resolver"] is None
    assert act(rob, t["id"], "claim").status_code == 409  # manual types are not claimable
    members = mgr.get(f"/api/v1/teams/{d.team.id}/members").json()["items"]
    assert {m["user"]["name"] for m in members} == {"Mia Manager", "Rob Resolver", "Rae Resolver"}
    assert all(m["on_shift"] for m in members)
    outsider = f.member("Ollie", org=d.org)
    r = act(mgr, t["id"], "assign", {"resolver_id": outsider.id})
    assert r.status_code == 422 and r.json()["error"]["code"] == "resolver_not_in_team"
    r = act(mgr, t["id"], "assign", {"resolver_id": d.rob.id})
    assert r.status_code == 200 and r.json()["resolver"]["id"] == d.rob.id
    assert notes_for(f, d.rob, "ASSIGNMENT")


def test_nobody_on_shift_then_claim_or_retry(f, d):
    clock.set_now(datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc))  # Saturday
    quinn, rae = f.as_(d.quinn), f.as_(d.rae)
    t1 = D.raise_ticket(quinn, d, title="first")
    t2 = D.raise_ticket(quinn, d, title="second")
    assert t1["status"]["code"] == "NEW" and t1["resolver"] is None
    seen = rae.get(f"/api/v1/tickets/{t1['id']}").json()
    assert "claim" in seen["allowed_actions"]
    assert act(rae, t1["id"], "claim").json()["resolver"]["id"] == d.rae.id
    jobs.run_cycle()  # still weekend: nothing routed
    assert rae.get(f"/api/v1/tickets/{t2['id']}").json()["status"]["code"] == "NEW"
    clock.set_now(datetime(2026, 11, 23, 9, 1, tzinfo=timezone.utc))  # shift starts Monday 09:00
    jobs.run_cycle()
    after = f.as_(d.manager).get(f"/api/v1/tickets/{t2['id']}").json()
    assert after["status"]["code"] == "ASSIGNED"
    assert after["resolver"]["id"] == d.rob.id  # fewest open: Rae already holds t1


def test_two_shift_calendars_only_on_shift_is_picked(f):
    night = f.calendar("Night shift", windows=[(d, 1320, 360) for d in range(1, 8)])
    day = f.calendar("Day shift", windows=[(d, 480, 1200) for d in range(1, 8)])
    org = f.org("Acme")
    proj = f.project(org, "ACM")
    team = f.team(proj)
    bug = f.ticket_type(proj, team, "Bug", "AUTO")
    nina, dan = f.member("Nina", org=org), f.member("Dan", org=org)
    f.grant(nina, "RESOLVER", team=team, calendar=night)
    f.grant(dan, "RESOLVER", team=team, calendar=day)
    req = f.as_(f.member("Req", org=org))
    body = {"project_id": proj.id, "type_id": bug.id, "title": "x", "description": "y"}
    clock.set_now(datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc))
    assert req.post("/api/v1/tickets", json=body).json()["resolver"]["id"] == nina.id
    clock.set_now(datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc))
    for _ in range(3):
        assert req.post("/api/v1/tickets", json=body).json()["resolver"]["id"] == dan.id


def test_fewest_open_then_longest_idle(f, d):
    quinn = f.as_(d.quinn)
    a = D.raise_ticket(quinn, d)
    b = D.raise_ticket(quinn, d)
    assert {a["resolver"]["id"], b["resolver"]["id"]} == {d.rob.id, d.rae.id}
    first = a["resolver"]["id"]
    clock.advance(minutes=1)
    c = D.raise_ticket(quinn, d)
    assert c["resolver"]["id"] == first  # equal counts: the longest idle (assigned earliest) wins


def test_reject_and_reopen_rules(f, d):
    quinn, mgr = f.as_(d.quinn), f.as_(d.manager)
    t = D.raise_ticket(quinn, d)
    r = act(mgr, t["id"], "reject", {"reason_code": "WRONG_TEAM", "reason": "Belongs to billing"})
    assert r.json()["status"]["code"] == "REJECTED"
    assert notes_for(f, d.quinn, "REJECTION")
    r = act(quinn, t["id"], "reopen", {"reason": "please"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "reopen_blocked"
    t2 = D.raise_ticket(quinn, d)
    act(mgr, t2["id"], "reject", {"reason_code": "INFO_MISSING", "reason": "need logs"})
    r = act(quinn, t2["id"], "reopen", {"reason": "logs attached in description"})
    assert r.status_code == 200 and r.json()["status"]["code"] in ("NEW", "ASSIGNED")
    assert r.json()["completed_at"] is None
    t3 = D.raise_ticket(quinn, d)
    act(mgr, t3["id"], "reject", {"reason_code": "OTHER", "reason": "no"})
    clock.advance(days=7, minutes=1)
    r = act(quinn, t3["id"], "reopen", {"reason": "late"})
    assert r.json()["error"]["code"] == "reopen_window_closed"
    assert act(quinn, t3["id"], "reopen", {"reason": "  "}).status_code == 422


def test_reopen_blocked_into_inactive_type(f, d, root):
    quinn = f.as_(d.quinn)
    t = D.raise_ticket(quinn, d)
    who = f.as_(f.db.get(User, t["resolver"]["id"]))
    act(who, t["id"], "resolve", {"note": "done"})
    root.patch(f"/api/v1/admin/ticket-types/{d.bug.id}", json={"is_active": False})
    assert act(quinn, t["id"], "reopen", {"reason": "again"}).json()["error"]["code"] == "reopen_blocked"


def test_withdraw_rules(f, d):
    quinn = f.as_(d.quinn)
    t = D.raise_ticket(quinn, d)
    who = f.as_(f.db.get(User, t["resolver"]["id"]))
    act(who, t["id"], "start")
    assert act(quinn, t["id"], "withdraw", {"reason": "nvm"}).status_code == 409  # not offered IN_PROGRESS
    act(who, t["id"], "hold", {"reason": "x"})
    assert act(who, t["id"], "withdraw", {"reason": "x"}).status_code == 403  # requester only
    r = act(quinn, t["id"], "withdraw", {"reason": "solved myself"})
    assert r.json()["status"]["code"] == "WITHDRAWN"


def test_request_info_respond_and_no_reply(f, d):
    quinn = f.as_(d.quinn)
    t = D.raise_ticket(quinn, d)
    who_user = f.db.get(User, t["resolver"]["id"])
    who = f.as_(who_user)
    act(who, t["id"], "request-info", {"question": "Which browser?"})
    assert notes_for(f, d.quinn, "INFO_REQUEST")
    assert quinn.get("/api/v1/tickets/counts").json()["waiting_on_me"] == 1
    assert act(who, t["id"], "respond", {"answer": "x"}).status_code == 403
    r = act(quinn, t["id"], "respond", {"answer": "Firefox"})
    assert r.json()["status"]["code"] == "IN_PROGRESS"
    assert notes_for(f, who_user, "REQUESTER_RESPONSE")
    act(who, t["id"], "request-info", {"question": "Version?"})
    clock.advance(days=7, minutes=1)
    jobs.run_cycle()
    final = quinn.get(f"/api/v1/tickets/{t['id']}").json()
    assert final["status"]["code"] == "REJECTED" and final["reject_reason_code"] == "NO_REPLY"
    assert notes_for(f, d.quinn, "NO_REPLY")
    ev = quinn.get(f"/api/v1/tickets/{t['id']}/events").json()["items"][0]
    assert ev["actor"] == "System" and ev["reason_code"] == "NO_REPLY"


def test_release_skips_releasing_resolver_once_and_reassign(f, d):
    quinn, mgr = f.as_(d.quinn), f.as_(d.manager)
    t = D.raise_ticket(quinn, d)
    first = t["resolver"]["id"]
    other = d.rae.id if first == d.rob.id else d.rob.id
    r = act(f.as_(f.db.get(User, first)), t["id"], "release")
    assert r.status_code == 200
    after = mgr.get(f"/api/v1/tickets/{t['id']}").json()
    assert after["resolver"]["id"] == other  # D30: releasing resolver skipped
    r = act(mgr, t["id"], "reassign", {"resolver_id": other, "reason": "same"})
    assert r.json()["error"]["code"] == "no_change"
    act(f.as_(f.db.get(User, other)), t["id"], "start")
    r = act(mgr, t["id"], "reassign", {"resolver_id": first, "reason": "load balancing"})
    assert r.json()["resolver"]["id"] == first and r.json()["status"]["code"] == "ASSIGNED"
    act(f.as_(f.db.get(User, first)), t["id"], "hold", {"reason": "x"})
    r = act(mgr, t["id"], "reassign", {"resolver_id": other, "reason": "back"})
    assert r.json()["status"]["code"] == "ON_HOLD"  # paused stays paused


def test_release_only_candidate_waits(f):
    org = f.org("Acme")
    proj = f.project(org, "ACM")
    team = f.team(proj)
    bug = f.ticket_type(proj, team, "Bug", "AUTO")
    solo = f.member("Solo", org=org)
    f.grant(solo, "RESOLVER", team=team)
    req = f.as_(f.member("Req", org=org))
    t = req.post("/api/v1/tickets", json={"project_id": proj.id, "type_id": bug.id, "title": "x",
                                          "description": "y"}).json()
    assert t["resolver"]["id"] == solo.id
    r = f.as_(solo).post(f"/api/v1/tickets/{t['id']}/release")
    assert r.status_code == 200
    f.db.expire_all()
    tk = f.db.get(Ticket, t["id"])
    assert tk.status_code == "NEW" and tk.resolver_id is None and tk.waiting_for_candidate


def test_priority_and_estimate(f, d):
    quinn, mgr, rob = f.as_(d.quinn), f.as_(d.manager), f.as_(d.rob)
    t = D.raise_ticket(quinn, d)
    assert t["priority"]["name"] == "Medium"
    high = f.priority("High")
    assert act(rob, t["id"], "priority", {"priority_id": high.id}).status_code == 403
    assert act(mgr, t["id"], "priority", {"priority_id": high.id}).json()["priority"]["name"] == "High"
    assert act(mgr, t["id"], "priority", {"priority_id": high.id}).json()["error"]["code"] == "no_change"
    assert act(mgr, t["id"], "estimate", {"minutes": 90}).json()["estimate_minutes"] == 90
    assert act(mgr, t["id"], "estimate", {"minutes": 0}).status_code == 422
    public = quinn.get(f"/api/v1/tickets/{t['id']}/events").json()["items"]
    assert all(e["type"] not in ("priority_changed", "estimate_set") for e in public)


def test_requester_sees_public_history_without_hidden_notes(f, d):
    quinn = f.as_(d.quinn)
    t = D.raise_ticket(quinn, d)
    who = f.as_(f.db.get(User, t["resolver"]["id"]))
    act(who, t["id"], "hold", {"reason": "secret vendor issue"})
    act(who, t["id"], "resume")
    act(who, t["id"], "request-info", {"question": "Screenshot?"})
    items = quinn.get(f"/api/v1/tickets/{t['id']}/events").json()["items"]
    held = next(e for e in items if e["type"] == "held")
    asked = next(e for e in items if e["type"] == "info_requested")
    assert held["note"] is None and asked["note"] == "Screenshot?"
    assert "visibility" not in held
    view = quinn.get(f"/api/v1/tickets/{t['id']}").json()
    assert "hold_reason" not in view and "nearest_due" not in view and "id" not in view["team"]


def test_ten_concurrent_claims_one_winner(f):
    org = f.org("Acme")
    proj = f.project(org, "ACM")
    team = f.team(proj)
    bug = f.ticket_type(proj, team, "Bug", "AUTO")
    people = [f.member(f"R{i}", org=org) for i in range(10)]
    weekend = f.calendar("Never", windows=[])
    for u in people:
        f.grant(u, "RESOLVER", team=team, calendar=weekend)  # nobody on shift: ticket waits to be claimed
    req = f.as_(f.member("Req", org=org))
    t = req.post("/api/v1/tickets", json={"project_id": proj.id, "type_id": bug.id, "title": "race",
                                          "description": "go"}).json()
    assert t["resolver"] is None
    results, barrier = [], threading.Barrier(10)

    def claim(uid):
        with SessionLocal() as db:
            p = load_principal(db, db.get(User, uid))
            barrier.wait()
            try:
                tickets.perform(db, p, t["id"], "claim")
                db.commit()
                results.append("ok")
            except AppError as e:
                db.rollback()
                results.append(e.code)

    threads = [threading.Thread(target=claim, args=(u.id,)) for u in people]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert results.count("ok") == 1
    assert results.count("already_claimed") == 9


def test_idempotency_key(f, d):
    quinn = f.as_(d.quinn)
    body = {"project_id": d.proj.id, "type_id": d.bug.id, "title": "dup", "description": "x"}
    a = quinn.post("/api/v1/tickets", json=body, headers={"Idempotency-Key": "k1"})
    b = quinn.post("/api/v1/tickets", json=body, headers={"Idempotency-Key": "k1"})
    assert a.status_code == 201 and b.status_code == 200 and a.json()["id"] == b.json()["id"]
    c = quinn.post("/api/v1/tickets", json={**body, "title": "other"}, headers={"Idempotency-Key": "k1"})
    assert c.status_code == 409 and c.json()["error"]["code"] == "idempotency_conflict"
    assert f.db.scalar(select(Ticket.id).where(Ticket.number == 2)) is None


def test_validation_on_raise(f, d, root):
    quinn = f.as_(d.quinn)
    bad = quinn.post("/api/v1/tickets", json={"project_id": d.proj.id, "type_id": d.bug.id, "title": " ",
                                              "description": ""})
    assert bad.status_code == 422 and set(bad.json()["error"]["fields"]) >= {"title", "description"}
    long = quinn.post("/api/v1/tickets", json={"project_id": d.proj.id, "type_id": d.bug.id, "title": "x" * 201,
                                               "description": "y"})
    assert long.status_code == 422
    other = f.project(d.org, "AC2")
    other_team = f.team(other)
    other_type = f.ticket_type(other, other_team, "Bug")
    r = quinn.post("/api/v1/tickets", json={"project_id": d.proj.id, "type_id": other_type.id, "title": "x",
                                            "description": "y"})
    assert r.json()["error"]["code"] == "inactive_reference"
    root.patch(f"/api/v1/admin/priorities/{f.priority('Low').id}", json={"is_active": False})
    r = quinn.post("/api/v1/tickets", json={"project_id": d.proj.id, "type_id": d.bug.id, "title": "x",
                                            "description": "y", "priority_id": f.priority("Low").id})
    assert r.json()["error"]["code"] == "inactive_reference"


def test_who_may_raise_d21(f, d):
    beta = f.org("Beta")
    pb = f.project(beta, "BET")
    tb = f.team(pb)
    tyb = f.ticket_type(pb, tb, "Bug")
    quinn = f.as_(d.quinn)  # Acme member, no role
    r = quinn.post("/api/v1/tickets", json={"project_id": pb.id, "type_id": tyb.id, "title": "x", "description": "y"})
    assert r.status_code == 422  # not a project they may raise in
    assert [p["key"] for p in quinn.get("/api/v1/projects").json()["items"]] == ["ACM"]
    internal_resolver = f.member("Ivy")
    f.grant(internal_resolver, "RESOLVER", team=tb)
    ivy = f.as_(internal_resolver)
    keys = {p["key"] for p in ivy.get("/api/v1/projects").json()["items"]}
    assert keys == {"BET"}  # internal org has no projects; role scope adds BET
    assert ivy.post("/api/v1/tickets", json={"project_id": pb.id, "type_id": tyb.id, "title": "x",
                                             "description": "y"}).status_code == 201


def test_ticket_number_lookup_and_scope_404(f, d):
    quinn = f.as_(d.quinn)
    D.raise_ticket(quinn, d)
    assert quinn.get("/api/v1/tickets/ACM-1").status_code == 200
    stranger = f.as_(f.member("Stranger"))
    assert stranger.get("/api/v1/tickets/ACM-1").status_code == 404
    assert stranger.post("/api/v1/tickets/ACM-1/reopen", json={"reason": "x"}).status_code == 404


def test_list_views_filters_and_counts(f, d):
    quinn, rob, mgr = f.as_(d.quinn), f.as_(d.rob), f.as_(d.manager)
    D.raise_ticket(quinn, d, title="alpha")
    D.raise_ticket(quinn, d, d.req_type, title="beta")
    D.raise_ticket(quinn, d, title="gamma")
    assert quinn.get("/api/v1/tickets?view=mine").json()["total"] == 3
    assert quinn.get("/api/v1/tickets?view=queue").status_code == 403
    assert rob.get("/api/v1/tickets?view=queue").json()["total"] == 1  # the manual one
    assert mgr.get("/api/v1/tickets?view=team").json()["total"] == 3
    assert mgr.get("/api/v1/tickets?q=bet").json()["items"][0]["title"] == "beta"
    assert mgr.get("/api/v1/tickets?q=ACM-3").json()["items"][0]["title"] == "gamma"
    assert mgr.get("/api/v1/tickets?status=NEW").json()["total"] == 1
    assert mgr.get("/api/v1/tickets?status=BOGUS").status_code == 422
    assert mgr.get("/api/v1/tickets?resolver=none").json()["total"] == 1
    assert [x["title"] for x in mgr.get("/api/v1/tickets?sort=title").json()["items"]] == ["alpha", "beta", "gamma"]
    assert mgr.get("/api/v1/tickets?overdue=true").json()["total"] == 0  # no SLA before Phase 3
    c = rob.get("/api/v1/tickets/counts").json()
    assert c["queue"] == 1 and c["waiting_on_me"] == 0
    assert quinn.get("/api/v1/tickets/counts").json() == {"queue": 0, "my_work": 0, "waiting_on_me": 0}


def test_notifications_inbox(f, d):
    quinn = f.as_(d.quinn)
    D.raise_ticket(quinn, d)
    D.raise_ticket(quinn, d)
    assert quinn.get("/api/v1/notifications/unread-count").json() == {"unread": 2}
    items = quinn.get("/api/v1/notifications").json()["items"]
    assert items[0]["ticket"]["number"] == "ACM-2" and "ACM-2" in items[0]["body"]
    assert quinn.post(f"/api/v1/notifications/{items[0]['id']}/read").status_code == 204
    assert quinn.get("/api/v1/notifications?unread=true").json()["total"] == 1
    other = f.as_(d.rob)
    assert other.post(f"/api/v1/notifications/{items[1]['id']}/read").status_code == 404
    assert quinn.post("/api/v1/notifications/read-all").status_code == 204
    assert quinn.get("/api/v1/notifications/unread-count").json() == {"unread": 0}


def test_deactivated_resolver_tickets_flagged(f, d, root):
    quinn = f.as_(d.quinn)
    t = D.raise_ticket(quinn, d)
    holder = t["resolver"]["id"]
    r = root.patch(f"/api/v1/admin/users/{holder}", json={"is_active": False})
    assert r.json()["open_tickets_held"] == 1
    view = f.as_(d.manager).get(f"/api/v1/tickets/{t['id']}").json()
    assert view["resolver_active"] is False
