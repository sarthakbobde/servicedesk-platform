# Phase 1 report: foundations

**Delivered:** schema for all 29 tables (one Alembic migration, up and down tested), repeatable seed, F1 accounts
(Argon2, lockout, server-side sessions with idle/absolute limits, revocable API tokens, change/reset password,
deactivation), F2 structure services with scope rules, the permission module (`Principal`), F11 settings registry
with cross-checks, F12 audit, the working-time engine, and the 35 endpoints of G1–G4.

**Tests:** 108 passing on PostgreSQL 17 — 17 engine worked examples, 13 account tests, 13 structure/settings/audit
tests, and role-denial tests for every Phase 1 endpoint (401 unauthenticated; 403/404 for a plain member and for a
resolver; platform-only endpoints refuse an org admin).

**Schema differences from the design:** see CHANGES-FROM-PLAN P1-1..P1-3.

**Rules enforced in the service layer, not as constraints:** a team's tickets/types belong to the team's project;
client staff hold roles only in their own organisation; administrators grant only within their scope; a team with
open tickets cannot be deactivated; project key locked after the first ticket; effective settings stay valid.
Enforced as constraints: role ↔ scope (check), one active assignment per user/role/scope (partial unique indexes),
member vs product-user fields (check), unique email (functional index), unique external ID per project, unique
ticket number per project, one default priority and calendar, one active SLA policy per priority and type.

**Spikes:** moved to Phase 2 (CHANGES P1-19).
