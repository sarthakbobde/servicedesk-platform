# Decisions

**Confirmed** = you answered it. **Proposed** = built as the default below; needs your answer. **Built** notes say
how the implementation realised it; see `CHANGES-FROM-PLAN.md` for anything that differs.

| ID | Question | Decision as built | Status |
| --- | --- | --- | --- |
| D1 | Release scope | Per A1 of the plan. Nothing from the deferred list is built. | Proposed |
| D2 | Stack | Python 3.12, FastAPI, Jinja2, SQLAlchemy 2.1, Alembic, PostgreSQL only. No JS framework; one small script for dialogs and the bell. | Proposed |
| D3 | Structure and access | Organisation → project → team → role assignment. Rights on a ticket are the union of matching assignments (`permissions.Principal`). | Proposed |
| D4 | Sign-in | Local accounts, email and password, created by an administrator. | **Confirmed** |
| D5 | API access | Staff: bearer token from API-003. Products: `X-Project-Key` + `X-Project-Secret` + `X-External-User`. | Proposed |
| D6 | Product-user channels | Project settings `product_user.api_access` and `product_user.portal_access`; at least one stays on (validated). | **Confirmed** |
| D7 | Ticket writing | Notes on actions only. | **Confirmed** |
| D8 | Statuses | Rows in `ticket_status` with category OPEN/ACTIVE/PAUSED/DONE; transition table in code (`services/tickets.py`). | Proposed |
| D9 | SLA clock | As proposed; see Phase 3 report. | Proposed |
| D10 | Routing | As proposed; see `services/routing.py`. | Proposed |
| D11 | Escalation | Fires once per ticket, rule and reopen cycle (`escalation_firing` unique key). | Proposed |
| D12 | Notification means | In-app always on; email and chat webhook optional; retries with back-off through the outbox. | Proposed |
| D13 | Background work | Database-backed `job` table, `FOR UPDATE SKIP LOCKED`, at-least-once, idempotent handlers. Spike: ~350 jobs/s with 4 workers, claim race correct; no broker needed (CHANGES P2-17). | Proposed |
| D14 | Platform administration | `users.is_platform_admin`; granted by platform admins (API-024/026). | Proposed |
| D15 | Ticket numbers | `KEY-n` from `project.ticket_seq`, incremented under row lock; key locked after the first ticket. | Proposed |
| D16 | Setting defaults | F11 table, plus `sla.warning_percent` = 80 (CHANGES P1-6). | Proposed |
| D17 | Moving tickets | No move; reject with WRONG_TEAM; reassign within the team only. | **Confirmed** |
| D18 | API contract depth | Pydantic request models for everyday endpoints (field-level in OpenAPI); admin endpoints take the shared pattern. | Proposed |
| D19 | Reject reason codes | WRONG_TEAM, DUPLICATE, NOT_A_TICKET, INFO_MISSING, OTHER; NO_REPLY system only. | Proposed |
| D20 | What requesters see | PUBLIC history rows; notes only for info_requested, responded, resolved, rejected, withdrawn, reopened; no SLA, estimate, effort or hold reason. | Proposed |
| D21 | Who may raise | As confirmed (`Principal.raisable_project_ids`). | **Confirmed** |
| D22 | Shifts | Calendar on each team role assignment; empty = default calendar; changes audited. | **Confirmed** |
| D23 | SLA calendar | Policy's calendar, else the default calendar. | Proposed |
| D24 | Phase order | Each phase only adds on top of earlier ones. | **Confirmed** |
| D25 | Close versus reopen | `auto_close_days >= reopen_window_days` enforced on every effective value. | Proposed |
| D26 | Statuses | Nine core statuses locked; rename only. | Proposed |
| D27 | Escalation | Once per ticket per level per cycle; F7.6 pairs validated. | Proposed |
| D28 | Priority change | Timers replaced, counted from the original SLA start; MET steps stay MET. | Proposed |
| D29 | Step minutes | Cumulative, non-decreasing; the last step is the total. | Proposed |
| D30 | Claim and re-routing | Claim only on NEW AUTO tickets waiting for a candidate; release skips the releasing resolver once. | Proposed |
| D31 | Reopen limits | Blocked after a WRONG_TEAM or DUPLICATE reject and into an inactive team or type. A NO_REPLY reject can be reopened inside the window (CHANGES P2-8). | Proposed |
| D32 | Performance targets | Worker cycle at 10,200 open timers: 4.5–5 s steady state (met). Ten concurrent claims: one winner (met). API/page latency: met up to ~25 concurrent users on a laptop with 4 workers, **not met at 100** (CHANGES P4-13, P5-11). Re-measure on production hardware. | Proposed |
