# Ticketing Management System

Tickets with a nine-status lifecycle, queue and auto routing on people's shifts, SLA timers in working time,
escalation rules, in-app / email / chat-webhook notifications, a server-rendered portal and admin portal, and a
REST API (`/api/v1`, OpenAPI page at `/api/docs`). Python, FastAPI, SQLAlchemy, Alembic, PostgreSQL (D2).

Start with:
- `CLAUDE.md` — how the code is organised and the rules every change follows.
- `CHANGES-FROM-PLAN.md` — every deviation from the development plan, and every choice the plan left open.
- `DECISIONS.md` — D1..D32 with status; `TRACEABILITY.md` — function → module, endpoint, screen, test.
- `docs/phase-reports/` — one report per phase.

## Run locally

```bash
uv sync
```

```bash
uv run alembic upgrade head
```

```bash
uv run python -m ticketing.seed
```

```bash
uv run uvicorn ticketing.main:app --reload
```

```bash
uv run python -m ticketing.worker
```

Optional demo data (on an empty database, after the seed):

```bash
uv run python scripts/demo_data.py
```

Configuration is read from environment variables (or `.env`): `TICKETING_DATABASE_URL`
(default `postgresql+psycopg://postgres@localhost:55432/ticketing`), `TICKETING_SECRET_KEY`,
`TICKETING_BASE_URL`, `TICKETING_COOKIE_SECURE`, `TICKETING_BOOTSTRAP_ADMIN_EMAIL`,
`TICKETING_BOOTSTRAP_ADMIN_PASSWORD`. Business settings (F11) live in the database and are edited in the admin
portal or through `/api/v1/admin/settings`.

On Windows with a long project path, keep the virtualenv elsewhere:
`set UV_PROJECT_ENVIRONMENT=C:\Users\<you>\.venvs\ticketing` (see CHANGES-FROM-PLAN S3).

## Tests

```bash
uv run pytest
```

Tests need PostgreSQL. They use `TICKETING_DATABASE_URL` if set, else
`postgresql+psycopg://postgres@localhost:55432/ticketing_test`, migrate it from scratch and reseed before each test.

## Repository

https://github.com/sarthakbobde/servicedesk-platform (private)
