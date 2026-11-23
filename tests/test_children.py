"""Child tickets (CHANGES P5-17): split one request across teams, one level deep, parent finishes last."""
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ticketing import clock
from ticketing.models import Ticket, TicketEvent
from ticketing.services import jobs
from tests import desk as D
from tests.conftest import PASSWORD


@pytest.fixture
def w(f):
    d = D.build(f)
    net = f.team(d.proj, "Network")
    d.net_type = f.ticket_type(d.proj, net, "Network change", "MANUAL")
    d.nia = f.member("Nia Network", org=d.org)
    d.nmgr = f.member("Ned Netmanager", org=d.org)
    f.grant(d.nia, "RESOLVER", team=net)
    f.grant(d.nmgr, "TEAM_MANAGER", team=net)
    d.net = net
    f.commit()
    return d


def act(caller, ref, action, body=None):
    return caller.post(f"/api/v1/tickets/{ref}/{action}", json=body)


def parent_with_rob(f, d, title="New starter: Jonas"):
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type, title=title)
    assert act(f.as_(d.manager), t["id"], "assign", {"resolver_id": d.rob.id}).status_code == 200
    return t


def child_body(d, **extra):
    return {"type_id": d.net_type.id, "title": "Open the firewall port", "description": "Port 8443 for the app.",
            **extra}


def test_child_goes_to_another_team_and_parent_waits_for_it(f, w):
    d = w
    rob, nia = f.as_(d.rob), f.as_(d.nia)
    p = parent_with_rob(f, d)
    r = rob.post(f"/api/v1/tickets/{p['id']}/children", json=child_body(d))
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["parent"]["id"] == p["id"] and c["parent"]["number"] == p["number"]
    assert c["team"]["name"] == "Network" and c["requester"]["id"] == d.rob.id
    assert c["priority"]["id"] == p["priority"]["id"]  # inherits the parent's priority
    assert c["status"]["code"] == "NEW"

    parent = rob.get(f"/api/v1/tickets/{p['id']}").json()
    assert [x["id"] for x in parent["children"]] == [c["id"]]
    assert parent["children"][0]["team"] == "Network"
    assert "resolve" not in parent["allowed_actions"] and "reject" not in parent["allowed_actions"]
    blocked = act(rob, p["id"], "resolve", {"note": "done"})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "children_open"
    assert c["number"] in blocked.json()["error"]["message"]
    assert act(f.as_(d.quinn), p["id"], "withdraw", {"reason": "no"}).json()["error"]["code"] == "children_open"

    # the network team works the child
    assert act(f.as_(d.nmgr), c["id"], "assign", {"resolver_id": d.nia.id}).status_code == 200
    assert act(nia, c["id"], "resolve", {"note": "Port open"}).status_code == 200
    types = [e.type for e in f.db.scalars(select(TicketEvent).where(TicketEvent.ticket_id == p["id"]))]
    assert "child_created" in types and "child_finished" in types

    done = act(rob, p["id"], "resolve", {"note": "All set up"})
    assert done.status_code == 200 and done.json()["status"]["code"] == "RESOLVED"


def test_one_level_and_only_open_parents(f, w):
    d = w
    rob = f.as_(d.rob)
    p = parent_with_rob(f, d)
    c = rob.post(f"/api/v1/tickets/{p['id']}/children", json=child_body(d)).json()
    padmin = f.as_(d.padmin)  # sees the child, may add children in general
    r = padmin.post(f"/api/v1/tickets/{c['id']}/children", json=child_body(d))
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_state"
    assert padmin.get(f"/api/v1/tickets/{c['id']}").json()["can_add_child"] is False
    # a finished parent takes no new children
    assert act(f.as_(d.nmgr), c["id"], "reject", {"reason_code": "OTHER", "reason": "not needed"}).status_code == 200
    assert act(rob, p["id"], "resolve", {"note": "ok"}).status_code == 200
    r = padmin.post(f"/api/v1/tickets/{p['id']}/children", json=child_body(d))
    assert r.status_code == 409


def test_children_role_denial(f, w):
    d = w
    p = parent_with_rob(f, d)
    outsider = f.member("Otto Outsider")
    # requester sees the parent but may not split it; permission wins over validation
    assert f.as_(d.quinn).post(f"/api/v1/tickets/{p['id']}/children", json={}).status_code == 403
    # a resolver of the team who does not hold it, and people outside, do not see it at all
    assert f.as_(d.rae).post(f"/api/v1/tickets/{p['id']}/children", json=child_body(d)).status_code == 404
    assert f.as_(outsider).post(f"/api/v1/tickets/{p['id']}/children", json=child_body(d)).status_code == 404
    assert f.as_(d.nia).post(f"/api/v1/tickets/{p['id']}/children", json=child_body(d)).status_code == 404
    # the team manager and the project admin may
    assert f.as_(d.manager).post(f"/api/v1/tickets/{p['id']}/children", json=child_body(d)).status_code == 201
    assert f.as_(d.padmin).post(f"/api/v1/tickets/{p['id']}/children", json=child_body(d)).status_code == 201
    # invalid body from someone allowed: 422 with fields
    r = f.as_(d.rob).post(f"/api/v1/tickets/{p['id']}/children", json={"type_id": d.net_type.id, "title": "",
                                                                           "description": "x"})
    assert r.status_code == 422


def test_requester_sees_children_without_internal_detail(f, w):
    d = w
    p = parent_with_rob(f, d)
    c = f.as_(d.rob).post(f"/api/v1/tickets/{p['id']}/children", json=child_body(d)).json()
    seen = f.as_(d.quinn).get(f"/api/v1/tickets/{p['id']}").json()
    kid = seen["children"][0]
    assert kid["number"] == c["number"] and kid["visible"] is False
    assert "team" not in kid and "resolver" not in kid
    assert f.as_(d.quinn).get(f"/api/v1/tickets/{c['id']}").status_code == 404
    evs = f.as_(d.quinn).get(f"/api/v1/tickets/{p['id']}/events").json()["items"]
    assert "child_created" not in [e["type"] for e in evs]  # internal


def test_no_reply_waits_for_open_children(f, w):
    d = w
    rob = f.as_(d.rob)
    p = parent_with_rob(f, d)
    assert act(rob, p["id"], "start").status_code == 200
    rob.post(f"/api/v1/tickets/{p['id']}/children", json=child_body(d))
    assert act(rob, p["id"], "request-info", {"question": "Which desk?"}).status_code == 200
    clock.advance(days=8)
    jobs.run_cycle()
    f.db.expire_all()
    assert f.db.get(Ticket, p["id"]).status_code == "AWAITING_REQUESTER"


def portal(user):
    from ticketing.main import app
    c = TestClient(app)
    r = c.post("/login", data={"email": user.email, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def test_child_ticket_through_the_portal(f, w):
    d = w
    p = parent_with_rob(f, d)
    rob = portal(d.rob)
    page = rob.get(f"/tickets/{p['id']}").text
    assert "Child tickets" in page and f"/tickets/new?parent={p['id']}" in page
    form = rob.get(f"/tickets/new?parent={p['id']}").text
    assert "New child ticket" in form and "Network change" in form
    token = re.search(r'name="csrf" value="([^"]+)"', form).group(1)
    r = rob.post("/tickets/new", data={"csrf": token, "parent": p["id"], "project_id": d.proj.id,
                                       "type_id": d.net_type.id, "title": "Desk phone", "description": "Extension"},
                 follow_redirects=False)
    assert r.status_code == 303
    child = f.db.scalar(select(Ticket).where(Ticket.parent_id == p["id"]))
    assert child is not None and child.title == "Desk phone"
    page = rob.get(f"/tickets/{p['id']}").text
    assert "Desk phone" in page and "once its child tickets are finished" in page
    assert f"Child of <a href=\"/tickets/{p['id']}\">" in rob.get(f"/tickets/{child.id}").text
    quinn = portal(d.quinn)
    assert quinn.get(f"/tickets/new?parent={p['id']}").status_code == 403
    assert "Add child ticket" not in quinn.get(f"/tickets/{p['id']}").text
    rob.close()
    quinn.close()
