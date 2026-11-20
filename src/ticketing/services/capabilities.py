"""Menu capabilities (API-005, A9.1). The portal menu and screens are built from these strings."""
from ..permissions import Principal


def capabilities(p: Principal) -> set[str]:
    caps = {"ticket.raise", "tickets.mine", "notifications.view", "account.view"}
    if p.is_product_user:
        return caps
    if p.teams():
        caps |= {"work.view", "queue.view"}
    if p.teams("TEAM_MANAGER"):
        caps |= {"team.board", "tickets.all"}
    if p.any_admin():
        caps |= {"tickets.all", "admin", "admin.dashboard", "admin.projects", "admin.users", "admin.audit",
                 "admin.ticket_types", "admin.sla_policies", "admin.role_assignments"}
    if p.is_platform_admin or p.orgs_admin():
        caps.add("admin.organizations")
    if p.is_platform_admin:
        caps |= {"admin.platform", "admin.priorities", "admin.statuses", "admin.calendars", "admin.settings",
                 "admin.channels", "admin.templates", "admin.system"}
    return caps
