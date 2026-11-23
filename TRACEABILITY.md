# Traceability

Every function of the plan mapped to its module, endpoints, screens and tests. Modules are under
`src/ticketing/`, tests under `tests/`.

| Function | Module | Endpoints | Screens | Tests |
|---|---|---|---|---|
| F1.1 Create account | `services/structure.py: create_user`, `services/auth.py: issue_temporary_password` | API-024 | S17 | `test_structure.py::test_create_user_shows_temporary_password_once`, `test_portal.py::test_secret_and_temporary_password_shown_once` |
| F1.2 Sign in | `services/auth.py: authenticate, start_session` | API-001 | S01 | `test_auth.py::test_login_sets_session_and_me_works`, `::test_one_message_for_unknown_email_and_wrong_password`, `test_portal.py::test_sign_in_flow_and_forced_change` |
| F1.3 Lockout | `services/auth.py: authenticate` | API-001, API-003 | S01 | `test_auth.py::test_lockout_after_five_failures_and_reset_on_success` |
| F1.4 Password rules | `services/auth.py: check_password_rules` | API-004 | S02 | `test_auth.py::test_forced_password_change_gate` |
| F1.5 Change password | `services/auth.py: change_password` | API-004 | S02 | `test_auth.py::test_change_password_ends_other_sessions` |
| F1.6 Reset password | `services/structure.py: reset_password` | API-027 | S18 | `test_auth.py::test_reset_password_by_admin` |
| F1.7 Sessions | `services/auth.py: resolve_token` | all | all | `test_auth.py::test_session_idle_and_absolute_limits` |
| F1.8 API token | `services/auth.py` | API-003 | — | `test_auth.py::test_token_lifetime` |
| F1.9 Deactivate | `services/structure.py: update_user` | API-026 | S18, S09 | `test_auth.py::test_token_and_deactivation`, `test_lifecycle.py::test_deactivated_resolver_tickets_flagged` |
| F1.10 Product-user accounts | `services/structure.py: create_user`, `services/reference.py` | API-024, API-065 | S17 | `test_structure.py::test_product_user_account_needs_portal_access`, `test_auth.py::test_product_user_token_forbidden_and_portal_gate` |
| F2.1 Organisations | `services/structure.py` | API-007..010 | S14 | `test_structure.py::test_org_project_team_flow_with_audit` |
| F2.2 Projects | `services/structure.py` | API-011..014 | S15, S16 | `test_structure.py::test_project_key_locked_after_first_ticket` |
| F2.3 Teams | `services/structure.py` | API-019..022 | S16 | `test_structure.py::test_team_with_open_tickets_cannot_be_deactivated` |
| F2.4 Role assignment | `services/structure.py: create_assignment` | API-028, 029, 031 | S16, S18 | `test_structure.py::test_scope_rules_for_role_assignments` |
| F2.5 Bulk add | `services/structure.py: bulk_assign` | API-030 | S19 | `test_structure.py::test_bulk_add_reports_each_outcome` |
| F2.6 Scope limits | `services/structure.py: _can_grant, _check_holder` | API-029 | S18 | `test_structure.py::test_project_admin_grants_only_in_scope` |
| F2.7 Project settings | `settings.py`, `services/structure.py` | API-017, 018 | S16 | `test_structure.py::test_settings_precedence_validation_and_audit` |
| F2.8 Credentials | `services/structure.py` | API-032..034 | S16 | `test_structure.py::test_credentials_secret_once_and_revoke` |
| F2.9 Deactivation | `services/structure.py` | PATCH endpoints | S14–S18 | `test_structure.py::test_team_with_open_tickets_cannot_be_deactivated` |
| F3 Intake (3.1–3.8) | `services/tickets.py: create_ticket` | API-041 | S04 | `test_lifecycle.py::test_raise_auto_assign_work_resolve_and_auto_close`, `::test_validation_on_raise`, `::test_idempotency_key`, `::test_who_may_raise_d21`, `test_product_api.py`, `test_portal.py::test_raise_and_work_a_ticket_through_the_portal` |
| F4.1 Queue | `services/tickets.py: list_query` | API-042 | S08 | `test_lifecycle.py::test_list_views_filters_and_counts` |
| F4.2 Manual routing | `services/tickets.py: _assign` | API-047, API-040 | S08, S09, S06 | `test_lifecycle.py::test_manual_type_waits_for_manager` |
| F4.3 Auto routing | `services/routing.py` | API-041 | — | `test_lifecycle.py::test_fewest_open_then_longest_idle` |
| F4.4 No candidate | `services/routing.py: try_auto_route, retry_waiting` | — | — | `test_lifecycle.py::test_nobody_on_shift_then_claim_or_retry` |
| F4.5 Claim | `services/tickets.py: _claim` | API-046 | S08 | `test_lifecycle.py::test_ten_concurrent_claims_one_winner` |
| F4.6 Assignment alert | `services/tickets.py: apply_assignment` | — | S11 | `test_lifecycle.py::test_manual_type_waits_for_manager` |
| F4.7 Shifts | `services/routing.py: on_shift`, `worktime.py` | API-031, API-040 | S16, S18 | `test_lifecycle.py::test_two_shift_calendars_only_on_shift_is_picked` |
| F4.8 Re-routing | `services/tickets.py: _release, _reopen` | API-056, API-058 | S06 | `test_lifecycle.py::test_release_skips_releasing_resolver_once_and_reassign`, `::test_release_only_candidate_waits` |
| F5 Lifecycle (15 + 2) | `services/tickets.py` | API-046..060 | S06, S07, S08, S09 | `test_lifecycle.py`, `test_matrix.py`, `test_portal.py::test_screen_and_api_give_the_same_permission_outcome` |
| Child tickets (P5-17) | `services/tickets.py: create_child, can_add_child, children_block` | API-111, API-043 | S04, S06 | `test_children.py` (flow, one level, role denial, requester view, no-reply, portal) |
| F6.1 Policy match | `services/sla.py: match_policy` | API-092..097 | S25, S26 | `test_sla.py::test_type_policy_beats_default_and_no_policy` |
| F6.2–6.5 Timers, working time, pauses, stop | `services/sla.py` | API-088 | S06 | `test_sla.py::test_due_times_from_the_policy_calendar`, `::test_assignment_met_hold_pauses_and_resume_keeps_remaining`, `::test_quick_fix_closes_earlier_steps`, `test_worktime.py` |
| F6.6 Outcome | `services/sla.py: breach_sweep` | API-088 | S06 | `test_sla.py::test_breach_set_once_and_kept_after_stop`, `::test_reject_and_withdraw_skip_unreached_steps` |
| F6.7 Manual actions | `services/sla.py: manual_action` | API-089..091 | S06, S09 | `test_sla.py::test_manual_pause_and_status_pause_together`, `::test_extend_adds_to_due_time` |
| F6.8 Reset | `services/sla.py: on_transition` | — | — | `test_sla.py::test_release_rearms_and_reopen_restarts` |
| F6.9 Effort and time | `services/tickets.py: _resolve, _estimate` | API-053, API-060 | S06 | `test_lifecycle.py::test_resolution_time_in_working_minutes`, `::test_priority_and_estimate` |
| F6.10 Policy edits | `services/sla.py: put_metrics` | API-097 | S26 | `test_sla.py::test_policy_edit_leaves_running_timers` |
| D28 Priority change | `services/sla.py: on_priority_change` | API-059 | S06, S09 | `test_sla.py::test_priority_change_replaces_timers_d28` |
| F7 Escalation | `services/escalation.py` | API-098, 099 | S26 | `test_escalation.py` (8 scenario tests + validation) |
| F8.1–8.2 Types, inbox | `services/notify.py` | API-061..064 | S11, bell | `test_lifecycle.py::test_notifications_inbox` |
| F8.3 Channels | `services/channels.py` | API-100..104 | S27 | `test_escalation.py::test_channel_settings_never_hold_secrets` |
| F8.4 Templates | `services/channels.py`, `services/notify.py` | API-105..108 | S28 | `test_escalation.py::test_templates_placeholders_preview_and_in_app_always_on` |
| F8.5 Delivery | `services/channels.py: deliver_outbox` | — | S27, S13 | `test_escalation.py::test_email_delivered_after_commit`, `::test_failed_email_retried_never_blocks_ticket`, `::test_chat_webhook_down_does_not_stop_in_app` |
| F8.6 Test and preview | `services/channels.py` | API-104, API-108 | S27, S28 | `test_escalation.py::test_failed_email_retried_never_blocks_ticket` |
| F9 Product-user channel | `api/deps.py`, `services/reference.py` | API-065, 066, U-or-K endpoints | S04, S05 | `test_product_api.py` |
| F10.1–10.3 Lists, views, counts | `services/tickets.py: list_tickets, counts` | API-042, API-045 | S05, S07–S10, S03 | `test_lifecycle.py::test_list_views_filters_and_counts` |
| F10.4 Dashboard | `services/operations.py: dashboard` | API-109 | S13 | `test_escalation.py::test_dashboard_scoped_counts` |
| F10.5 System status | `services/operations.py: system_status` | API-110 | S13 | `test_escalation.py::test_system_status` |
| F11 Settings | `settings.py` | API-015..018 | S29, S16 | `test_structure.py::test_settings_precedence_validation_and_audit`, `test_portal.py::test_admin_forms_round_trip` |
| F12 Audit | `audit.py` | API-035 | S30 | `test_structure.py::test_audit_scoped_to_project_admin`, `::test_shift_calendar_on_assignment_change_is_audited` |
| Working-time engine | `worktime.py` | — | S24 preview | `test_worktime.py` (17 worked examples) |
| Role denial, every endpoint | `permissions.py` | all 109 | all 30 | `test_structure.py`, `test_configuration.py`, `test_sla.py`, `test_escalation.py` (`*_role_denial`, `*_denied`), `test_portal.py::test_role_menus_and_hidden_screens` |
