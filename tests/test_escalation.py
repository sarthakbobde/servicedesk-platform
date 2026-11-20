"""Phase 4: escalation rules, notification channels and the outbox, templates, dashboard and system status."""
import httpx
import pytest
from sqlalchemy import select

from ticketing import clock
from ticketing.models import EscalationFiring, Notification, NotificationOutbox, Ticket, TicketEvent, User
from ticketing.services import channels, jobs
from tests import desk as D
from tests.test_sla import STEPS, make_policy


@pytest.fixture
def d(f, root):
    desk = D.build(f)
    pid = make_policy(root, f.priority("Medium").id)
    desk.policy_id = pid
    desk.steps = {s["name"]: s["id"] for s in root.get(f"/api/v1/admin/sla-policies/{pid}/metrics").json()["steps"]}
    return desk


def rules(root, metric_id, items):
    r = root.put(f"/api/v1/admin/sla-metrics/{metric_id}/escalation-rules", json={"rules": items})
    assert r.status_code == 200, r.text
    return r.json()["rules"]


def events(f, tid, type_="escalation_fired"):
    f.db.expire_all()
    return list(f.db.scalars(select(TicketEvent).where(TicketEvent.ticket_id == tid, TicketEvent.type == type_)))


def alerts(f, user, ntype):
    f.db.expire_all()
    return list(f.db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.type == ntype)))


def test_notify_fires_once_per_level_and_names_rule(f, d, root):
    rules(root, d.steps["Assignment"], [
        {"level": 1, "name": "Queue half used", "trigger": "PERCENT_USED", "threshold": 50, "action": "NOTIFY",
         "target_type": "TEAM_MANAGERS"},
        {"level": 2, "name": "Assignment breached", "trigger": "BREACHED", "action": "NOTIFY",
         "target_type": "PROJECT_ADMINS"}])
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    clock.advance(minutes=14)
    jobs.run_cycle()
    assert events(f, t["id"]) == []
    clock.advance(minutes=2)  # 16 of 30 minutes used
    jobs.run_cycle()
    jobs.run_cycle()
    fired = events(f, t["id"])
    assert len(fired) == 1 and fired[0].note == "Queue half used" and fired[0].rule_id
    a = alerts(f, d.manager, "ESCALATION")
    assert len(a) == 1 and a[0].rule_id == fired[0].rule_id
    clock.advance(minutes=15)
    jobs.run_cycle()
    assert len(events(f, t["id"])) == 2
    assert alerts(f, d.padmin, "ESCALATION")
    assert alerts(f, d.manager, "SLA_BREACH")  # unassigned: breach alert goes to the team managers
    hist = f.as_(d.manager).get(f"/api/v1/tickets/{t['id']}/events").json()["items"]
    esc = [e for e in hist if e["type"] == "escalation_fired"]
    assert esc[0]["rule"]["name"] == "Assignment breached"
    req_hist = f.as_(d.quinn).get(f"/api/v1/tickets/{t['id']}/events").json()["items"]
    assert all(e["type"] != "escalation_fired" for e in req_hist)


def test_reopen_starts_a_new_cycle(f, d, root):
    rules(root, d.steps["Resolution"], [{"level": 1, "trigger": "PERCENT_USED", "threshold": 1, "action": "NOTIFY",
                                         "target_type": "REQUESTER"}])
    t = D.raise_ticket(f.as_(d.quinn), d)
    who = f.as_(f.db.get(User, t["resolver"]["id"]))
    clock.advance(minutes=10)
    jobs.run_cycle()
    assert len(events(f, t["id"])) == 1
    who.post(f"/api/v1/tickets/{t['id']}/resolve", json={"note": "ok"})
    f.as_(d.quinn).post(f"/api/v1/tickets/{t['id']}/reopen", json={"reason": "not ok"})
    clock.advance(minutes=10)
    jobs.run_cycle()
    assert len(events(f, t["id"])) == 2
    cycles = {x.cycle for x in f.db.scalars(select(EscalationFiring))}
    assert cycles == {1, 2}


def test_on_hold_trigger_fires_on_paused_timer(f, d, root):
    rules(root, d.steps["Resolution"], [{"level": 1, "trigger": "ON_HOLD_MINUTES", "threshold": 60,
                                         "action": "NOTIFY", "target_type": "TEAM_MANAGERS"}])
    t = D.raise_ticket(f.as_(d.quinn), d)
    who = f.as_(f.db.get(User, t["resolver"]["id"]))
    who.post(f"/api/v1/tickets/{t['id']}/hold", json={"reason": "vendor"})
    clock.advance(minutes=59)
    jobs.run_cycle()
    assert events(f, t["id"]) == []
    clock.advance(minutes=2)
    jobs.run_cycle()
    assert len(events(f, t["id"])) == 1


def test_increase_priority_fires_once_no_loop(f, d, root):
    high = make_policy(root, f.priority("High").id, name="High")
    crit = make_policy(root, f.priority("Critical").id, name="Critical")
    for pid in (d.policy_id, high, crit):
        steps = root.get(f"/api/v1/admin/sla-policies/{pid}/metrics").json()["steps"]
        res = next(s for s in steps if s["name"] == "Resolution")
        rules(root, res["id"], [{"level": 1, "trigger": "PERCENT_USED", "threshold": 1,
                                 "action": "INCREASE_PRIORITY"}])
    t = D.raise_ticket(f.as_(d.quinn), d)
    clock.advance(minutes=30)
    for _ in range(3):
        jobs.run_cycle()
    f.db.expire_all()
    assert f.db.get(Ticket, t["id"]).priority_id == f.priority("High").id  # raised one rank, once
    assert len(events(f, t["id"])) == 1
    assert len(events(f, t["id"], "priority_changed")) == 1


def test_reassign_and_unassigned_minutes(f, d, root):
    pa = make_policy(root, f.priority("Medium").id, type_id=d.req_type.id, name="Requests")
    steps = {s["name"]: s["id"] for s in root.get(f"/api/v1/admin/sla-policies/{pa}/metrics").json()["steps"]}
    rules(root, steps["Assignment"], [{"level": 1, "trigger": "UNASSIGNED_MINUTES", "threshold": 20,
                                       "action": "REASSIGN", "target_type": "USER", "target_id": d.rae.id}])
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    clock.advance(minutes=21)
    jobs.run_cycle()
    f.db.expire_all()
    tk = f.db.get(Ticket, t["id"])
    assert tk.resolver_id == d.rae.id and tk.status_code == "ASSIGNED"
    assert alerts(f, d.rae, "ASSIGNMENT")


def test_minutes_after_breach(f, d, root):
    rules(root, d.steps["Assignment"], [{"level": 1, "trigger": "MINUTES_AFTER_BREACH", "threshold": 10,
                                         "action": "NOTIFY", "target_type": "PROJECT_ADMINS"}])
    t = D.raise_ticket(f.as_(d.quinn), d, d.req_type)
    clock.advance(minutes=35)
    jobs.run_cycle()
    assert events(f, t["id"]) == []
    clock.advance(minutes=6)
    jobs.run_cycle()
    assert len(events(f, t["id"])) == 1


def test_rule_validation(f, d, root):
    mid = d.steps["Assignment"]
    bad = root.put(f"/api/v1/admin/sla-metrics/{mid}/escalation-rules", json={"rules": [
        {"level": 1, "trigger": "BREACHED", "action": "REASSIGN", "target_type": "TEAM_MANAGERS"},
        {"level": 1, "trigger": "BREACHED", "action": "NOTIFY", "target_type": "USER", "target_id": d.rob.id},
        {"level": 2, "trigger": "PERCENT_USED", "threshold": 0, "action": "INCREASE_PRIORITY",
         "target_type": "REQUESTER"},
        {"level": 3, "trigger": "SOMETIMES", "action": "NOTIFY", "target_type": "REQUESTER"}]})
    assert bad.status_code == 422
    fields = bad.json()["error"]["fields"]
    assert "USER" in fields["rules.0.target_type"]
    assert "one rule per step and level" in fields["rules.1.level"]
    assert "default policies" in fields["rules.1.target_type"]
    assert "no target" in fields["rules.2.target_type"] and "rules.2.threshold" in fields
    assert "rules.3.trigger" in fields
    tp = make_policy(root, f.priority("Low").id, type_id=d.bug.id, name="Bugs low")
    m = root.get(f"/api/v1/admin/sla-policies/{tp}/metrics").json()["steps"][0]["id"]
    outsider = f.member("Ollie")
    r = root.put(f"/api/v1/admin/sla-metrics/{m}/escalation-rules", json={"rules": [
        {"level": 1, "trigger": "BREACHED", "action": "REASSIGN", "target_type": "USER", "target_id": outsider.id}]})
    assert r.status_code == 422
    r = root.put(f"/api/v1/admin/sla-metrics/{m}/escalation-rules", json={"rules": [
        {"level": 1, "trigger": "BREACHED", "action": "REASSIGN", "target_type": "USER", "target_id": d.rae.id},
        {"level": 2, "trigger": "BREACHED", "action": "NOTIFY", "target_type": "TEAM", "target_id": d.team.id}]})
    assert r.status_code == 200 and len(r.json()["rules"]) == 2
    assert f.as_(d.padmin).get(f"/api/v1/admin/sla-metrics/{m}/escalation-rules").status_code == 200
    assert f.as_(d.padmin).get(f"/api/v1/admin/sla-metrics/{mid}/escalation-rules").status_code == 404


# ---------------------------------------------------------------- channels, outbox, templates
class FakeSMTP:
    sent: list = []
    fail = False

    def __init__(self, host, port, timeout=10):
        if FakeSMTP.fail:
            raise OSError("connection refused")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


@pytest.fixture
def mail(monkeypatch, root):
    FakeSMTP.sent, FakeSMTP.fail = [], False
    monkeypatch.setattr(channels.EmailChannel, "smtp_factory", FakeSMTP)
    r = root.post("/api/v1/admin/notification-channels", json={
        "type": "EMAIL", "name": "Mail", "settings": {"host": "smtp.test", "from_address": "desk@acme.test",
                                                      "password_env": "SMTP_PASSWORD"}})
    assert r.status_code == 201, r.text
    ch = r.json()
    tpl = root.post("/api/v1/admin/notification-templates", json={
        "notification_type": "ACKNOWLEDGEMENT", "channel_id": ch["id"], "subject": "We got {ticket_number}",
        "body": "Hello, {title} is {status}. {link}"})
    assert tpl.status_code == 201, tpl.text
    return ch


def test_channel_settings_never_hold_secrets(root):
    r = root.post("/api/v1/admin/notification-channels", json={
        "type": "EMAIL", "name": "Bad", "settings": {"host": "h", "from_address": "a@b.test", "password": "x"}})
    assert r.status_code == 422 and "environment variable" in r.text
    r = root.post("/api/v1/admin/notification-channels", json={"type": "IN_APP", "name": "Another"})
    assert r.status_code == 422
    inapp = root.get("/api/v1/admin/notification-channels").json()["items"][0]
    assert root.patch(f"/api/v1/admin/notification-channels/{inapp['id']}", json={"enabled": False}).status_code == 422


def test_email_delivered_after_commit(f, d, mail):
    t = D.raise_ticket(f.as_(d.quinn), d)
    ob = f.db.scalar(select(NotificationOutbox))
    assert ob.status == "PENDING" and "ACM-1" in ob.subject
    jobs.run_cycle()
    assert len(FakeSMTP.sent) == 1 and FakeSMTP.sent[0]["To"] == d.quinn.email
    assert "Login broken" in FakeSMTP.sent[0].get_content()
    f.db.expire_all()
    assert f.db.get(NotificationOutbox, ob.id).status == "SENT"


def test_failed_email_retried_never_blocks_ticket(f, d, mail, root):
    FakeSMTP.fail = True
    t = D.raise_ticket(f.as_(d.quinn), d)
    assert t["status"]["code"] == "ASSIGNED"  # the ticket is unaffected
    for i in range(5):
        jobs.run_cycle()
        clock.advance(minutes=20)
    f.db.expire_all()
    ob = f.db.scalar(select(NotificationOutbox))
    assert ob.status == "FAILED" and ob.attempts == 5 and "connection refused" in ob.last_error
    status = root.get("/api/v1/admin/system/status").json()
    assert status["failed_sends"]["count"] == 1
    chans = {c["name"]: c for c in status["channels"]}
    assert chans["Mail"]["last_failure_text"]
    assert f.as_(d.quinn).get(f"/api/v1/tickets/{t['id']}").json()["status"]["code"] == "ASSIGNED"
    assert root.post(f"/api/v1/admin/notification-channels/{mail['id']}/test").json()["ok"] is False
    FakeSMTP.fail = False
    assert root.post(f"/api/v1/admin/notification-channels/{mail['id']}/test").json() == {"ok": True, "detail": None}


def test_chat_webhook_down_does_not_stop_in_app(f, d, root, monkeypatch):
    monkeypatch.setenv("CHAT_URL", "https://chat.test/hook")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    monkeypatch.setattr(channels.ChatWebhookChannel, "transport", httpx.MockTransport(handler))
    ch = root.post("/api/v1/admin/notification-channels", json={"type": "CHAT_WEBHOOK", "name": "Chat",
                                                                "settings": {"url_env": "CHAT_URL"}}).json()
    root.post("/api/v1/admin/notification-templates", json={"notification_type": "ASSIGNMENT", "channel_id": ch["id"],
                                                            "body": "{ticket_number} -> {resolver}"})
    D.raise_ticket(f.as_(d.quinn), d)
    jobs.run_cycle()
    assert calls and f.db.scalar(select(NotificationOutbox)).last_error == "webhook answered HTTP 503"
    assigned = f.db.scalar(select(Ticket)).resolver_id
    assert alerts(f, f.db.get(User, assigned), "ASSIGNMENT")  # in-app still delivered


def test_templates_placeholders_preview_and_in_app_always_on(root, mail):
    bad = root.post("/api/v1/admin/notification-templates", json={
        "notification_type": "RESOLUTION", "channel_id": mail["id"], "body": "Done {ticket_numbr}"})
    assert bad.status_code == 422 and "ticket_numbr" in bad.text
    items = root.get("/api/v1/admin/notification-templates").json()
    ack = next(t for t in items["items"] if t["notification_type"] == "ACKNOWLEDGEMENT" and t["channel_id"] == mail["id"])
    pv = root.post(f"/api/v1/admin/notification-templates/{ack['id']}/preview").json()
    assert pv["subject"] == "We got ACM-42" and "Cannot sign in" in pv["body"]
    inapp = next(t for t in items["items"] if t["channel_id"] != mail["id"])
    assert root.patch(f"/api/v1/admin/notification-templates/{inapp['id']}", json={"enabled": False}).status_code == 422
    r = root.patch(f"/api/v1/admin/notification-templates/{inapp['id']}", json={"body": "New text {title}"})
    assert r.status_code == 200 and r.json()["body"] == "New text {title}"
    assert root.post("/api/v1/admin/notification-templates", json={
        "notification_type": "ACKNOWLEDGEMENT", "channel_id": mail["id"], "body": "dup"}).status_code == 422


# ---------------------------------------------------------------- dashboard and system status
def test_dashboard_scoped_counts(f, d, root):
    t1 = D.raise_ticket(f.as_(d.quinn), d, d.req_type, title="waiting")
    D.raise_ticket(f.as_(d.quinn), d, title="auto")
    beta = f.org("Beta")
    pb = f.project(beta, "BET")
    tb = f.team(pb)
    tyb = f.ticket_type(pb, tb, "Bug")
    bq = f.member("Bea", org=beta)
    f.as_(bq).post("/api/v1/tickets", json={"project_id": pb.id, "type_id": tyb.id, "title": "b", "description": "c"})
    clock.advance(minutes=25)
    jobs.run_cycle()  # warning at 80% of the 30 minute assignment step
    dash = f.as_(d.padmin).get("/api/v1/admin/dashboard").json()
    assert {p["key"] for p in dash["by_project"]} == {"ACM"}
    assert dash["oldest_unassigned"]["items"][0]["title"] == "waiting"
    assert dash["at_risk"]["count"] == 1
    clock.advance(minutes=10)
    jobs.run_cycle()
    dash = f.as_(d.padmin).get("/api/v1/admin/dashboard").json()
    assert dash["overdue"]["count"] == 1 and dash["overdue"]["items"][0]["id"] == t1["id"]
    everything = root.get("/api/v1/admin/dashboard").json()
    assert {p["key"] for p in everything["by_project"]} == {"ACM", "BET"}
    assert sum(s["count"] for s in everything["by_status"]) == 3
    assert f.as_(d.manager).get("/api/v1/admin/dashboard").status_code == 403
    assert alerts(f, d.manager, "SLA_WARNING")


def test_system_status(f, d, root):
    jobs.run_cycle()
    s = root.get("/api/v1/admin/system/status").json()
    assert s["worker_last_run"] and s["job_backlog"] == 0
    kinds = {w["kind"] for w in s["warnings"]}
    assert "missing_sla_policy" in kinds  # Low, High, Critical have no policy
    assert f.as_(d.padmin).get("/api/v1/admin/system/status").status_code == 403


PHASE4 = [("GET", "/api/v1/admin/sla-metrics/{m}/escalation-rules"),
          ("PUT", "/api/v1/admin/sla-metrics/{m}/escalation-rules"),
          ("GET", "/api/v1/admin/notification-channels"), ("POST", "/api/v1/admin/notification-channels"),
          ("GET", "/api/v1/admin/notification-channels/{c}"), ("PATCH", "/api/v1/admin/notification-channels/{c}"),
          ("POST", "/api/v1/admin/notification-channels/{c}/test"),
          ("GET", "/api/v1/admin/notification-templates"), ("POST", "/api/v1/admin/notification-templates"),
          ("PATCH", "/api/v1/admin/notification-templates/{t}"),
          ("POST", "/api/v1/admin/notification-templates/{t}/preview"),
          ("GET", "/api/v1/admin/dashboard"), ("GET", "/api/v1/admin/system/status")]


def test_phase4_endpoint_count():
    assert len(PHASE4) == 13


@pytest.mark.parametrize("method,path", PHASE4)
def test_phase4_role_denial(f, d, client, root, method, path):
    ch = root.get("/api/v1/admin/notification-channels").json()["items"][0]["id"]
    tpl = root.get("/api/v1/admin/notification-templates").json()["items"][0]["id"]
    url = path.format(m=d.steps["Assignment"], c=ch, t=tpl)
    assert client.request(method, url, json={}).status_code == 401
    who_list = [d.quinn, d.rob, d.manager, f.member("Outsider")]
    if "dashboard" not in path and "sla-metrics" not in path:
        who_list.append(d.padmin)  # platform-only
    for who in who_list:
        r = f.as_(who).client.request(method, url, json={"rules": []}, headers=f.headers(who))
        assert r.status_code in (403, 404), (who.name, method, path, r.status_code, r.text)
