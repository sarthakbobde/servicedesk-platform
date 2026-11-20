"""allowed_actions equals the permission matrix (A6 x A10.2) for every role and status. The expected table is
written out here independently of the service code."""
import pytest

from ticketing import clock
from ticketing.models import Ticket
from tests import desk as D

STATUS_ACTIONS = {  # A10.2 (SLA actions arrive in Phase 3)
    "NEW": {"claim", "assign", "reject", "withdraw", "priority", "estimate"},
    "ASSIGNED": {"start", "hold", "request-info", "resolve", "reject", "withdraw", "release", "reassign",
                 "priority", "estimate"},
    "IN_PROGRESS": {"hold", "request-info", "resolve", "reject", "release", "reassign", "priority", "estimate"},
    "ON_HOLD": {"resume", "reject", "withdraw", "release", "reassign", "priority", "estimate"},
    "AWAITING_REQUESTER": {"resume", "respond", "reject", "withdraw", "release", "reassign", "priority",
                           "estimate"},
    "RESOLVED": {"reopen"}, "REJECTED": {"reopen"}, "WITHDRAWN": {"reopen"}, "CLOSED": set(),
}
ADMINS = ("manager", "padmin", "oadmin", "platform")
WHO = {  # A6
    "claim": {"own", "other", "manager"},
    "assign": set(ADMINS),
    "start": {"own", "manager"}, "hold": {"own", "manager"}, "request-info": {"own", "manager"},
    "resume": {"own", "manager"}, "resolve": {"own", "manager"},
    "reject": {"own", "other", "manager"},
    "respond": {"requester"}, "withdraw": {"requester"}, "reopen": {"requester"},
    "release": {"own"} | set(ADMINS),
    "reassign": set(ADMINS), "priority": set(ADMINS), "estimate": set(ADMINS),
}
ROLES = ("requester", "own", "other", "manager", "padmin", "oadmin", "platform", "outsider")


def sees(role, status):
    if role == "outsider":
        return False
    if role == "other":
        return status == "NEW"  # another resolver sees only the team queue
    return True


def expected(role, status):
    if not sees(role, status):
        return None
    out = set()
    for a in STATUS_ACTIONS[status]:
        who = WHO[a]
        r = role
        if status == "NEW" and role == "own":
            r = "other"  # nobody holds a NEW ticket; "own" is just a resolver of the team
        if r in who:
            out.add(a)
    return out


@pytest.fixture
def world(f):
    weekend = f.calendar("Off", windows=[])
    d = D.build(f, resolver_calendar=weekend)  # nobody on shift: NEW AUTO tickets wait and are claimable
    oadmin = f.member("Olga Org", org=d.org)
    f.grant(oadmin, "ORG_ADMIN", org=d.org)
    platform = f.member("Pia Platform", platform_admin=True)
    outsider = f.member("Otto Outsider")
    t = D.raise_ticket(f.as_(d.quinn), d)
    people = {"requester": d.quinn, "own": d.rob, "other": d.rae, "manager": d.manager, "padmin": d.padmin,
              "oadmin": oadmin, "platform": platform, "outsider": outsider}
    return d, t["id"], people


@pytest.mark.parametrize("status", list(STATUS_ACTIONS))
def test_allowed_actions_match_matrix(f, world, status):
    d, tid, people = world
    t = f.db.get(Ticket, tid)
    t.status_code = status
    t.resolver_id = None if status == "NEW" else d.rob.id
    t.waiting_for_candidate = status == "NEW"
    t.completed_at = clock.now() if status in ("RESOLVED", "REJECTED", "WITHDRAWN", "CLOSED") else None
    f.commit()
    for role in ROLES:
        r = f.as_(people[role]).get(f"/api/v1/tickets/{tid}")
        exp = expected(role, status)
        if exp is None:
            assert r.status_code == 404, (role, status, r.text)
            continue
        assert r.status_code == 200, (role, status, r.text)
        assert set(r.json()["allowed_actions"]) == exp, (role, status, r.json()["allowed_actions"], exp)


@pytest.mark.parametrize("status", ["ASSIGNED", "IN_PROGRESS", "ON_HOLD"])
def test_disallowed_action_returns_403_or_409(f, world, status):
    """Calling an action that is not offered gives forbidden (wrong role) or invalid_state (wrong status)."""
    d, tid, people = world
    t = f.db.get(Ticket, tid)
    t.status_code, t.resolver_id, t.waiting_for_candidate = status, d.rob.id, False
    f.commit()
    for role in ("requester", "own", "manager", "platform"):
        offered = set(f.as_(people[role]).get(f"/api/v1/tickets/{tid}").json()["allowed_actions"])
        for action in ("start", "resume", "assign", "respond"):
            if action in offered:
                continue
            body = {"resolver_id": d.rae.id, "answer": "x"}
            r = f.as_(people[role]).post(f"/api/v1/tickets/{tid}/{action}", json=body)
            assert r.status_code in (403, 409), (role, status, action, r.status_code)
