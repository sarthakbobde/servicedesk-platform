# Administration and runbooks

## Deploy

1. PostgreSQL 15+ database and a user that owns it.
2. Environment: `TICKETING_DATABASE_URL`, `TICKETING_SECRET_KEY` (long random value; signs CSRF tokens),
   `TICKETING_BASE_URL` (used in alert links), `TICKETING_COOKIE_SECURE=true` behind HTTPS,
   `TICKETING_BOOTSTRAP_ADMIN_EMAIL` and `TICKETING_BOOTSTRAP_ADMIN_PASSWORD` for the first platform admin.
   Channel secrets: the environment variables your channels name (`password_env`, `url_env`).
3. `uv run alembic upgrade head`, then `uv run python -m ticketing.seed` (repeatable; creates nothing twice).
4. Web: `uvicorn ticketing.main:app --app-dir src --workers N` behind a reverse proxy with HTTPS.
   Use several workers; one request costs about 40–80 ms of CPU (CHANGES P5-11).
5. Worker: exactly one or more `python -m ticketing.worker` processes (jobs use `SKIP LOCKED`, so several are
   safe). The cycle length is the setting `sla.escalation_cycle_seconds` (default 60).
6. Sign in as the bootstrap admin, change the password, then: calendars (S23/S24), priorities (S21),
   organisations, projects, teams, people and roles (S14–S19), ticket types (S20), SLA policies and escalation
   rules (S25/S26), channels and templates (S27/S28).

## Health and monitoring

- `GET /api/v1/health` — liveness and database check (503 if the database is unreachable).
- S13 / `GET /api/v1/admin/system/status` — worker last run, job backlog, failed sends, configuration warnings.
  Alert if the worker's last run is older than three cycles or failed sends keep growing.

## Backup and restore (drill done; CHANGES P5-13)

```bash
pg_dump -Fc -f ticketing-$(date +%F).dump "$DATABASE"
```

```bash
createdb ticketing_restore && pg_restore -d ticketing_restore ticketing-YYYY-MM-DD.dump
```

Check after restore: `select version_num from alembic_version` matches the release, and ticket / user counts match.

## Upgrade

Stop the worker, back up, `uv run alembic upgrade head`, start web and worker. Migrations are tested up and down
in CI (`alembic upgrade head && alembic downgrade base && alembic upgrade head`).

## Common tasks

| Task | Where |
|---|---|
| Person locked out | S18 → Reset password (issues a temporary password, shown once) — the lock clears too |
| Person leaves | S18 → Deactivate (sessions and tokens end; their open tickets are flagged on S09) |
| Change someone's shift | S16 People tab or S18 → shift calendar on the team role (audited, applies at once) |
| Product credential leaked | S16 API credentials → Revoke, then create a new one |
| Email not arriving | S27 shows the last failure; "Send a test"; S13 system status lists failed sends |
| A ticket in the wrong team | Reject with reason WRONG_TEAM; the requester raises a new one (D17) |

## Demo data

`uv run python scripts/demo_data.py` after the seed, on an empty database. Accounts end in `@demo.test`; the
password is printed and must be changed at first sign-in.

## Before release (remaining human actions, open decisions, known limits)

**Human actions (plan "Human prerequisites")**
- First organisations, projects, teams, people and roles; per client project: API, portal or both.
- Ticket types per project with owning team and manual/auto assignment.
- Shift calendars (hours, holidays, time zone) and who works which shift.
- SLA targets per priority and type; the escalation matrix.
- Mail gateway and/or chat webhook details.
- Test users for every role in two projects (one client project).
- A named owner who approves production configuration changes.
- Publish the repository: `gh auth login`, then `gh repo create` (README).

**Decisions still Proposed:** see `DECISIONS.md` (everything except D4, D6, D7, D17, D21, D22, D24).

**Not met / not done**
- D32 latency at 100 concurrent users was not met on the development laptop (met to about 25). Re-measure on
  production-like hardware with `scripts/load_api.py`; profile the ticket list view if still short.
- Escalation bursts (thousands of rules firing in one minute) make that worker cycle longer than 30 s.
- Structured JSON logs and a metrics endpoint were not built.
- A manual keyboard and screen-reader pass of all screens was not done (automated label checks only).
- The design document's own worked examples for the working-time engine were not available; please supply them.

**Known limits (by design, A10.3)**
- A requester can add information only when raising, responding, withdrawing or reopening.
- No move between teams; reject with WRONG_TEAM and raise again.
- One portal account per email platform-wide (F9.4).
- Login rate limiting is per process (CHANGES P1-9).
