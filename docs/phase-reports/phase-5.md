# Phase 5 report: screens and hardening

**Delivered:** all 30 screens (S01–S30), the shell (role menu, project selector, bell with unread count, footer
with name, role in the selected project and sign-out) and the 14 action dialogs, server-rendered over the same
service layer as the API. CSRF on every form, output escaping (Jinja2 autoescape), HttpOnly SameSite session
cookie, login rate limiting, secrets shown once. A demonstration data script, a load test for the API and pages, a
backup and restore drill.

**Acceptance**

| Criterion | Result |
|---|---|
| Each role sees only its own menu, lists and buttons; different roles in two projects show a different role label after switching | tested (`test_role_menus_and_hidden_screens`, `test_menu_follows_selected_project`) |
| Client staff see nothing of other organisations; a product user sees only their own tickets in the channel their project enables | tested in Phase 2 (API) and through the portal role tests |
| End-to-end raise → close through the portal, with an SLA breach and an escalation alert | tested (`test_end_to_end_with_breach_and_escalation_alert`) and checked by hand in a real browser (sign in, ticket page, Assign dialog, SLA strip updates) |
| All 30 screens pass the keyboard and not-by-colour-alone checks | automated label and chip-text checks on every screen; native `<dialog>`; manual keyboard pass not done |
| Page and API latency meet D32 at the tested load | **met up to ~25 concurrent users, not at 100** on this laptop (CHANGES P5-11) |
| `TRACEABILITY.md` has no function without a test | done |

**Parity:** for 10 actions × 7 people (requester, two resolvers, manager, project admin, platform admin,
outsider) the screen gives the same permission outcome as the API call.

**Hardening found and fixed two real problems** under load: a pool/threadpool deadlock and lock contention on
session rows (CHANGES P5-8, P5-9).

**Remaining human actions, Proposed decisions and known limits:** see `ADMIN.md` → "Before release".
