# CLAUDE.md

Ticketing management system. Read `CHANGES-FROM-PLAN.md` before changing behaviour: it records where the build
departs from the development plan and why. Record any new departure there, and any new decision in `DECISIONS.md`.

## Layout

```
src/ticketing/
  config.py         environment configuration (TICKETING_*)
  db.py, models.py  SQLAlchemy engine and the 29 tables (A4)
  clock.py          injectable clock: all business code calls clock.now()
  worktime.py       working-time engine: pure functions over CalendarSpec
  settings.py       typed settings registry (F11): global + project scope, validated, hot reloaded
  audit.py          configuration audit log (F12)
  permissions.py    Principal: the ONE permission module (scope, visibility, rights)
  services/         all business rules; the API and the portal call only these
    auth.py           F1 accounts, sessions, tokens
    structure.py      F2 organisations, projects, teams, users, roles, credentials, settings
    configuration.py  G10 ticket types, priorities, statuses, calendars
    tickets.py        F3 intake, F5 lifecycle, allowed_actions, lists (F10)
    routing.py        F4 manual/auto routing, shifts, claim
    notify.py         F8 notifications, templates, outbox
    sla.py            F6 policies, timers
    escalation.py     F7 rules and evaluation
    jobs.py           D13 database-backed jobs and the worker cycle
    operations.py     F10.4 dashboard, F10.5 system status
    channels.py       F8.3 channel adapters (base class, registry, SPEC)
  api/              REST routes (thin): parse, call a service, serialise
  web/              server-rendered portal and admin portal (Jinja2), same services
  worker.py         background worker entry point
migrations/         Alembic
tests/              pytest on PostgreSQL
```

## Rules

- **One service layer.** No business rule in a route or a template. Portal and API call the same functions and the
  same `Principal` checks, so their permission outcomes are identical (parity tests).
- **One transaction per request/action.** `api/deps.TxRoute` commits after the handler succeeds; any exception
  rolls back. Lifecycle actions lock the ticket row (`SELECT ... FOR UPDATE`). Delivery to external channels happens
  after commit through `notification_outbox`.
- **Out of scope is not found.** A record the caller cannot see raises `not_found` (404), never `forbidden`.
  Check permission before validating input on create endpoints.
- **Configuration, not code branches.** Statuses, priorities, types, policies, rules, channels, templates are rows.
  Deactivate (`is_active=false`), never delete.
- **Time.** Store UTC. Use `clock.now()`, never `datetime.now()`. Working time only through `worktime`.
- **Secrets.** Passwords: Argon2. Tokens and credential secrets: SHA-256 of long random values, shown once.
  Channel secrets are referenced by environment-variable name, never stored.
- **Errors** use `errors.AppError(code)` with the A11.2 catalogue; the shape is `{"error": {code, message, fields}}`.

## Commands

```
uv run alembic upgrade head            # migrate
uv run alembic revision --autogenerate -m "..."   # new migration after a model change
uv run python -m ticketing.seed        # repeatable seed
uv run uvicorn ticketing.main:app --reload
uv run python -m ticketing.worker      # background jobs, SLA, escalation, outbox
uv run pytest                          # tests (PostgreSQL required)
```

## Testing conventions

- `tests/conftest.py` truncates and reseeds before every test and fixes the clock at Monday 2026-11-16 10:00 UTC.
  Move time with `clock.advance(minutes=...)`; run background work with `jobs.run_cycle(db)`.
- `Factory` (`f` fixture) builds structure directly in the database; `f.as_(user)` returns an API caller with a token.
- Every endpoint has a role-denial test; add one for every new route.
