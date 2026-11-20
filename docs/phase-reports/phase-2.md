# Phase 2 report: lifecycle, routing, core configuration

**Delivered:** 51 endpoints (G5–G10 without API-076): reference data, raise/list/read/history/counts, the 15
lifecycle actions and the 2 system actions (auto-close, no-reply), the in-app inbox, product integration, and core
configuration (ticket types, priorities, statuses rename-only, calendars with hours and holidays and a two-week
preview). Routing: manual and auto, on-shift from each person's own calendar, fewest open then longest idle,
retry job plus per-cycle shift-start check, claim fallback for AUTO types, release skips the releasing resolver
once. The database job runner and worker loop (`python -m ticketing.worker`). SLA hooks are in place and empty.

**Tests (all green):** scripted scenarios — raise → auto-assign → start → hold → resume → resolve → auto-close;
manual assign; nobody on shift → claim, and retry when the shift starts; reject and the reopen rules (blocked,
inside, outside the window); withdraw; no-reply; release/reassign; priority/estimate; two resolvers on different
shift calendars; ten concurrent claims → 1 success and 9 `already_claimed`; idempotency; D21 raise rights;
visibility of history rows and staff fields; product user raising and following through the API with isolation.
**Matrix:** `allowed_actions` checked against an independent A6 × A10.2 table for 8 roles × 9 statuses.
**Role denial:** all 51 endpoints refuse unauthenticated callers (401) and an outsider (403/404); admin endpoints
refuse resolvers; platform configuration refuses project admins.

**Spikes:** CHANGES-FROM-PLAN P2-17 — no broker needed; claim locking with `SELECT … FOR UPDATE` is correct.

**OpenAPI reference:** `/api/docs` (Swagger UI) and `/api/openapi.json`; everyday endpoints have field-level
request models.

**Deviations:** CHANGES-FROM-PLAN P2-1..P2-16.

**Known limits (A10.3):** a requester can add information only when raising, responding, withdrawing or reopening;
resolvers use request-info. No move between teams (reject WRONG_TEAM and raise again).
