# Phase 3 report: SLA policies and timers

**Delivered:** 10 endpoints — G11 (read timers, manual pause, resume, extend) and API-092..097 (policies and
steps). Policy match (type then default), timers from the post-create hook, pause by status category and manually,
stop on the working path (quick fixes close earlier steps), skip on reject/withdraw, re-arm on release, restart on
reopen, replacement on priority change (D28), extensions, breach sweep (`breached_at` once + INTERNAL history row),
`nearest_due` on tickets and lists, and the overdue filter.

**Accuracy against worked examples:** due times reproduce exactly on the clock-driven harness — standard week,
holiday (due moves a day), after-hours raise (rolls into the next morning), pause keeps the remaining time (Mon
12:20 + 460 min → Tue 12:00), priority change counted from the original start, re-arm after release, restart on
reopen. Engine examples (holiday, overnight, Kolkata, New York DST) are in `tests/test_worktime.py`.

**Timer behaviour table**

| Event | Unfinished steps | Finished steps |
|---|---|---|
| Enter ON_HOLD / AWAITING_REQUESTER | PAUSED (STATUS) | — |
| Manual pause | PAUSED (MANUAL) until manual resume, whatever the status | — |
| Enter a status at or past a step's stop status | STOPPED: MET, or BREACHED if past due | — |
| Reject / withdraw | SKIPPED | unchanged |
| Release (back to NEW) | continue | STOPPED steps no longer reached → RUNNING again |
| Reopen | old timers archived; new SLA started | archived |
| Priority change | replaced under the new policy, clock kept | MET stays MET |
| Due time passes while unfinished | `breached_at` set once, history row | — |

**Tests:** 30 new (clock harness scenarios, policy validation and scope, role denial on all 10 endpoints). The whole
Phase 1–2 suite stays green.

**Deviations:** CHANGES-FROM-PLAN P3-1..P3-13.
