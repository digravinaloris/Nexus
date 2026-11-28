# Cloudflare route-to-service inventory (2026-11-28)

Audit baseline: `main` commit `582e6aaad0c0b2766f05d2bad521fd3b557ec9ed` (after PR #422). This inventory is regenerated from the current source declarations and route call sites. It is a method-name comparison only, not behavioral parity or cutover certification.

## Executive summary

- `BotService` declares 80 public operations.
- `InProcessBotService` declares 80 public methods and covers all 80 interface operations.
- The six Flask route modules call 77 distinct BotService operations.
- `SignedHttpBotService` declares 36 public methods.
- `CloudflareFetchBotService` declares 34 public methods.
- 32 of the 77 route-used operation names exist on the Worker Fetch client; 45 route-used operations remain absent.
- A matching method name does not prove equivalent authorization, error mapping, schema, mutation idempotency, audit behavior, or route response parity.

## Route usage by module

### `web/dashboard.py` — 35 operations

`api_accept_appeal`, `api_announce_calendar_event`, `api_audit_snapshot`, `api_claim_appeal_by_case`, `api_health_snapshot`, `api_log_dashboard_action`, `api_notify_appeal_result`, `api_record_audit`, `api_release_appeal_claim_by_case`, `api_resolve_appeal_by_case`, `api_sanction_cases_snapshot`, `api_update_config`, `available_guild_ids`, `dashboard_add_command_role`, `dashboard_apply_template_snapshot`, `dashboard_audit_export_records`, `dashboard_calendar_delete_event`, `dashboard_calendar_has_duplicate_event`, `dashboard_calendar_insert_event`, `dashboard_calendar_upcoming_events`, `dashboard_config_snapshot`, `dashboard_overview_snapshot`, `dashboard_presence_ping`, `dashboard_presence_viewers`, `dashboard_public_templates_snapshot`, `dashboard_remove_command_role`, `dashboard_roles_snapshot`, `dashboard_sanction_stats_snapshot`, `dashboard_sanction_types_snapshot`, `dashboard_settings_snapshot`, `dashboard_submitted_appeals_snapshot`, `dashboard_template_snapshot`, `guild_name`, `is_guild_owner`, `set_guild_locked`

### `web/public.py` — 14 operations

`api_get_appeal`, `api_post_appeal_for_review`, `api_record_audit`, `guild_name`, `health_snapshot`, `notify_owner_bruteforce`, `notify_owner_killswitch`, `public_get_killswitch_token`, `public_global_lock_state`, `public_increment_killswitch_attempts`, `public_mark_killswitch_token_used`, `public_set_global_lock`, `public_status_snapshot`, `public_submit_ban_appeal`

### `web/verify.py` — 6 operations

`api_grant_verified`, `api_record_audit`, `guild_name`, `verify_get_valid_challenge`, `verify_mark_used`, `verify_record_failed_attempt`

### `web/rest_api.py` — 34 operations

`api_accept_appeal`, `api_audit_snapshot`, `api_authenticate_key`, `api_ban_member`, `api_broadcast`, `api_claim_appeal`, `api_faq_add_entry`, `api_faq_remove_entry`, `api_faq_set_enabled`, `api_faq_snapshot`, `api_get_appeal`, `api_guild_list_snapshot`, `api_guild_stats_snapshot`, `api_health_snapshot`, `api_kick_member`, `api_member_warnings`, `api_members_snapshot`, `api_mute_member`, `api_notify_appeal_result`, `api_record_audit`, `api_release_appeal_claim`, `api_resolve_appeal`, `api_rest_config_snapshot`, `api_roles_snapshot`, `api_sanction_cases_snapshot`, `api_submitted_appeals`, `api_text_channels_snapshot`, `api_unmute_member`, `api_unwarn_member`, `api_update_config`, `api_warn_member`, `api_warnlist_snapshot`, `guild_presence`, `set_guild_locked`

### `web/auth.py` — 1 operations

`guild_presence`

### `web/github_webhook.py` — 1 operations

`api_handle_github_release`


## Current declared client surfaces

### BotService interface (80)
`health_snapshot`, `guild_presence`, `available_guild_ids`, `api_guild_id_candidates`, `api_authenticate_key`, `is_guild_owner`, `is_guild_admin`, `public_status_snapshot`, `guild_name`, `api_health_snapshot`, `api_config_snapshot`, `api_rest_config_snapshot`, `dashboard_config_snapshot`, `api_update_config`, `api_faq_snapshot`, `api_faq_add_entry`, `api_faq_remove_entry`, `api_faq_set_enabled`, `api_audit_snapshot`, `api_sanction_cases_snapshot`, `api_record_audit`, `api_member_warnings`, `api_warnlist_snapshot`, `api_submitted_appeals`, `dashboard_submitted_appeals_snapshot`, `api_get_appeal`, `public_get_killswitch_token`, `public_increment_killswitch_attempts`, `public_mark_killswitch_token_used`, `public_global_lock_state`, `public_set_global_lock`, `public_submit_ban_appeal`, `verify_get_valid_challenge`, `verify_record_failed_attempt`, `verify_mark_used`, `api_claim_appeal`, `api_release_appeal_claim`, `api_resolve_appeal`, `api_claim_appeal_by_case`, `api_release_appeal_claim_by_case`, `api_resolve_appeal_by_case`, `api_guild_list_snapshot`, `api_text_channels_snapshot`, `api_roles_snapshot`, `dashboard_presence_ping`, `dashboard_presence_viewers`, `dashboard_settings_snapshot`, `dashboard_roles_snapshot`, `dashboard_overview_snapshot`, `dashboard_add_command_role`, `dashboard_remove_command_role`, `dashboard_sanction_stats_snapshot`, `dashboard_sanction_types_snapshot`, `dashboard_calendar_has_duplicate_event`, `dashboard_calendar_insert_event`, `dashboard_calendar_delete_event`, `dashboard_calendar_upcoming_events`, `dashboard_audit_export_records`, `dashboard_public_templates_snapshot`, `dashboard_template_snapshot`, `dashboard_apply_template_snapshot`, `api_handle_github_release`, `api_members_snapshot`, `set_guild_locked`, `api_guild_stats_snapshot`, `api_unmute_member`, `api_broadcast`, `api_mute_member`, `api_ban_member`, `api_kick_member`, `api_warn_member`, `api_unwarn_member`, `api_accept_appeal`, `api_notify_appeal_result`, `api_announce_calendar_event`, `api_grant_verified`, `api_log_dashboard_action`, `api_post_appeal_for_review`, `notify_owner_bruteforce`, `notify_owner_killswitch`

### In-process adapter (80)
`dashboard_presence_ping`, `dashboard_presence_viewers`, `is_guild_owner`, `is_guild_admin`, `available_guild_ids`, `api_guild_id_candidates`, `api_authenticate_key`, `guild_presence`, `guild_name`, `api_health_snapshot`, `api_guild_list_snapshot`, `api_text_channels_snapshot`, `api_roles_snapshot`, `dashboard_settings_snapshot`, `dashboard_roles_snapshot`, `api_handle_github_release`, `dashboard_overview_snapshot`, `api_members_snapshot`, `set_guild_locked`, `api_config_snapshot`, `dashboard_config_snapshot`, `api_rest_config_snapshot`, `api_update_config`, `api_faq_snapshot`, `api_faq_add_entry`, `api_faq_remove_entry`, `api_faq_set_enabled`, `api_audit_snapshot`, `api_sanction_cases_snapshot`, `api_record_audit`, `api_member_warnings`, `api_warnlist_snapshot`, `api_submitted_appeals`, `dashboard_submitted_appeals_snapshot`, `api_get_appeal`, `api_claim_appeal`, `api_release_appeal_claim`, `api_resolve_appeal`, `api_claim_appeal_by_case`, `api_release_appeal_claim_by_case`, `api_resolve_appeal_by_case`, `dashboard_add_command_role`, `dashboard_remove_command_role`, `dashboard_sanction_stats_snapshot`, `dashboard_sanction_types_snapshot`, `dashboard_calendar_has_duplicate_event`, `dashboard_calendar_insert_event`, `dashboard_calendar_delete_event`, `dashboard_calendar_upcoming_events`, `dashboard_audit_export_records`, `public_get_killswitch_token`, `public_increment_killswitch_attempts`, `public_mark_killswitch_token_used`, `public_global_lock_state`, `public_set_global_lock`, `public_submit_ban_appeal`, `verify_get_valid_challenge`, `verify_record_failed_attempt`, `verify_mark_used`, `dashboard_public_templates_snapshot`, `dashboard_template_snapshot`, `dashboard_apply_template_snapshot`, `api_guild_stats_snapshot`, `api_unmute_member`, `api_broadcast`, `api_mute_member`, `api_ban_member`, `api_kick_member`, `api_warn_member`, `api_unwarn_member`, `api_accept_appeal`, `api_notify_appeal_result`, `api_grant_verified`, `api_log_dashboard_action`, `api_post_appeal_for_review`, `notify_owner_bruteforce`, `notify_owner_killswitch`, `api_announce_calendar_event`, `public_status_snapshot`, `health_snapshot`

### Signed HTTP adapter (36)
`health_snapshot`, `public_status_snapshot`, `available_guild_ids`, `is_guild_owner`, `is_guild_admin`, `guild_presence`, `guild_name`, `api_text_channels_snapshot`, `api_roles_snapshot`, `api_faq_snapshot`, `api_members_snapshot`, `api_guild_list_snapshot`, `dashboard_sanction_stats_snapshot`, `dashboard_sanction_types_snapshot`, `api_rest_config_snapshot`, `dashboard_config_snapshot`, `dashboard_public_templates_snapshot`, `dashboard_template_snapshot`, `dashboard_presence_ping`, `dashboard_presence_viewers`, `dashboard_calendar_has_duplicate_event`, `dashboard_calendar_upcoming_events`, `api_authenticate_key`, `api_member_warnings`, `api_warnlist_snapshot`, `api_audit_snapshot`, `dashboard_submitted_appeals_snapshot`, `api_claim_appeal_by_case`, `api_release_appeal_claim_by_case`, `api_resolve_appeal_by_case`, `api_sanction_cases_snapshot`, `dashboard_roles_snapshot`, `dashboard_settings_snapshot`, `dashboard_overview_snapshot`, `api_guild_stats_snapshot`, `api_health_snapshot`

### Cloudflare Worker Fetch adapter (34)
`health_snapshot`, `public_status_snapshot`, `public_global_lock_state`, `is_guild_owner`, `is_guild_admin`, `guild_presence`, `guild_name`, `api_health_snapshot`, `api_guild_list_snapshot`, `api_guild_id_candidates`, `available_guild_ids`, `api_text_channels_snapshot`, `api_roles_snapshot`, `api_guild_stats_snapshot`, `api_faq_snapshot`, `api_members_snapshot`, `api_rest_config_snapshot`, `dashboard_config_snapshot`, `dashboard_public_templates_snapshot`, `dashboard_template_snapshot`, `dashboard_presence_ping`, `dashboard_presence_viewers`, `dashboard_submitted_appeals_snapshot`, `api_member_warnings`, `api_warnlist_snapshot`, `api_sanction_cases_snapshot`, `api_audit_snapshot`, `dashboard_calendar_has_duplicate_event`, `dashboard_calendar_upcoming_events`, `dashboard_overview_snapshot`, `dashboard_sanction_stats_snapshot`, `dashboard_sanction_types_snapshot`, `dashboard_roles_snapshot`, `dashboard_settings_snapshot`

## Route-used operations absent from Worker Fetch (45)

`api_accept_appeal`, `api_announce_calendar_event`, `api_authenticate_key`, `api_ban_member`, `api_broadcast`, `api_claim_appeal`, `api_claim_appeal_by_case`, `api_faq_add_entry`, `api_faq_remove_entry`, `api_faq_set_enabled`, `api_get_appeal`, `api_grant_verified`, `api_handle_github_release`, `api_kick_member`, `api_log_dashboard_action`, `api_mute_member`, `api_notify_appeal_result`, `api_post_appeal_for_review`, `api_record_audit`, `api_release_appeal_claim`, `api_release_appeal_claim_by_case`, `api_resolve_appeal`, `api_resolve_appeal_by_case`, `api_submitted_appeals`, `api_unmute_member`, `api_unwarn_member`, `api_update_config`, `api_warn_member`, `dashboard_add_command_role`, `dashboard_apply_template_snapshot`, `dashboard_audit_export_records`, `dashboard_calendar_delete_event`, `dashboard_calendar_insert_event`, `dashboard_remove_command_role`, `notify_owner_bruteforce`, `notify_owner_killswitch`, `public_get_killswitch_token`, `public_increment_killswitch_attempts`, `public_mark_killswitch_token_used`, `public_set_global_lock`, `public_submit_ban_appeal`, `set_guild_locked`, `verify_get_valid_challenge`, `verify_mark_used`, `verify_record_failed_attempt`

Each absent operation needs an explicit decision: add a reviewed DTO/action contract, retain it on the persistent bot host, or remove it only through a separate product decision. Mutations and security-sensitive flows must not be bulk-forwarded. They require operation-specific actor identity, bot-side authorization, idempotency, audit logging, bounded schemas, and stable failure semantics.

## Next sequence

1. Continue with reviewed read-only route gaps only; verify source DTOs and authorization before adding transport methods.
2. Keep moderation, verification, appeals, configuration writes, killswitch state, calendar writes, role changes, and webhook delivery on the persistent bot host until their individual security contracts are approved.
3. Complete production opaque/encrypted OAuth sessions before moving any identity-bearing route to a Worker.
4. Inject a remote adapter into a separately testable Worker app only after route, template, static asset, environment, and failure-mode parity tests pass.

## Scope and reproducibility

Generated from source at `582e6aaad0c0b2766f05d2bad521fd3b557ec9ed`. Route call sites scanned in `web/dashboard.py`, `web/public.py`, `web/verify.py`, `web/rest_api.py`, `web/auth.py`, and `web/github_webhook.py`; declarations scanned in `web/bot_service.py`, `web/in_process_bot_service.py`, `web/http_bot_service.py`, and `cloudflare-spike/src/bot_service_client.py`.

This report intentionally counts declared public method names and route call sites; it does not inspect the internal endpoint's dispatch allowlist as a substitute for runtime tests. Regenerate after route or service contract changes.
