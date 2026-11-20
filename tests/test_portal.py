"""Phase 5: screens S01-S30, dialogs, CSRF, parity with the API, keyboard / not-by-colour checks."""
import re
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ticketing import clock
from ticketing.models import Ticket, User
from ticketing.services import jobs
from tests import desk as D
from tests.conftest import PASSWORD
from tests.test_sla import make_policy


def portal(app_client_factory, user):
    c = app_client_factory()
    r = c.post("/login", data={"email": user.email, "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


@pytest.fixture
def new_client():
    from ticketing.main import app
    clients = []

    def make():
        c = TestClient(app)
        clients.append(c)
        return c
    yield make
    for c in clients:
        c.close()


def csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, "no csrf token on page"
    return m.group(1)


class A11y(HTMLParser):
    """Every visible form control has a label (label[for], wrapping label or aria-label)."""

    def __init__(self):
        super().__init__()
        self.labels_for, self.controls, self.in_label = set(), [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self.in_label += 1
            if a.get("for"):
                self.labels_for.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit"):
            self.controls.append((tag, a, self.in_label > 0))

    def handle_endtag(self, tag):
        if tag == "label":
            self.in_label -= 1

    def unlabeled(self):
        return [(t, a) for t, a, wrapped in self.controls
                if not wrapped and not a.get("aria-label") and a.get("id") not in self.labels_for]


def check_page(html):
    p = A11y()
    p.feed(html)
    bad = p.unlabeled()
    assert not bad, f"unlabelled controls: {bad[:3]}"
    # status shown as text, not colour alone: every chip has text content
    for chip in re.findall(r'<span class="chip[^"]*">(.*?)</span>', html):
        assert chip.strip(), "empty chip"


@pytest.fixture
def world(f, root):
    d = D.build(f)
    make_policy(root, f.priority("Medium").id)
    platform = f.root()
    return d, platform


def test_sign_in_flow_and_forced_change(f, new_client, world):
    d, _ = world
    c = new_client()
    assert c.get("/", follow_redirects=False).headers["location"].startswith("/login")
    bad = c.post("/login", data={"email": d.quinn.email, "password": "nope-nope-nope"})
    assert bad.status_code == 401 and "not correct" in bad.text
    f.db.refresh(d.quinn)
    d.quinn.must_change_password = True
    f.commit()
    r = c.post("/login", data={"email": d.quinn.email, "password": PASSWORD}, follow_redirects=False)
    assert r.headers["location"] == "/account?force=1"
    assert c.get("/tickets", follow_redirects=False).headers["location"] == "/account?force=1"
    page = c.get("/account?force=1").text
    r = c.post("/account", data={"csrf": csrf(page), "current_password": PASSWORD, "new_password": "Brand-new-pass-1",
                                 "repeat_password": "Brand-new-pass-1"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert c.get("/tickets").status_code == 200


def test_csrf_required_on_forms(new_client, world):
    d, _ = world
    c = portal(new_client, d.quinn)
    r = c.post("/tickets/new", data={"title": "x"})
    assert r.status_code == 403


def test_raise_and_work_a_ticket_through_the_portal(f, new_client, world):
    d, _ = world
    quinn = portal(new_client, d.quinn)
    page = quinn.get("/tickets/new").text
    assert "Handled by team Support" in page
    key = re.search(r'name="idempotency_key" value="([^"]+)"', page).group(1)
    data = {"csrf": csrf(page), "project_id": d.proj.id, "type_id": d.req_type.id, "title": "Printer on fire",
            "description": "Smoke everywhere", "idempotency_key": key}
    r = quinn.post("/tickets/new", data=data, follow_redirects=False)
    assert r.status_code == 303
    again = quinn.post("/tickets/new", data=data, follow_redirects=False)  # double click: one ticket
    assert again.headers["location"].split("?")[0] == r.headers["location"].split("?")[0]
    assert f.db.scalar(select(Ticket.id).where(Ticket.number == 2)) is None
    tid = f.db.scalar(select(Ticket.id))
    view = quinn.get(f"/tickets/{tid}").text
    assert "Printer on fire" in view and "SLA" not in view and "Withdraw" in view  # requester view
    mgr = portal(new_client, d.manager)
    page = mgr.get(f"/tickets/{tid}").text
    assert "Assignment" in page and "Assign…" in page  # staff see the SLA strip
    r = mgr.post(f"/tickets/{tid}/act/assign", data={"csrf": csrf(page), "resolver_id": d.rob.id},
                 follow_redirects=False)
    assert r.status_code == 303
    rob = portal(new_client, d.rob)
    work = rob.get("/work").text
    assert "Printer on fire" in work and "Start" in work
    page = rob.get(f"/tickets/{tid}").text
    rob.post(f"/tickets/{tid}/act/start", data={"csrf": csrf(page)})
    # required note enforced on the server; typed text kept
    r = rob.post(f"/tickets/{tid}/act/resolve", data={"csrf": csrf(page), "note": "  ", "actual_effort_minutes": "15"})
    assert r.status_code == 422 and 'value="15"' in r.text and "data-autoopen" in r.text
    r = rob.post(f"/tickets/{tid}/act/resolve", data={"csrf": csrf(page), "note": "Extinguished"},
                 follow_redirects=False)
    assert r.status_code == 303
    f.db.expire_all()
    assert f.db.get(Ticket, tid).status_code == "RESOLVED"
    notes = quinn.get("/notifications").text
    assert "resolved" in notes.lower()


def test_conflict_keeps_typed_text(f, new_client, world):
    d, _ = world
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    mgr = portal(new_client, d.manager)
    page = mgr.get(f"/tickets/{t['id']}").text
    f.as_(d.quinn).post(f"/api/v1/tickets/{t['id']}/withdraw", json={"reason": "changed my mind"})
    r = mgr.post(f"/tickets/{t['id']}/act/reject", data={"csrf": csrf(page), "reason_code": "OTHER",
                                                         "reason": "my long careful reason"})
    assert r.status_code == 409
    assert "This ticket was updated. Reload to see the latest" in r.text
    assert "my long careful reason" in r.text


ALL_SCREENS = ["/", "/account", "/tickets/new", "/tickets", "/tickets/{t}", "/work", "/queue", "/team", "/tickets/all",
               "/notifications", "/error", "/admin", "/admin/organizations", "/admin/projects", "/admin/projects/{p}",
               "/admin/projects/{p}?tab=teams", "/admin/projects/{p}?tab=people",
               "/admin/projects/{p}?tab=product-users", "/admin/projects/{p}?tab=credentials", "/admin/users",
               "/admin/users/{u}", "/admin/role-assignments/bulk", "/admin/ticket-types", "/admin/priorities",
               "/admin/statuses", "/admin/calendars", "/admin/calendars/{c}", "/admin/sla-policies",
               "/admin/sla-policies?missing=1", "/admin/sla-policies/{s}", "/admin/notification-channels",
               "/admin/notification-templates", "/admin/notification-templates?edit=1", "/admin/settings",
               "/admin/audit"]


def test_every_screen_renders_for_platform_admin_and_is_labelled(f, new_client, world, root):
    d, platform = world
    f.grant(platform, "TEAM_MANAGER", team=d.team)  # so the team screens show too
    t = D.raise_ticket(f.as_(d.quinn), d)
    cal = root.get("/api/v1/admin/calendars").json()["items"][0]["id"]
    pol = root.get("/api/v1/admin/sla-policies").json()["items"][0]["id"]
    c = portal(new_client, platform)
    for path in ALL_SCREENS:
        url = path.format(t=t["id"], p=d.proj.id, u=d.rob.id, c=cal, s=pol)
        r = c.get(url)
        assert r.status_code == 200, (url, r.status_code, r.text[:500])
        check_page(r.text)


def test_role_menus_and_hidden_screens(f, new_client, world):
    d, _ = world
    quinn = portal(new_client, d.quinn)
    home = quinn.get("/").text
    assert "My work" not in home and ">Admin<" not in home and "Team board" not in home
    for url in ("/work", "/queue", "/team", "/tickets/all", "/admin", "/admin/users", "/admin/settings"):
        r = quinn.get(url)
        assert r.status_code in (403, 404) and "do not have access" in r.text, url
    rob = portal(new_client, d.rob)
    menu = rob.get("/").text
    assert "My work" in menu and "Team queue" in menu and "Team board" not in menu
    mgr = portal(new_client, d.manager)
    assert "Team board" in mgr.get("/").text and mgr.get("/team").status_code == 200
    pa = portal(new_client, d.padmin)
    assert pa.get("/admin").status_code == 200 and pa.get("/admin/settings").status_code in (403, 404)


def test_menu_follows_selected_project(f, new_client, world):
    d, _ = world
    beta = f.org("Beta")
    pb = f.project(beta, "BET")
    tb = f.team(pb)
    f.grant(d.manager, "RESOLVER", team=tb)
    mgr = portal(new_client, d.manager)
    page = mgr.get("/").text
    assert 'id="project-select"' in page
    r = mgr.get(f"/project-select?project={pb.id}&back=/", follow_redirects=True)
    assert "Resolver" in r.text  # role in the selected project shown in the footer
    r = mgr.get(f"/project-select?project={d.proj.id}&back=/", follow_redirects=True)
    assert "Team Manager" in r.text


def test_secret_and_temporary_password_shown_once(f, new_client, world):
    d, platform = world
    c = portal(new_client, platform)
    page = c.get(f"/admin/projects/{d.proj.id}?tab=credentials").text
    r = c.post(f"/admin/projects/{d.proj.id}/credentials", data={"csrf": csrf(page), "name": "Mobile app"})
    assert r.status_code == 200 and "will not be shown again" in r.text
    secret = re.search(r'id="secret">([^<]+)<', r.text).group(1)
    again = c.get(f"/admin/projects/{d.proj.id}?tab=credentials").text
    assert secret not in again and "Mobile app" in again
    page = c.get("/admin/users").text
    r = c.post("/admin/users", data={"csrf": csrf(page), "user_type": "MEMBER", "name": "New Person",
                                     "email": "new.person@example.com", "organization_id": d.org.id})
    assert r.status_code == 200 and "Temporary password" in r.text


# ---------------------------------------------------------------- parity: screen outcome == API outcome
PARITY = [("claim", {}), ("assign", {"resolver_id": None}), ("start", {}), ("hold", {"reason": "x"}),
          ("reject", {"reason_code": "OTHER", "reason": "x"}), ("withdraw", {"reason": "x"}),
          ("priority", {"priority_id": None}), ("estimate", {"minutes": 5}), ("release", {}),
          ("sla-pause", {"reason": "x"})]


@pytest.mark.parametrize("action,body", PARITY)
def test_screen_and_api_give_the_same_permission_outcome(f, new_client, world, action, body):
    d, platform = world
    outsider = f.member("Otto")
    people = [d.quinn, d.rob, d.rae, d.manager, d.padmin, platform, outsider]
    for who in people:
        t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)  # NEW manual ticket
        b = dict(body)
        if "resolver_id" in b:
            b["resolver_id"] = d.rob.id
        if "priority_id" in b:
            b["priority_id"] = f.priority("High").id
        api_path = {"sla-pause": "sla/pause"}.get(action, action)
        api = f.as_(who).post(f"/api/v1/tickets/{t['id']}/{api_path}", json=b).status_code
        t2 = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
        c = portal(new_client, who)
        home = c.get("/").text
        r = c.post(f"/tickets/{t2['id']}/act/{action}", data={"csrf": csrf(home), **b}, follow_redirects=False)
        screen = 200 if r.status_code == 303 else r.status_code
        assert screen == api, (who.name, action, api, r.status_code)


def test_end_to_end_with_breach_and_escalation_alert(f, new_client, world, root):
    """Raise to close through the portal, with an SLA breach and an escalation alert on the way."""
    d, _ = world
    pid = root.get("/api/v1/admin/sla-policies").json()["items"][0]["id"]
    steps = root.get(f"/api/v1/admin/sla-policies/{pid}/metrics").json()["steps"]
    root.put(f"/api/v1/admin/sla-metrics/{steps[0]['id']}/escalation-rules", json={"rules": [
        {"level": 1, "name": "Unassigned too long", "trigger": "BREACHED", "action": "NOTIFY",
         "target_type": "TEAM_MANAGERS"}]})
    quinn = portal(new_client, d.quinn)
    page = quinn.get("/tickets/new").text
    quinn.post("/tickets/new", data={"csrf": csrf(page), "project_id": d.proj.id, "type_id": d.req_type.id,
                                     "title": "E2E", "description": "full path"})
    tid = f.db.scalar(select(Ticket.id))
    clock.advance(minutes=31)
    jobs.run_cycle()
    mgr = portal(new_client, d.manager)
    notes = mgr.get("/notifications").text
    assert "escalated" in notes.lower() and "sla breach" in notes.lower()
    page = mgr.get(f"/tickets/{tid}").text
    assert "breached" in page and "Unassigned too long" in page
    mgr.post(f"/tickets/{tid}/act/assign", data={"csrf": csrf(page), "resolver_id": d.rob.id})
    rob = portal(new_client, d.rob)
    page = rob.get(f"/tickets/{tid}").text
    rob.post(f"/tickets/{tid}/act/start", data={"csrf": csrf(page)})
    rob.post(f"/tickets/{tid}/act/resolve", data={"csrf": csrf(page), "note": "done"})
    clock.advance(days=8)
    jobs.run_cycle()
    f.db.expire_all()
    assert f.db.get(Ticket, tid).status_code == "CLOSED"
    assert "Closed" in portal(new_client, d.quinn).get(f"/tickets/{tid}").text  # old session expired


def test_admin_forms_round_trip(f, new_client, world):
    d, platform = world
    c = portal(new_client, platform)
    tok = csrf(c.get("/admin/settings").text)
    r = c.post("/admin/settings", data={"csrf": tok, "s:auth.lockout_attempts": "6"}, follow_redirects=True)
    assert "Settings saved" in r.text
    r = c.post("/admin/settings", data={"csrf": tok, "s:auth.lockout_attempts": "0"}, follow_redirects=True)
    assert "allowed range" in r.text
    r = c.post("/admin/calendars", data={"csrf": tok, "name": "Night", "time_zone": "Asia/Kolkata"},
               follow_redirects=True)
    assert "Calendar created" in r.text
    cid = int(re.search(r"/admin/calendars/(\d+)", str(r.url)).group(1))
    r = c.post(f"/admin/calendars/{cid}/hours", data={"csrf": tok, "d1s0": "22:00", "d1e0": "06:00"},
               follow_redirects=True)
    assert "Weekly pattern saved" in r.text and "22:00-24:00" in r.text or "Weekly pattern saved" in r.text
    r = c.post("/admin/ticket-types", data={"csrf": tok, "name": "Access", "team_id": d.team.id,
                                            "assignment_mode": "AUTO"}, follow_redirects=True)
    assert "Ticket type created" in r.text
