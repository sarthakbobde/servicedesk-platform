"""Demonstration data (Phase 5): two projects (one internal, one client with product users on the API and the
portal), people holding several roles, ticket types, a night shift, SLA policies with escalation rules and a few
tickets in different states. Run after the seed on an empty database:

    uv run python -m ticketing.seed && uv run python scripts/demo_data.py

All demo accounts use the password printed at the end and must change it at first sign-in.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import select  # noqa: E402

from ticketing.db import SessionLocal  # noqa: E402
from ticketing.models import Organization, Priority, User  # noqa: E402
from ticketing.permissions import load_principal  # noqa: E402
from ticketing.security import hash_password  # noqa: E402
from ticketing.services import configuration as cfg  # noqa: E402
from ticketing.services import escalation, sla, structure as st, tickets  # noqa: E402

PASSWORD = os.environ.get("DEMO_PASSWORD", "Demo-password-2026")


def main():
    with SessionLocal() as db:
        root = db.scalar(select(User).where(User.is_platform_admin.is_(True)).order_by(User.id))
        P = load_principal(db, root)
        if db.scalar(select(Organization).where(Organization.name == "Acme Retail")):
            print("demo data already present")
            return
        internal = db.scalar(select(Organization).where(Organization.type == "INTERNAL").order_by(Organization.id))
        acme = st.create_organization(db, P, {"name": "Acme Retail", "type": "CLIENT"})
        itp = st.create_project(db, P, {"organization_id": internal.id, "name": "Internal IT", "key": "IT"})
        shop = st.create_project(db, P, {"organization_id": acme.id, "name": "Acme web shop", "key": "SHOP"})
        st.put_project_settings(db, P, shop.id, {"product_user.portal_access": True,
                                                 "product_user.can_set_priority": False})
        it_desk = st.create_team(db, P, itp.id, {"name": "Service desk"})
        shop_l1 = st.create_team(db, P, shop.id, {"name": "Shop support"})
        night = cfg.create_calendar(db, P, {"name": "Night shift (UTC)", "time_zone": "UTC"})
        cfg.put_hours(db, P, night.id, {"windows": [{"weekday": d, "start": "22:00", "end": "06:00"}
                                                    for d in range(1, 8)]})
        people = {}
        for name, org in [("Maya Manager", internal), ("Ravi Resolver", internal), ("Nina Night", internal),
                          ("Paula Project-Admin", acme), ("Carl Client-Staff", acme), ("Rita Requester", internal)]:
            u, _ = st.create_user(db, P, {"user_type": "MEMBER", "name": name, "organization_id": org.id,
                                          "email": name.split()[0].lower() + "@demo.test"})
            u.password_hash = hash_password(PASSWORD)
            people[name.split()[0]] = u
        g = lambda **kw: st.create_assignment(db, P, kw)  # noqa: E731
        g(user_id=people["Maya"].id, role="TEAM_MANAGER", team_id=it_desk.id)
        g(user_id=people["Maya"].id, role="RESOLVER", team_id=shop_l1.id)  # several roles in two projects
        g(user_id=people["Ravi"].id, role="RESOLVER", team_id=it_desk.id)
        g(user_id=people["Ravi"].id, role="RESOLVER", team_id=shop_l1.id)
        g(user_id=people["Nina"].id, role="RESOLVER", team_id=shop_l1.id, calendar_id=night.id)
        g(user_id=people["Paula"].id, role="PROJECT_ADMIN", project_id=shop.id)
        g(user_id=people["Carl"].id, role="TEAM_MANAGER", team_id=shop_l1.id)
        pu, _ = st.create_user(db, P, {"user_type": "PRODUCT_USER", "name": "Sam Shopper", "email": "sam@demo.test",
                                       "project_id": shop.id, "external_id": "cust-1001"})
        pu.password_hash = hash_password(PASSWORD)
        laptop = cfg.create_ticket_type(db, P, {"project_id": itp.id, "name": "Laptop problem", "team_id": it_desk.id,
                                       "assignment_mode": "MANUAL"})
        access = cfg.create_ticket_type(db, P, {"project_id": itp.id, "name": "Access request", "team_id": it_desk.id,
                                       "assignment_mode": "AUTO"})
        order = cfg.create_ticket_type(db, P, {"project_id": shop.id, "name": "Order problem", "team_id": shop_l1.id,
                                               "assignment_mode": "AUTO"})
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
        rita = load_principal(db, people["Rita"])
        sam = load_principal(db, pu)
        t1, _ = tickets.create_ticket(db, rita, {"project_id": itp.id, "type_id": laptop.id, "title":
                                                 "Laptop will not boot", "description": "Black screen since today."},
                                      "PORTAL")
        tickets.create_ticket(db, rita, {"project_id": itp.id, "type_id": access.id, "title": "Access to the "
                                         "finance share", "description": "Needed for month end."}, "PORTAL")
        tickets.create_ticket(db, sam, {"project_id": shop.id, "type_id": order.id, "title": "Order 5531 not "
                                        "delivered", "description": "Tracking stuck for 5 days."}, "API")
        db.commit()
    print("Demo data created. Accounts (all must change the password at first sign-in):")
    for e in ("maya", "ravi", "nina", "paula", "carl", "rita", "sam"):
        print(f"  {e}@demo.test")
    print(f"Password: {PASSWORD}")


if __name__ == "__main__":
    main()
