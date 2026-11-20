"""F9 product-user channel through the API (project credential + X-External-User)."""
import pytest

from tests import desk as D
from tests.conftest import Caller


@pytest.fixture
def product(f, root):
    d = D.build(f)
    cred = root.post(f"/api/v1/admin/projects/{d.proj.id}/credentials", json={"name": "app"}).json()
    base = {"X-Project-Key": cred["key_prefix"], "X-Project-Secret": cred["secret"]}
    return d, cred, base


def as_product_user(client, base, ext):
    return Caller(client, {**base, "X-External-User": ext})


def test_register_raise_follow_and_isolation(f, client, product, root):
    d, cred, base = product
    r = client.put("/api/v1/product-users/cust-1", json={"name": "Cora", "email": "cora@acme.test"}, headers=base)
    assert r.status_code == 201 and r.json()["portal_access"] is False
    assert client.put("/api/v1/product-users/cust-1", json={"name": "Cora B"}, headers=base).status_code == 200
    assert client.get("/api/v1/product-users/cust-1", headers=base).json()["name"] == "Cora B"
    assert client.get("/api/v1/product-users/nobody", headers=base).status_code == 404
    client.put("/api/v1/product-users/cust-2", json={}, headers=base)
    cora, cid = as_product_user(client, base, "cust-1"), as_product_user(client, base, "cust-2")
    unknown = as_product_user(client, base, "ghost")
    assert unknown.get("/api/v1/me").status_code == 401
    me = cora.get("/api/v1/me").json()
    assert me["project"]["key"] == "ACM" and me["user"]["external_id"] == "cust-1"
    types = cora.get(f"/api/v1/projects/{d.proj.id}/ticket-types").json()["items"]
    assert {t["name"] for t in types} == {"Bug", "Request"} and "assignment_mode" not in types[0]
    assert cora.get("/api/v1/priorities").status_code == 200
    t = cora.post("/api/v1/tickets", json={"type_id": d.bug.id, "title": "App crash", "description": "boom"})
    assert t.status_code == 201, t.text
    t = t.json()
    assert t["source"] == "API" and t["requester"]["name"] == "Cora B"
    assert t["allowed_actions"] == ["withdraw"]
    assert cid.get(f"/api/v1/tickets/{t['id']}").status_code == 404
    assert cid.get("/api/v1/tickets").json()["total"] == 0
    assert cora.get("/api/v1/tickets").json()["total"] == 1
    # staff-only things are refused
    assert cora.post(f"/api/v1/tickets/{t['id']}/claim").status_code in (401, 403)
    assert cora.get("/api/v1/admin/users").status_code in (401, 403)
    # follow the ticket: respond, then withdraw
    resolver = f.as_(f.db.get(__import__("ticketing.models", fromlist=["User"]).User, t["resolver"]["id"]))
    resolver.post(f"/api/v1/tickets/{t['id']}/request-info", json={"question": "Device?"})
    assert cora.get("/api/v1/notifications/unread-count").json()["unread"] == 2
    assert cora.post(f"/api/v1/tickets/{t['id']}/respond", json={"answer": "Pixel 9"}).status_code == 200
    events = cora.get(f"/api/v1/tickets/{t['id']}/events").json()["items"]
    assert all("visibility" not in e for e in events)


def test_priority_choice_setting(f, client, product, root):
    d, cred, base = product
    client.put("/api/v1/product-users/c1", json={}, headers=base)
    c1 = as_product_user(client, base, "c1")
    body = {"type_id": d.bug.id, "title": "x", "description": "y", "priority_id": f.priority("High").id}
    assert c1.post("/api/v1/tickets", json=body).status_code == 403
    assert c1.get("/api/v1/projects").json()["items"][0]["portal_priority_choice"] is False
    root.put(f"/api/v1/admin/projects/{d.proj.id}/settings", json={"product_user.can_set_priority": True})
    assert c1.post("/api/v1/tickets", json=body).json()["priority"]["name"] == "High"


def test_api_access_off_and_bad_secret(f, client, product, root):
    d, cred, base = product
    client.put("/api/v1/product-users/c1", json={}, headers=base)
    assert client.get("/api/v1/me", headers={**base, "X-Project-Secret": "wrong",
                                              "X-External-User": "c1"}).status_code == 401
    root.put(f"/api/v1/admin/projects/{d.proj.id}/settings",
             json={"product_user.portal_access": True, "product_user.api_access": False})
    assert client.get("/api/v1/me", headers={**base, "X-External-User": "c1"}).status_code == 403


def test_product_user_cannot_be_given_password_or_role_via_api(f, client, product):
    d, cred, base = product
    r = client.put("/api/v1/product-users/c9", json={"name": "x", "password": "hunter2hunter2"}, headers=base)
    assert r.status_code == 422
