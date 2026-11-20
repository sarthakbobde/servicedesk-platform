"""F2 structure and roles, F11 settings, F12 audit, and Phase 1 role-denial on all 35 endpoints."""
import pytest
from sqlalchemy import select

from ticketing.models import AuditLog


def test_org_project_team_flow_with_audit(root, f):
    r = root.post("/api/v1/admin/organizations", json={"name": "Acme", "type": "CLIENT"})
    assert r.status_code == 201
    org_id = r.json()["id"]
    assert root.post("/api/v1/admin/organizations", json={"name": "acme", "type": "CLIENT"}).status_code == 422
    r = root.post("/api/v1/admin/projects", json={"organization_id": org_id, "name": "Portal", "key": "ACM"})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert root.post("/api/v1/admin/projects", json={"organization_id": org_id, "name": "X", "key": "ACM"}).status_code == 422
    assert root.post("/api/v1/admin/projects", json={"organization_id": org_id, "name": "Y", "key": "a"}).status_code == 422
    r = root.post(f"/api/v1/admin/projects/{pid}/teams", json={"name": "Support"})
    assert r.status_code == 201
    tid = r.json()["id"]
    assert root.get(f"/api/v1/admin/teams/{tid}").json()["resolvers"] == []
    assert root.patch(f"/api/v1/admin/teams/{tid}", json={"name": "Help desk"}).json()["name"] == "Help desk"
    assert root.get(f"/api/v1/admin/organizations/{org_id}").json()["projects"][0]["key"] == "ACM"
    entities = {a.entity for a in f.db.scalars(select(AuditLog))}
    assert {"organization", "project", "team"} <= entities


def test_project_key_locked_after_first_ticket(root, f):
    org = f.org("Acme")
    p = f.project(org, "ACM")
    assert root.patch(f"/api/v1/admin/projects/{p.id}", json={"key": "ACX"}).json()["key"] == "ACX"
    f.db.refresh(p)
    p.ticket_seq = 1
    f.commit()
    r = root.patch(f"/api/v1/admin/projects/{p.id}", json={"key": "ACY"})
    assert r.status_code == 422 and "locked" in r.json()["error"]["fields"]["key"]


def test_create_user_shows_temporary_password_once(root, f, client):
    org = f.internal_org()
    r = root.post("/api/v1/admin/users", json={"user_type": "MEMBER", "name": "Bob", "email": "Bob@Example.com",
                                               "organization_id": org.id})
    assert r.status_code == 201
    body = r.json()
    assert body["temporary_password"] and body["must_change_password"] is True
    assert body["email"] == "bob@example.com"
    again = root.get(f"/api/v1/admin/users/{body['id']}").json()
    assert "temporary_password" not in again and "password_hash" not in again
    dup = root.post("/api/v1/admin/users", json={"user_type": "MEMBER", "name": "B2", "email": "bob@example.com",
                                                 "organization_id": org.id})
    assert dup.status_code == 422
    r = client.post("/api/v1/auth/login", json={"email": "bob@example.com", "password": body["temporary_password"]})
    assert r.status_code == 200 and r.json()["must_change_password"]


def test_product_user_account_needs_portal_access(root, f):
    org = f.org("Acme")
    p = f.project(org, "ACM")
    data = {"user_type": "PRODUCT_USER", "name": "Pat", "email": "pat@acme.test", "project_id": p.id,
            "external_id": "u-1"}
    assert root.post("/api/v1/admin/users", json=data).status_code == 422
    root.put(f"/api/v1/admin/projects/{p.id}/settings", json={"product_user.portal_access": True})
    r = root.post("/api/v1/admin/users", json=data)
    assert r.status_code == 201 and r.json()["temporary_password"]


def test_scope_rules_for_role_assignments(root, f):
    acme, beta = f.org("Acme"), f.org("Beta")
    pa, pb = f.project(acme, "ACM"), f.project(beta, "BET")
    ta, tb = f.team(pa), f.team(pb)
    client_staff = f.member("Cara", org=acme)
    internal = f.member("Ivan")
    # client staff only in their own organisation
    r = root.post("/api/v1/admin/role-assignments", json={"user_id": client_staff.id, "role": "RESOLVER",
                                                          "team_id": tb.id})
    assert r.status_code == 422 and "own organisation" in r.text
    assert root.post("/api/v1/admin/role-assignments", json={"user_id": client_staff.id, "role": "RESOLVER",
                                                             "team_id": ta.id}).status_code == 201
    # internal staff may work in client projects
    assert root.post("/api/v1/admin/role-assignments", json={"user_id": internal.id, "role": "RESOLVER",
                                                             "team_id": tb.id}).status_code == 201
    # product users never hold roles
    pu = f.product_user(pa, "x1")
    r = root.post("/api/v1/admin/role-assignments", json={"user_id": pu.id, "role": "RESOLVER", "team_id": ta.id})
    assert r.status_code == 422
    # duplicate refused
    r = root.post("/api/v1/admin/role-assignments", json={"user_id": internal.id, "role": "RESOLVER",
                                                          "team_id": tb.id})
    assert r.status_code == 422
    # scope must match the role
    r = root.post("/api/v1/admin/role-assignments", json={"user_id": internal.id, "role": "PROJECT_ADMIN"})
    assert r.status_code == 422


def test_project_admin_grants_only_in_scope(f):
    acme = f.org("Acme")
    pa, pb = f.project(acme, "ACM"), f.project(acme, "AC2")
    ta, tb = f.team(pa), f.team(pb)
    admin = f.member("Pam", org=acme)
    f.grant(admin, "PROJECT_ADMIN", project=pa)
    worker = f.member("Wes", org=acme)
    me = f.as_(admin)
    assert me.post("/api/v1/admin/role-assignments", json={"user_id": worker.id, "role": "RESOLVER",
                                                           "team_id": ta.id}).status_code == 201
    assert me.post("/api/v1/admin/role-assignments", json={"user_id": worker.id, "role": "RESOLVER",
                                                           "team_id": tb.id}).status_code == 403
    assert me.post("/api/v1/admin/role-assignments", json={"user_id": worker.id, "role": "ORG_ADMIN",
                                                           "organization_id": acme.id}).status_code == 403
    # out-of-scope project is not found, not forbidden
    assert me.get(f"/api/v1/admin/projects/{pb.id}").status_code == 404
    assert me.get(f"/api/v1/admin/projects/{pa.id}").status_code == 200


def test_bulk_add_reports_each_outcome(root, f):
    acme = f.org("Acme")
    p = f.project(acme, "ACM")
    t = f.team(p)
    a, b = f.member("A1"), f.member("B1")
    pu = f.product_user(p, "ext")
    f.grant(b, "RESOLVER", team=t)
    r = root.post("/api/v1/admin/role-assignments/bulk", json={"team_id": t.id, "role": "RESOLVER",
                                                               "user_ids": [a.id, b.id, pu.id]})
    assert r.status_code == 200
    out = r.json()
    assert [x["user_id"] for x in out["created"]] == [a.id]
    assert out["skipped"] == [{"user_id": b.id, "reason": "duplicate"}]
    assert out["refused"][0]["user_id"] == pu.id


def test_shift_calendar_on_assignment_change_is_audited(root, f):
    acme = f.org("Acme")
    p = f.project(acme, "ACM")
    t = f.team(p)
    u = f.member("Una")
    ra = f.grant(u, "RESOLVER", team=t)
    night = f.calendar("Night", windows=[(1, 1320, 360)])
    r = root.patch(f"/api/v1/admin/role-assignments/{ra.id}", json={"calendar_id": night.id})
    assert r.status_code == 200 and r.json()["calendar"]["name"] == "Night"
    row = f.db.scalar(select(AuditLog).where(AuditLog.entity == "role_assignment"))
    assert row.new_value == {"calendar_id": night.id}


def test_team_with_open_tickets_cannot_be_deactivated(root, f):
    from ticketing.models import Ticket
    acme = f.org("Acme")
    p = f.project(acme, "ACM")
    t = f.team(p)
    tt = f.ticket_type(p, t)
    u = f.member("Req", org=acme)
    f.db.add(Ticket(project_id=p.id, number=1, type_id=tt.id, team_id=t.id, priority_id=f.priority("Low").id,
                    status_code="NEW", title="x", description="y", requester_id=u.id, source="API"))
    f.commit()
    r = root.patch(f"/api/v1/admin/teams/{t.id}", json={"is_active": False})
    assert r.status_code == 422 and "open tickets" in r.text


def test_credentials_secret_once_and_revoke(root, f):
    acme = f.org("Acme")
    p = f.project(acme, "ACM")
    r = root.post(f"/api/v1/admin/projects/{p.id}/credentials", json={"name": "Main app"})
    assert r.status_code == 201
    cred = r.json()
    assert cred["secret"] and cred["key_prefix"].startswith("pk_")
    listed = root.get(f"/api/v1/admin/projects/{p.id}/credentials").json()["items"]
    assert "secret" not in listed[0]
    f.product_user(p, "ext-9")
    hdr = {"X-Project-Key": cred["key_prefix"], "X-Project-Secret": cred["secret"], "X-External-User": "ext-9"}
    assert root.client.get("/api/v1/me", headers=hdr).status_code == 200
    assert root.patch(f"/api/v1/admin/credentials/{cred['id']}", json={"is_active": False}).status_code == 200
    assert root.client.get("/api/v1/me", headers=hdr).status_code == 401


def test_settings_precedence_validation_and_audit(root, f):
    acme = f.org("Acme")
    p = f.project(acme, "ACM")
    r = root.put(f"/api/v1/admin/projects/{p.id}/settings", json={"ticket.reopen_window_days": 3,
                                                                    "ticket.auto_close_days": 3})
    assert r.status_code == 200
    eff = {x["key"]: x for x in r.json()["items"]}
    assert eff["ticket.reopen_window_days"]["value"] == 3 and eff["ticket.reopen_window_days"]["source"] == "project"
    # a global change that would make the project's effective values invalid is refused and names the project
    r = root.put("/api/v1/admin/settings", json={"ticket.reopen_window_days": 5})
    assert r.status_code == 200  # project overrides both values, still valid
    r = root.put(f"/api/v1/admin/projects/{p.id}/settings", json={"ticket.auto_close_days": None})
    assert r.status_code == 200  # inherits global 7 >= project 3
    r = root.put("/api/v1/admin/settings", json={"ticket.auto_close_days": 2})
    assert r.status_code == 422 and "ACM" in r.json()["error"]["fields"]
    r = root.put(f"/api/v1/admin/projects/{p.id}/settings", json={"product_user.api_access": False})
    assert r.status_code == 422  # both product-user channels off
    assert root.put("/api/v1/admin/settings", json={"auth.lockout_attempts": 0}).status_code == 422
    assert root.put("/api/v1/admin/settings", json={"no.such": 1}).status_code == 422
    globals_ = {x["key"]: x["value"] for x in root.get("/api/v1/admin/settings").json()["items"]}
    assert globals_["ticket.reopen_window_days"] == 5
    assert f.db.scalar(select(AuditLog).where(AuditLog.entity == "setting", AuditLog.project_id.is_(None)))


def test_audit_scoped_to_project_admin(root, f):
    acme = f.org("Acme")
    pa, pb = f.project(acme, "ACM"), f.project(acme, "AC2")
    root.post(f"/api/v1/admin/projects/{pa.id}/teams", json={"name": "T1"})
    root.post(f"/api/v1/admin/projects/{pb.id}/teams", json={"name": "T2"})
    admin = f.member("Pam", org=acme)
    f.grant(admin, "PROJECT_ADMIN", project=pa)
    rows = f.as_(admin).get("/api/v1/admin/audit").json()["items"]
    assert rows and all(r["project_id"] == pa.id for r in rows)
    assert len(root.get("/api/v1/admin/audit?entity=team").json()["items"]) == 2


# ---------------------------------------------------------------- role denial: all 35 Phase 1 endpoints
PHASE1 = [
    ("POST", "/api/v1/auth/logout"), ("GET", "/api/v1/me"), ("POST", "/api/v1/auth/change-password"),
    ("GET", "/api/v1/admin/organizations"), ("POST", "/api/v1/admin/organizations"),
    ("GET", "/api/v1/admin/organizations/{org}"), ("PATCH", "/api/v1/admin/organizations/{org}"),
    ("GET", "/api/v1/admin/projects"), ("POST", "/api/v1/admin/projects"),
    ("GET", "/api/v1/admin/projects/{proj}"), ("PATCH", "/api/v1/admin/projects/{proj}"),
    ("GET", "/api/v1/admin/settings"), ("PUT", "/api/v1/admin/settings"),
    ("GET", "/api/v1/admin/projects/{proj}/settings"), ("PUT", "/api/v1/admin/projects/{proj}/settings"),
    ("GET", "/api/v1/admin/projects/{proj}/teams"), ("POST", "/api/v1/admin/projects/{proj}/teams"),
    ("GET", "/api/v1/admin/teams/{team}"), ("PATCH", "/api/v1/admin/teams/{team}"),
    ("GET", "/api/v1/admin/users"), ("POST", "/api/v1/admin/users"),
    ("GET", "/api/v1/admin/users/{user}"), ("PATCH", "/api/v1/admin/users/{user}"),
    ("POST", "/api/v1/admin/users/{user}/reset-password"),
    ("GET", "/api/v1/admin/role-assignments"), ("POST", "/api/v1/admin/role-assignments"),
    ("POST", "/api/v1/admin/role-assignments/bulk"), ("PATCH", "/api/v1/admin/role-assignments/{ra}"),
    ("GET", "/api/v1/admin/projects/{proj}/credentials"), ("POST", "/api/v1/admin/projects/{proj}/credentials"),
    ("PATCH", "/api/v1/admin/credentials/{cred}"), ("GET", "/api/v1/admin/audit"),
]
PUBLIC = [("POST", "/api/v1/auth/login"), ("POST", "/api/v1/auth/token"), ("GET", "/api/v1/health")]
SELF_SERVICE = {"/api/v1/auth/logout", "/api/v1/me", "/api/v1/auth/change-password"}


@pytest.fixture
def world(f, root):
    acme = f.org("Acme")
    proj = f.project(acme, "ACM")
    team = f.team(proj)
    target = f.member("Target", org=acme)
    ra = f.grant(target, "RESOLVER", team=team)
    cred = root.post(f"/api/v1/admin/projects/{proj.id}/credentials", json={"name": "c"}).json()
    return {"org": acme.id, "proj": proj.id, "team": team.id, "user": target.id, "ra": ra.id, "cred": cred["id"]}


def test_phase1_endpoint_count():
    assert len(PHASE1) + len(PUBLIC) == 35


@pytest.mark.parametrize("method,path", PHASE1)
def test_phase1_unauthenticated_is_401(client, world, method, path):
    r = client.request(method, path.format(**world), json={})
    assert r.status_code == 401, (method, path, r.text)


@pytest.mark.parametrize("method,path", [x for x in PHASE1 if x[1] not in SELF_SERVICE])
def test_phase1_plain_member_and_resolver_denied(f, world, method, path):
    plain = f.member("Plain")
    resolver = f.db.get(__import__("ticketing.models", fromlist=["User"]).User, world["user"])
    for who in (plain, resolver):
        r = f.as_(who).client.request(method, path.format(**world), json={}, headers=f.headers(who))
        assert r.status_code in (403, 404), (who.name, method, path, r.status_code, r.text)


@pytest.mark.parametrize("method,path", [x for x in PHASE1 if "/settings" in x[1] and "projects" not in x[1]
                                         or "organizations" in x[1] and x[0] != "GET"])
def test_platform_only_endpoints_deny_org_admin(f, world, method, path):
    from ticketing.models import Organization
    oa = f.member("Olga", org=f.db.get(Organization, world["org"]))
    f.grant(oa, "ORG_ADMIN", org=f.db.get(Organization, world["org"]))
    r = f.as_(oa).client.request(method, path.format(**world), json={"name": "Z", "type": "CLIENT"},
                                 headers=f.headers(oa))
    assert r.status_code in (403, 404)
