# Changes from the development plan

Every place where the build differs from `development-plan-ticketing-systemv1.md` (draft 3), or where the plan left
something open and a choice had to be made to keep moving. Grouped by phase, newest last. Each entry says **what**
changed, **why**, and **where** to look. Decisions that need your answer are also listed in `DECISIONS.md`.

Legend: **Change** = differs from the plan. **Choice** = the plan was silent or ambiguous; this is what was built.
**Env** = local environment only, not the product.

---

## Setup

| # | Type | What | Why | Where |
|---|------|------|-----|-------|
| S1 | Env | The GitHub repository could not be created automatically: the GitHub CLI on this machine is not signed in. All work is committed to a local git repository; run `gh auth login` and then `gh repo create` (commands in `README.md`) to publish it. | Creating a repository needs your GitHub sign-in, which I must not enter for you. | `README.md` → "Publish to GitHub" |
| S2 | Env | Development and tests use a private PostgreSQL 17 cluster on port **55432** with trust auth (`%LOCALAPPDATA%\ticketing-pgdata`), databases `ticketing` and `ticketing_test`. | The installed PostgreSQL service needs a password I don't have. | `TICKETING_DATABASE_URL`, `tests/conftest.py` |
| S3 | Env | The Python virtualenv lives outside the project (`UV_PROJECT_ENVIRONMENT=C:/Users/sbobd/.venvs/ticketing`). | The project folder path is long; files inside `.venv` passed Windows' 260-character limit and failed to import. | `README.md` |
| S4 | Choice | Python 3.12, FastAPI, SQLAlchemy 2.1, Alembic, psycopg 3, Jinja2, Argon2 for passwords (D2 as proposed). | D2 default. | `pyproject.toml` |

## Phase 1: Foundations

| # | Type | What | Why | Where |
|---|------|------|-----|-------|
| P1-1 | Change | `role_assignment` has a `scope` column (TEAM, PROJECT, ORGANIZATION) and always stores `organization_id`, plus `project_id` for team and project roles (denormalised). A check constraint enforces role ↔ scope (A4). | One indexed column per scope makes the permission and visibility queries simple and lets the database enforce the scope rule. | `models.py: RoleAssignment` |
| P1-2 | Change | `team.calendar_id` is **not created**. | A4 says it is unused (D22: shifts belong to role assignments). | `models.py: Team` |
| P1-3 | Change | Extra columns the plan did not list but the functions need: `ticket.assigned_at` (longest-idle tie-break), `info_requested_at` (no-reply timer), `hold_reason`, `reject_reason_code`, `waiting_for_candidate` (claim rule D30), `skip_resolver_id` (D30), `sla_policy_id`, `sla_calendar_id`, `sla_started_at`, `sla_cycle` (escalation once per cycle), `manual_paused`, `manual_pause_reason`; `ticket_event.note_public`, `rule_id`, `data`; `notification.subject`, `rule_id`; `notification_outbox.subject`, `body`; `job.dedupe_key`, `finished_at`; detailed `sla_timer` columns (see Phase 3). Still 29 tables. | Needed to implement F4–F7 without recomputing history. | `models.py`, `migrations/versions/0001_initial.py` |
| P1-4 | Choice | **Partial holiday** = the given window is *not* worked that day (rest of the day works as usual). A **whole-day holiday** blocks the full local date 00:00–24:00, including the part of an overnight shift that falls on it. | "Whole day off or only a window" was ambiguous. | `worktime.py`, `tests/test_worktime.py` |
| P1-5 | Change | The design's "worked examples" were not available to me, so the engine is verified against 17 worked examples written here (standard week, weekend, holiday, partial day, overnight, overnight + holiday, Asia/Kolkata, New York DST change, overlapping windows, 24 h calendars, no-hours calendar). **Please send the design's examples and I will add them as tests.** | Missing input. | `tests/test_worktime.py` |
| P1-6 | Change | New setting `sla.warning_percent` (global, default 80): the share of a step after which a ticket is "at risk" and SLA_WARNING is sent. | F8.1 has an SLA warning type and F10.4 an "at risk" list but no threshold. | `settings.py` |
| P1-7 | Change | During a forced password change, `GET /me` also works (besides API-002 and API-004). | The portal needs `/me` to route the person to the change-password form (S01). | `api/deps.py: PASSWORD_GATE_ALLOWED` |
| P1-8 | Change | **CSRF**: API writes authenticated by the *session cookie* need an `X-CSRF-Token` header; the token is returned by API-001. Bearer-token and credential callers are unaffected. | A cookie-authenticated JSON API is otherwise open to cross-site requests (Phase 5 security item, done early). | `api/deps.py` |
| P1-9 | Choice | Sign-in rate limit (`rate_limited`): 20 **failed** attempts per source IP per 5 minutes, in process memory. | A11.2 names the code but no numbers. A multi-instance deployment would need a shared store. | `api/deps.py: RateLimiter` |
| P1-10 | Choice | When an account locks, its failure counter resets to 0 (5 more failures after the lock expires lock it again). | F1.3 was silent. | `services/auth.py` |
| P1-11 | Choice | Email is unique platform-wide for all users (members and product users), case-insensitive. Domains under `.test` are accepted. | F9.4 states the product-user case; applied to everyone for one account per email. | `models.py`, `services/structure.py` |
| P1-12 | Choice | **Who a project admin may administer** (API-023..027): members of the project's organisation, product users of the project, and anyone holding a role in the project. Org admins: members of their organisation and product users of its projects. | The plan says "(scope)" without defining it for users, who are not project-scoped. | `services/structure.py: _user_scope_filter` |
| P1-13 | Change | `is_platform_admin` can be set on create (API-024) and changed (API-026) by platform admins only; you cannot remove your own flag or deactivate yourself. | D14 needs a way to grant the flag; otherwise only the seed admin would ever exist. | `services/structure.py` |
| P1-14 | Choice | Priority `rank` 1 is the most urgent. Seeded: Critical 1, High 2, **Medium 3 (default)**, Low 4. "Increase priority" moves to the next lower rank number. | Plan says "four priorities with a default". | `seed.py` |
| P1-15 | Choice | Seeded default calendar: "Standard hours (UTC)", Monday–Friday 09:00–17:00 UTC. | Plan says "a default calendar". Replace with your real hours. | `seed.py` |
| P1-16 | Choice | The first platform admin comes from `TICKETING_BOOTSTRAP_ADMIN_EMAIL` / `TICKETING_BOOTSTRAP_ADMIN_PASSWORD` (defaults `admin@example.com` / `ChangeMe!12345`) and must change the password at first sign-in. | Plan says "a first platform admin". | `seed.py`, `config.py` |
| P1-17 | Change | Error code `conflict` (409) is added for database uniqueness races not covered by a specific code. | A11.2 has no generic conflict code. | `main.py` |
| P1-18 | Choice | A product user registered through the API first (no password) can later get a portal account through API-024 with the same `external_id`; the existing identity is upgraded instead of refused. | F9.2 + F9.4 together. | `services/structure.py: create_user` |
| P1-19 | Change | The three **spikes** (claim race, job runner vs broker, auto-assignment query) run in Phase 2, once the lifecycle exists to measure; their numbers are in `DECISIONS.md` (D13, D32) and the Phase 2 report. | A claim race and an assignment query need tickets and routing to measure meaningfully. | `docs/phase-reports/` |
| P1-20 | Choice | A permission failure is checked before input validation on create endpoints, so a caller without rights gets 403/404, never a 422 that hints at the shape of a valid request. | Role-denial tests. | `services/structure.py` |
