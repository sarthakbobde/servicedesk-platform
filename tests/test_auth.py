"""F1 accounts and sign-in."""
from datetime import timedelta

from ticketing import clock
from tests.conftest import PASSWORD


def login(client, email, password=PASSWORD):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_login_sets_session_and_me_works(f, client):
    u = f.member("Ann")
    r = login(client, u.email)
    assert r.status_code == 200
    assert r.json()["must_change_password"] is False
    me = client.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == u.email
    assert "ticket.raise" in me.json()["capabilities"]


def test_one_message_for_unknown_email_and_wrong_password(f, client):
    u = f.member("Ann")
    a = login(client, "nobody@example.com")
    b = login(client, u.email, "wrong-password-1")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()
    assert a.json()["error"]["code"] == "login_failed"


def test_lockout_after_five_failures_and_reset_on_success(f, client):
    u = f.member("Ann")
    for _ in range(4):
        login(client, u.email, "bad-password-xx")
    assert login(client, u.email).status_code == 200  # success resets the count
    for _ in range(5):
        login(client, u.email, "bad-password-xx")
    r = login(client, u.email)  # correct password, but locked
    assert r.status_code == 401 and r.json()["error"]["code"] == "login_failed"
    clock.advance(minutes=16)
    assert login(client, u.email).status_code == 200


def test_forced_password_change_gate(f, client):
    u = f.member("Ann")
    f.db.refresh(u)
    u.must_change_password = True
    f.commit()
    r = login(client, u.email)
    assert r.json()["must_change_password"] is True
    csrf = r.json()["csrf_token"]
    assert client.get("/api/v1/admin/users").json()["error"]["code"] == "password_change_required"
    assert client.get("/api/v1/me").status_code == 200
    bad = client.post("/api/v1/auth/change-password", json={"current_password": PASSWORD, "new_password": "short"},
                      headers={"X-CSRF-Token": csrf})
    assert bad.status_code == 422
    same = client.post("/api/v1/auth/change-password",
                       json={"current_password": PASSWORD, "new_password": PASSWORD}, headers={"X-CSRF-Token": csrf})
    assert same.json()["error"]["fields"]["new_password"].startswith("must differ")
    email_pw = client.post("/api/v1/auth/change-password",
                           json={"current_password": PASSWORD, "new_password": u.email},
                           headers={"X-CSRF-Token": csrf})
    assert email_pw.status_code == 422
    ok = client.post("/api/v1/auth/change-password",
                     json={"current_password": PASSWORD, "new_password": "A-much-better-pass"},
                     headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 204
    assert client.get("/api/v1/tickets/counts").status_code in (200, 404)  # gate lifted


def test_cookie_writes_need_csrf(f, client):
    u = f.member("Ann")
    login(client, u.email)
    r = client.post("/api/v1/auth/logout")
    assert r.status_code == 403


def test_change_password_ends_other_sessions(f, client):
    u = f.member("Ann")
    other = f.as_(u)  # a token session
    r = login(client, u.email)
    client.post("/api/v1/auth/change-password",
                json={"current_password": PASSWORD, "new_password": "Another-good-pass"},
                headers={"X-CSRF-Token": r.json()["csrf_token"]})
    assert other.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me").status_code == 200  # the current session stays


def test_reset_password_by_admin(f, client, root):
    u = f.member("Ann")
    token_caller = f.as_(u)
    r = root.post(f"/api/v1/admin/users/{u.id}/reset-password")
    assert r.status_code == 200
    temp = r.json()["temporary_password"]
    assert login(client, u.email).status_code == 401  # old password stops at once
    assert token_caller.get("/api/v1/me").status_code == 401  # sessions end
    r = login(client, u.email, temp)
    assert r.status_code == 200 and r.json()["must_change_password"] is True


def test_token_and_deactivation(f, client, root):
    u = f.member("Ann")
    caller = f.as_(u)
    assert caller.get("/api/v1/me").status_code == 200
    assert root.patch(f"/api/v1/admin/users/{u.id}", json={"is_active": False}).status_code == 200
    assert caller.get("/api/v1/me").status_code == 401
    assert login(client, u.email).status_code == 401


def test_token_lifetime(f):
    u = f.member("Ann")
    caller = f.as_(u)
    clock.advance(minutes=61)
    assert caller.get("/api/v1/me").status_code == 401


def test_session_idle_and_absolute_limits(f, client):
    u = f.member("Ann")
    login(client, u.email)
    clock.advance(minutes=29)
    assert client.get("/api/v1/me").status_code == 200
    clock.advance(minutes=31)
    assert client.get("/api/v1/me").status_code == 401
    login(client, u.email)
    for _ in range(25):  # stay active for 12.5 hours
        clock.advance(minutes=29)
        last = client.get("/api/v1/me").status_code
    assert last == 401


def test_product_user_token_forbidden_and_portal_gate(f, client, root):
    org = f.org("Acme")
    proj = f.project(org, "ACM")
    pu = f.product_user(proj, "ext-1", email="pu@acme.test", password=True)
    r = client.post("/api/v1/auth/token", json={"email": pu.email, "password": PASSWORD})
    assert r.status_code == 401  # portal access is off by default, so the account cannot sign in at all
    root.put(f"/api/v1/admin/projects/{proj.id}/settings", json={"product_user.portal_access": True})
    r = client.post("/api/v1/auth/token", json={"email": pu.email, "password": PASSWORD})
    assert r.status_code == 403
    assert login(client, pu.email).status_code == 200


def test_rate_limit(f, client):
    for _ in range(20):
        login(client, "x@example.com", "nope-nope-nope")
    r = login(client, "x@example.com", "nope-nope-nope")
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


def test_health(client):
    assert client.get("/api/v1/health").json() == {"status": "ok", "database": "ok"}
