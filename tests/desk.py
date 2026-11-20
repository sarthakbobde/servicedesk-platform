"""A small realistic setup used by the ticket tests: client org Acme, project ACM, team Support with a manager and
two resolvers, an AUTO type (Bug) and a MANUAL type (Request), a requester, and a project admin."""
from dataclasses import dataclass

from ticketing.models import Organization, Project, Team, TicketType, User


@dataclass
class Desk:
    org: Organization
    proj: Project
    team: Team
    bug: TicketType
    req_type: TicketType
    manager: User
    rob: User
    rae: User
    quinn: User
    padmin: User


def build(f, resolver_calendar=None) -> Desk:
    org = f.org("Acme")
    proj = f.project(org, "ACM", "Acme portal")
    team = f.team(proj, "Support")
    bug = f.ticket_type(proj, team, "Bug", "AUTO")
    req_type = f.ticket_type(proj, team, "Request", "MANUAL")
    manager = f.member("Mia Manager", org=org)
    rob = f.member("Rob Resolver", org=org)
    rae = f.member("Rae Resolver", org=org)
    quinn = f.member("Quinn Requester", org=org)
    padmin = f.member("Pat Admin", org=org)
    f.grant(manager, "TEAM_MANAGER", team=team)
    f.grant(rob, "RESOLVER", team=team, calendar=resolver_calendar)
    f.grant(rae, "RESOLVER", team=team, calendar=resolver_calendar)
    f.grant(padmin, "PROJECT_ADMIN", project=proj)
    return Desk(org, proj, team, bug, req_type, manager, rob, rae, quinn, padmin)


def raise_ticket(caller, desk, type_=None, title="Login broken", **extra):
    body = {"project_id": desk.proj.id, "type_id": (type_ or desk.bug).id, "title": title,
            "description": "It fails with error 500.", **extra}
    r = caller.post("/api/v1/tickets", json=body)
    assert r.status_code == 201, r.text
    return r.json()
