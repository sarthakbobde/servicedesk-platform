# Phase 4 report: escalation and notification means

**Delivered:** 13 endpoints — escalation rules (API-098, API-099), G13 channels and templates (API-100..108),
G14 dashboard and system status (API-109, API-110). The escalation engine (5 triggers, 3 actions, F7.6 pairs,
once per ticket per rule per reopen cycle, INCREASE_PRIORITY once per cycle), SLA warning and breach alerts, the
EMAIL and CHAT_WEBHOOK adapters behind one base class / registry / SPEC, outbox delivery with back-off, the channel
test and template preview, the dashboard and the system status. Migration 0002 adds `sla_timer.warned_at`.

The full API inventory is now in place: **109 endpoints** (`/api/docs`).

**Acceptance**

| Criterion | Result |
|---|---|
| A rule fires once per level; reopen starts a new cycle; history names the rule; on-hold escalation fires on a paused timer | tested (`test_escalation.py`) |
| A rule that would loop (priority increase) fires once | tested: three policies each raising priority → one change |
| A failed email is retried and never changes the ticket; a down chat endpoint does not stop in-app alerts | tested with a fake SMTP server and an `httpx.MockTransport` returning 503 |
| Worker cycle within 30 s at 10,000 open timers | **met for steady state (4.5–5 s at 10,200 timers)**; bursts of thousands of simultaneous firings take longer (CHANGES P4-13) |
| All 13 endpoints pass role-denial tests | tested (requester, resolver, manager, outsider; project admin on platform-only ones) |

**Channel failure drill:** email channel pointed at a refusing SMTP server → 5 attempts with back-off, then FAILED;
the channel shows the last failure on S27 and the system status; the ticket stayed ASSIGNED and in-app alerts
arrived. Switching the server back on, "Send a test" succeeds.

**Escalation matrix as configured:** none in production yet (needs your escalation matrix, plan "Human
prerequisites"). `scripts/demo_data.py` configures an example: assignment step — 80 % notify team managers,
breach notify project admins; resolution step — breach raises priority.

**Deviations:** CHANGES-FROM-PLAN P4-1..P4-13.
