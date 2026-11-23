"""Demonstration data. Run after the seed on an empty database:

    uv run python -m ticketing.seed && uv run python scripts/demo_data.py

Builds three projects (Internal IT, the Acme web shop and the Northwind fleet app), five teams, people holding
several roles, product users on the portal and the API, ticket types, a night shift, SLA policies with escalation
rules and about forty tickets. Each ticket's history is replayed at back-dated times over the last three weeks
through the normal service functions, so ages, SLA timers, history, notifications and auto-closing look real:
every status appears (new, assigned, in progress, on hold, awaiting the requester, resolved, closed, rejected,
withdrawn, reopened), with priorities, estimates, effort, notes and breaches.

All demo accounts share the password printed at the end (DEMO_PASSWORD overrides it). They are ready to use: no
password change at first sign-in. Never load this into a production database.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import select  # noqa: E402

from ticketing import clock  # noqa: E402
from ticketing.db import SessionLocal  # noqa: E402
from ticketing.errors import AppError  # noqa: E402
from ticketing.models import Organization, Priority, Ticket, TicketType, User  # noqa: E402
from ticketing.permissions import load_principal  # noqa: E402
from ticketing.security import hash_password  # noqa: E402
from ticketing.services import configuration as cfg  # noqa: E402
from ticketing.services import escalation, jobs, sla, structure as st, tickets  # noqa: E402

PASSWORD = os.environ.get("DEMO_PASSWORD", "Demo-password-2026")

# (name, organisation key, user type, project key for product users)
PEOPLE = [
    ("Maya Manager", "internal"), ("Ravi Resolver", "internal"), ("Nina Night", "internal"),
    ("Tom Tech", "internal"), ("Lena Lead", "internal"), ("Omar Ops", "internal"),
    ("Rita Requester", "internal"), ("Ellen Employee", "internal"), ("Victor Finance", "internal"),
    ("Paula Project-Admin", "acme"), ("Carl Client-Staff", "acme"), ("Hana Payments", "acme"),
    ("Greg Fleet-Manager", "northwind"), ("Fatima Fleet", "northwind"), ("Priya Fleet-Admin", "northwind"),
]
PRODUCT_USERS = [("Sam Shopper", "SHOP", "cust-1001"), ("Ben Buyer", "SHOP", "cust-1002"),
                 ("Chloe Customer", "SHOP", "cust-1003"), ("Dana Driver", "FLEET", "drv-2001"),
                 ("Felix Dispatcher", "FLEET", "drv-2002")]

# team key -> (project key, team name, manager first name, resolver first names)
TEAMS = {
    "desk": ("IT", "Service desk", "Maya", ["Ravi", "Tom"]),
    "network": ("IT", "Network and infrastructure", "Lena", ["Omar", "Ravi"]),
    "shop": ("SHOP", "Shop support", "Carl", ["Maya", "Ravi", "Nina"]),
    "payments": ("SHOP", "Payments", "Paula", ["Hana"]),
    "fleet": ("FLEET", "Fleet support", "Greg", ["Fatima", "Tom"]),
}

# type key -> (project key, name, team key, assignment mode)
TYPES = {
    "laptop": ("IT", "Laptop problem", "desk", "MANUAL"),
    "access": ("IT", "Access request", "desk", "AUTO"),
    "software": ("IT", "Software install", "desk", "AUTO"),
    "starter": ("IT", "New starter setup", "desk", "MANUAL"),
    "network": ("IT", "Network or VPN issue", "network", "AUTO"),
    "server": ("IT", "Server alert", "network", "AUTO"),
    "order": ("SHOP", "Order problem", "shop", "AUTO"),
    "account": ("SHOP", "Account and login", "shop", "AUTO"),
    "payment": ("SHOP", "Payment issue", "payments", "AUTO"),
    "refund": ("SHOP", "Refund request", "payments", "MANUAL"),
    "appbug": ("FLEET", "App bug", "fleet", "MANUAL"),
    "tracking": ("FLEET", "Tracking data", "fleet", "AUTO"),
    "question": ("FLEET", "How-to question", "fleet", "AUTO"),
}

# Tickets: (working days ago, "HH:MM" UTC, type, requester, priority or None, source, title, description, steps)
# A step is (minutes after creation, actor, action, body). Actor: "res" (current resolver), "mgr" (the team's
# manager), "req" (the requester) or a first name. Action "take:<Name>" makes <Name> the resolver the way a real
# team would: claim from the queue, a manager's assignment, or a manager's reassignment.
TICKETS = [
    # ---- Internal IT
    (15, "09:20", "laptop", "Rita", "High", "PORTAL", "Laptop will not boot",
     "Black screen with a blinking cursor since this morning. Tried holding the power button.",
     [(25, "mgr", "take:Ravi", None), (40, "res", "start", None),
      (180, "res", "resolve", {"note": "Reseated the SSD and ran a disk check. Boots normally now.",
                               "actual_effort_minutes": 75})]),
    (14, "10:05", "access", "Ellen", "Medium", "PORTAL", "Access to the finance share",
     "Needed for month-end reporting. My manager Victor has approved it.",
     [(10, "Tom", "take:Tom", None), (30, "res", "start", None),
      (90, "res", "resolve", {"note": "Added you to the FIN-Share-RW group. Sign out and in again.",
                              "actual_effort_minutes": 10})]),
    (12, "11:30", "software", "Victor", "Low", "PORTAL", "Install Microsoft Visio",
     "I need Visio for the new process diagrams.",
     [(15, "Tom", "take:Tom", None), (60, "res", "start", None),
      (120, "res", "hold", {"reason": "Waiting for a licence from procurement (PO 7781)."}),
      (2880, "res", "resume", None),
      (2920, "res", "resolve", {"note": "Licence arrived. Visio installed from the company portal.",
                                "actual_effort_minutes": 20})]),
    (10, "09:00", "starter", "Rita", "High", "PORTAL", "New starter: Jonas Berg, starts Monday",
     "Jonas joins Finance on Monday. Needs a laptop, email, Teams and the finance share.",
     [(20, "mgr", "take:Ravi", None), (25, "mgr", "estimate", {"minutes": 180}), (60, "res", "start", None),
      (1500, "res", "resolve", {"note": "Laptop imaged, accounts created, welcome pack on his desk.",
                                "actual_effort_minutes": 200})]),
    (9, "14:10", "network", "Ellen", "Critical", "PORTAL", "VPN drops every 10 minutes",
     "Working from home today. The VPN disconnects about every ten minutes and Teams calls drop.",
     [(5, "Omar", "take:Omar", None), (10, "res", "start", None),
      (30, "res", "request-info", {"question": "Which VPN client version do you have, and are you on Wi-Fi or "
                                               "cable?"}),
      (90, "req", "respond", {"answer": "Version 5.2, home Wi-Fi."}),
      (300, "res", "resolve", {"note": "Pushed client 5.3, which fixes the keep-alive bug. Stable since.",
                               "actual_effort_minutes": 120})]),
    (8, "08:40", "server", "Victor", "Critical", "API", "File server FS02 disk 92% full",
     "Monitoring alert: volume D: on FS02 above 90% for 15 minutes.",
     [(10, "Omar", "take:Omar", None), (15, "res", "start", None),
      (120, "res", "resolve", {"note": "Archived 400 GB of old project folders to cold storage. Now at 61%.",
                               "actual_effort_minutes": 45})]),
    (7, "10:15", "laptop", "Victor", "Medium", "PORTAL", "Keyboard keys sticking",
     "Several keys on my laptop keyboard stick, especially E and R.",
     [(30, "mgr", "take:Ravi", None),
      (45, "res", "reject", {"reason_code": "DUPLICATE",
                             "reason": "Same laptop as the ticket you raised yesterday; we are handling it there."})]),
    (6, "13:00", "access", "Rita", "Low", "PORTAL", "Access to the old CRM",
     "I need read access to the old CRM to look up a customer from 2023.",
     [(60, "req", "withdraw", {"reason": "No longer needed, the data is in the new CRM."})]),
    (5, "09:30", "software", "Ellen", "Medium", "PORTAL", "Adobe Acrobat keeps crashing",
     "Acrobat closes as soon as I open a scanned PDF.",
     [(10, "Tom", "take:Tom", None), (20, "res", "start", None),
      (240, "res", "resolve", {"note": "Repaired the installation and cleared the cache.",
                               "actual_effort_minutes": 30}),
      (2880, "req", "reopen", {"reason": "It is crashing again after yesterday's Windows update."}),
      (2900, "Tom", "take:Tom", None), (2930, "res", "start", None)]),
    (4, "11:00", "starter", "Victor", "High", "PORTAL", "New starter: Aisha Khan, starts 1 December",
     "Aisha joins the finance team on 1 December. Same setup as Jonas, please.",
     [(30, "mgr", "take:Ravi", None), (35, "mgr", "estimate", {"minutes": 240}), (60, "res", "start", None),
      (120, "res", "hold", {"reason": "Laptop on order, delivery expected Thursday."})]),
    (3, "15:20", "network", "Rita", "High", "PORTAL", "Wi-Fi in meeting room 3 very slow",
     "Video calls in meeting room 3 freeze. Other rooms are fine.",
     [(10, "Omar", "take:Omar", None), (20, "res", "start", None),
      (40, "res", "request-info", {"question": "Does it happen on every device, or only on your laptop?"})]),
    (3, "10:00", "software", "Rita", "Medium", "PORTAL", "Need Python 3.12 on my laptop",
     "For the new reporting scripts.",
     [(10, "Tom", "take:Tom", None), (20, "res", "start", None),
      (60, "res", "resolve", {"note": "Installed Python 3.12 and added it to PATH.", "actual_effort_minutes": 15})]),
    (2, "09:10", "laptop", "Ellen", "Medium", "PORTAL", "Second monitor not detected",
     "The dock charges the laptop but the second screen stays black.",
     [(40, "mgr", "take:Ravi", None)]),
    (1, "10:40", "access", "Victor", "Medium", "PORTAL", "Admin rights for build tools",
     "I need local admin rights for one day to install the build tools for the reporting project.",
     [(15, "Tom", "take:Tom", None), (30, "res", "start", None),
      (45, "mgr", "priority", {"priority": "High"})]),
    (1, "14:00", "laptop", "Rita", "Low", "PORTAL", "Request a privacy screen",
     "I work on salary data in open-plan; a privacy filter for a 14-inch laptop, please.", []),
    (0, "08:05", "server", "Ellen", "High", "API", "Backup job failed last night",
     "Backup job NIGHTLY-SQL ended with error 0x80070005 (access denied).", []),
    (0, "09:15", "network", "Victor", "Critical", "PORTAL", "Office internet down on floor 2",
     "Nobody on floor 2 has internet; floor 1 is fine.",
     [(5, "Omar", "take:Omar", None), (8, "res", "start", None)]),

    # ---- Acme web shop (product users raise without choosing a priority)
    (13, "10:30", "order", "Sam", None, "PORTAL", "Order 4410 arrived damaged",
     "The box was crushed and the lamp inside is broken. Photos attached to my email.",
     [(20, "Ravi", "take:Ravi", None), (30, "res", "start", None),
      (150, "res", "resolve", {"note": "A replacement is on its way, tracking AC-77120. No need to return the "
                                       "damaged lamp.", "actual_effort_minutes": 25})]),
    (11, "12:00", "payment", "Ben", None, "API", "Charged twice for order 4502",
     "My card statement shows two charges of 89.90 for the same order.",
     [(10, "mgr", "priority", {"priority": "High"}), (15, "Hana", "take:Hana", None), (30, "res", "start", None),
      (200, "res", "resolve", {"note": "Refunded the duplicate charge. It shows on your statement in 3-5 days.",
                               "actual_effort_minutes": 40})]),
    (9, "09:45", "account", "Chloe", None, "PORTAL", "Cannot reset my password",
     "The reset link says it has expired every time.",
     [(10, "Maya", "take:Maya", None), (15, "res", "start", None),
      (30, "res", "resolve", {"note": "Fixed the link expiry for your account. Try the reset again.",
                              "actual_effort_minutes": 10})]),
    (8, "15:20", "refund", "Sam", None, "PORTAL", "Refund for returned shoes",
     "I returned the shoes from order 4377 two weeks ago.",
     [(30, "mgr", "take:Hana", None), (60, "res", "start", None),
      (70, "res", "request-info", {"question": "Please send the return label number."}),
      (400, "req", "respond", {"answer": "RL-88213"}),
      (460, "res", "resolve", {"note": "Return found and refund issued.", "actual_effort_minutes": 20})]),
    (6, "11:10", "order", "Ben", None, "API", "Wrong size delivered",
     "Got a size M instead of L.",
     [(20, "Maya", "take:Maya", None),
      (30, "res", "reject", {"reason_code": "INFO_MISSING",
                             "reason": "Please raise it again with the order number so we can find it."})]),
    (5, "10:00", "account", "Chloe", None, "PORTAL", "Two-factor code never arrives",
     "I get no text message when I sign in.",
     [(15, "Ravi", "take:Ravi", None), (20, "res", "start", None),
      (40, "res", "request-info", {"question": "Which phone number is on the account? Just the last four digits."})]),
    (4, "13:30", "payment", "Sam", None, "PORTAL", "Card declined at checkout",
     "My card works everywhere else but is declined in your shop.",
     [(15, "Hana", "take:Hana", None), (25, "res", "start", None),
      (90, "res", "hold", {"reason": "Waiting for the payment provider to check their fraud filter."})]),
    (3, "09:20", "order", "Chloe", None, "API", "Order 4788 stuck in 'processing'",
     "It has said 'processing' for four days.",
     [(10, "Ravi", "take:Ravi", None), (25, "res", "start", None)]),
    (2, "11:45", "refund", "Ben", None, "PORTAL", "Refund not received after 10 days",
     "You confirmed the refund for order 4502 but nothing has arrived.",
     [(30, "mgr", "take:Hana", None), (35, "mgr", "priority", {"priority": "High"})]),
    (2, "10:10", "order", "Chloe", None, "PORTAL", "Can I change the delivery address?",
     "I am moving next week.",
     [(45, "req", "withdraw", {"reason": "I changed it myself in my account."})]),
    (1, "15:00", "order", "Sam", None, "PORTAL", "Discount code SPRING not applied",
     "The code is accepted but the total does not change.", []),
    (0, "08:30", "account", "Ben", None, "API", "Account locked after failed sign-ins",
     "I mistyped my password a few times and now the account is locked.", []),

    # ---- Northwind fleet app (product users may choose a priority here)
    (12, "09:00", "appbug", "Dana", "High", "API", "Driver app crashes when scanning a barcode",
     "Version 3.4.1 on Android. Crashes on every parcel scan since this morning's update.",
     [(15, "mgr", "take:Fatima", None), (20, "mgr", "estimate", {"minutes": 480}), (30, "res", "start", None),
      (2400, "res", "resolve", {"note": "Fixed in 3.4.2, released to all drivers.",
                                "actual_effort_minutes": 420})]),
    (10, "14:00", "tracking", "Felix", "Medium", "PORTAL", "GPS positions delayed by 15 minutes",
     "The live map shows vans where they were a quarter of an hour ago.",
     [(10, "Tom", "take:Tom", None), (20, "res", "start", None),
      (300, "res", "resolve", {"note": "A stuck message queue consumer was restarted; positions are live again.",
                               "actual_effort_minutes": 90})]),
    (7, "09:30", "question", "Dana", "Low", "PORTAL", "How do I export the monthly mileage report?",
     "I need it for the fuel claims.",
     [(10, "Fatima", "take:Fatima", None),
      (30, "res", "resolve", {"note": "Reports > Mileage > choose the month > Export CSV.",
                              "actual_effort_minutes": 5})]),
    (6, "10:00", "appbug", "Felix", "Medium", "PORTAL", "Dark mode text unreadable on settings page",
     "Grey text on a dark grey background in the settings screen.",
     [(20, "mgr", "take:Fatima", None), (30, "res", "start", None), (90, "res", "release", None),
      (120, "mgr", "take:Tom", None), (130, "res", "start", None),
      (400, "res", "resolve", {"note": "Contrast fixed in 3.4.3.", "actual_effort_minutes": 60})]),
    (5, "11:00", "appbug", "Felix", "Critical", "PORTAL", "Login fails for all drivers in depot North",
     "Every driver in depot North gets 'invalid token' since 10:45.",
     [(5, "mgr", "take:Fatima", None), (10, "res", "start", None),
      (90, "res", "resolve", {"note": "An expired signing certificate for that depot's tenant was renewed.",
                              "actual_effort_minutes": 80})]),
    (4, "10:20", "tracking", "Dana", "Medium", "API", "Vehicle NW-114 shows in the wrong city",
     "NW-114 is in Leeds but the map shows Manchester.",
     [(10, "Tom", "take:Tom", None), (20, "res", "start", None),
      (50, "res", "request-info", {"question": "Can you confirm the tracker serial number on the dashboard unit?"})]),
    (3, "13:40", "appbug", "Felix", "High", "PORTAL", "Delivery photos not uploading on Android 15",
     "Proof-of-delivery photos stay in 'pending upload'.",
     [(20, "mgr", "take:Fatima", None), (25, "mgr", "estimate", {"minutes": 600}), (40, "res", "start", None)]),
    (2, "09:05", "question", "Felix", "Low", "PORTAL", "Can we add a second depot manager?",
     "Depot South needs two managers who can approve routes.",
     [(10, "Tom", "take:Tom", None)]),
    (1, "11:30", "tracking", "Dana", "High", "API", "Route history missing for 20 November",
     "The history page is empty for all vehicles on 20 November.", []),
    (0, "07:50", "appbug", "Dana", "Medium", "API", "Push notifications arrive twice",
     "Drivers get every new-job notification twice.", []),
]


def working_day(days_ago: int, hhmm: str, now: datetime) -> datetime:
    d = now.date()
    n = days_ago
    while n > 0 or d.weekday() >= 5:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    h, m = map(int, hhmm.split(":"))
    at = datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc)
    return min(at, now - timedelta(minutes=30))


def build_structure(db, P):
    internal = db.scalar(select(Organization).where(Organization.type == "INTERNAL").order_by(Organization.id))
    orgs = {"internal": internal,
            "acme": st.create_organization(db, P, {"name": "Acme Retail", "type": "CLIENT"}),
            "northwind": st.create_organization(db, P, {"name": "Northwind Logistics", "type": "CLIENT"})}
    projects = {
        "IT": st.create_project(db, P, {"organization_id": internal.id, "name": "Internal IT", "key": "IT"}),
        "SHOP": st.create_project(db, P, {"organization_id": orgs["acme"].id, "name": "Acme web shop",
                                          "key": "SHOP"}),
        "FLEET": st.create_project(db, P, {"organization_id": orgs["northwind"].id, "name": "Northwind fleet app",
                                           "key": "FLEET"}),
    }
    st.put_project_settings(db, P, projects["SHOP"].id, {"product_user.portal_access": True,
                                                         "product_user.can_set_priority": False})
    st.put_project_settings(db, P, projects["FLEET"].id, {"product_user.portal_access": True,
                                                          "product_user.can_set_priority": True})
    night = cfg.create_calendar(db, P, {"name": "Night shift (UTC)", "time_zone": "UTC"})
    cfg.put_hours(db, P, night.id, {"windows": [{"weekday": d, "start": "22:00", "end": "06:00"}
                                                for d in range(1, 8)]})

    people = {}
    for name, org in PEOPLE:
        u, _ = st.create_user(db, P, {"user_type": "MEMBER", "name": name, "organization_id": orgs[org].id,
                                      "email": name.split()[0].lower() + "@demo.test"})
        people[name.split()[0]] = u
    for name, proj, ext in PRODUCT_USERS:
        u, _ = st.create_user(db, P, {"user_type": "PRODUCT_USER", "name": name, "project_id": projects[proj].id,
                                      "email": name.split()[0].lower() + "@demo.test", "external_id": ext})
        people[name.split()[0]] = u
    for u in people.values():
        u.password_hash = hash_password(PASSWORD)
        u.must_change_password = False

    def grant(**kw):
        st.create_assignment(db, P, kw)

    teams = {}
    for key, (proj, name, mgr, resolvers) in TEAMS.items():
        teams[key] = st.create_team(db, P, projects[proj].id, {"name": name})
        if key != "payments":  # Paula manages Payments as the project admin of SHOP
            grant(user_id=people[mgr].id, role="TEAM_MANAGER", team_id=teams[key].id)
        for r in resolvers:
            extra = {"calendar_id": night.id} if r == "Nina" else {}
            grant(user_id=people[r].id, role="RESOLVER", team_id=teams[key].id, **extra)
    grant(user_id=people["Paula"].id, role="PROJECT_ADMIN", project_id=projects["SHOP"].id)
    grant(user_id=people["Priya"].id, role="PROJECT_ADMIN", project_id=projects["FLEET"].id)

    types = {}
    for key, (proj, name, team, mode) in TYPES.items():
        types[key] = cfg.create_ticket_type(db, P, {"project_id": projects[proj].id, "name": name,
                                                    "team_id": teams[team].id, "assignment_mode": mode})

    prios = {p.name: p for p in db.scalars(select(Priority))}
    for name, mins in [("Critical", (15, 30, 240)), ("High", (30, 60, 480)), ("Medium", (60, 240, 960)),
                       ("Low", (240, 480, 2400))]:
        pol = sla.create_policy(db, P, {"name": f"{name} default", "priority_id": prios[name].id})
        steps = sla.put_metrics(db, P, pol.id, {"steps": [
            {"name": "Assignment", "minutes": mins[0], "stop_status_code": "ASSIGNED"},
            {"name": "First response", "minutes": mins[1], "stop_status_code": "IN_PROGRESS"},
            {"name": "Resolution", "minutes": mins[2], "stop_status_code": "RESOLVED"}]})
        escalation.put_rules(db, P, steps[0].id, {"rules": [
            {"level": 1, "name": f"{name}: queue 80%", "trigger": "PERCENT_USED", "threshold": 80,
             "action": "NOTIFY", "target_type": "TEAM_MANAGERS"},
            {"level": 2, "name": f"{name}: unassigned breach", "trigger": "BREACHED", "action": "NOTIFY",
             "target_type": "PROJECT_ADMINS"}]})
        escalation.put_rules(db, P, steps[2].id, {"rules": [
            {"level": 1, "name": f"{name}: resolution breached", "trigger": "BREACHED",
             "action": "INCREASE_PRIORITY"}]})
    db.flush()
    return people, teams, types, prios


def replay_tickets(db, people, teams, types, prios, real_now):
    managers = {key: people[mgr] for key, (_, _, mgr, _) in TEAMS.items()}
    team_of_type = {key: team for key, (_, _, team, _) in TYPES.items()}
    principal = lambda u: load_principal(db, db.get(User, u.id))  # noqa: E731

    queue = []  # (time, sequence, ticket index, step)
    for i, (days, hhmm, tkey, req, prio, source, title, desc, steps) in enumerate(TICKETS):
        created = working_day(days, hhmm, real_now)
        queue.append((created, 0, i, ("create", None)))
        for n, (offset, actor, action, body) in enumerate(steps, start=1):
            at = created + timedelta(minutes=offset)
            if at >= real_now:
                print(f"  skipped a future step of '{title}'")
                continue
            queue.append((at, n, i, (actor, action, body)))
    queue.sort(key=lambda q: (q[0], q[2], q[1]))

    made = {}
    problems = 0
    for at, _, i, step in queue:
        days, hhmm, tkey, req, prio, source, title, desc, _ = TICKETS[i]
        clock.set_now(at)
        try:
            with db.begin_nested():
                if step[0] == "create":
                    data = {"project_id": types[tkey].project_id, "type_id": types[tkey].id, "title": title,
                            "description": desc}
                    if prio:
                        data["priority_id"] = prios[prio].id
                    t, _ = tickets.create_ticket(db, principal(people[req]), data, source)
                    made[i] = t.id
                    continue
                actor, action, body = step
                t = db.get(Ticket, made[i])
                db.refresh(t)
                mgr = managers[team_of_type[tkey]]
                if action.startswith("take:"):
                    who = people[action.split(":", 1)[1]]
                    tt = db.get(TicketType, t.type_id)
                    if t.resolver_id == who.id:
                        continue
                    if t.resolver_id is None and tt.assignment_mode == "AUTO" and t.waiting_for_candidate:
                        tickets.perform(db, principal(who), t.id, "claim")
                    elif t.resolver_id is None:
                        tickets.perform(db, principal(mgr), t.id, "assign", {"resolver_id": who.id})
                    else:
                        tickets.perform(db, principal(mgr), t.id, "reassign",
                                        {"resolver_id": who.id, "reason": "Rebalanced by the team manager."})
                    continue
                user = {"res": db.get(User, t.resolver_id) if t.resolver_id else None, "mgr": mgr,
                        "req": db.get(User, t.requester_id)}.get(actor) or people[actor]
                body = dict(body or {})
                if "priority" in body:
                    body = {"priority_id": prios[body["priority"]].id}
                tickets.perform(db, principal(user), t.id, action, body)
        except AppError as e:
            problems += 1
            print(f"  {at:%Y-%m-%d %H:%M} '{title}': {step[1] if step[0] != 'create' else 'create'} -> {e.code}")
    clock.set_now(None)
    return len(made), problems


def main():
    real_now = clock.now()
    with SessionLocal() as db:
        root = db.scalar(select(User).where(User.is_platform_admin.is_(True)).order_by(User.id))
        if root is None:
            sys.exit("Run the seed first: uv run python -m ticketing.seed")
        if db.scalar(select(Organization).where(Organization.name == "Acme Retail")):
            print("demo data already present (load it into an empty database)")
            return
        P = load_principal(db, root)
        people, teams, types, prios = build_structure(db, P)
        count, problems = replay_tickets(db, people, teams, types, prios, real_now)
        closed = tickets.auto_close(db)
        db.commit()
    stats = jobs.run_cycle()  # SLA breaches, escalations, no-reply and routing as of now
    print(f"Demo data created: {count} tickets ({closed} auto-closed, {problems} steps refused). Worker: "
          f"{ {k: v for k, v in stats.items() if not str(k).endswith('_ms')} }")
    print("Accounts (password below, no change needed at first sign-in):")
    for name, _ in PEOPLE:
        print(f"  {name.split()[0].lower()}@demo.test  {name}")
    for name, proj, _ in PRODUCT_USERS:
        print(f"  {name.split()[0].lower()}@demo.test  {name} (product user, {proj})")
    print(f"Password: {PASSWORD}")


if __name__ == "__main__":
    main()
