# Cloudflare route-to-service inventory (2026-11-28)

Audit baseline: `main` after PR #364 (merge commit `55a8e916715cb63a23ad03fb70a9344f1d66a163`).

This is a source-level inventory of the six Flask route modules and the current service/transport declarations. It is not a cutover-readiness certification. Counts describe method names found in the current source, not proven behavioral parity.

## Executive summary

- `web/bot_service.py` declares 81 BotService operations (excluding the module-level `get_bot_service()` accessor).
- The six route modules below reference 78 distinct BotService operations.
- `InProcessBotService` implements the full interface and remains the active production adapter.
- `SignedHttpBotService` declares 31 interface operations.
- The internal signed dispatch endpoint explicitly allows 35 operation names. The endpoint remains opt-in and disabled by default.
- `CloudflareFetchBotService` currently declares 31 public interface operations (excluding private helpers). It is a Worker-runtime proof client, not wired into the production Flask app.
- A method being present in a client or endpoint does not by itself prove equivalent authorization, error mapping, data shape, mutation idempotency, or route behavior.

## Route usage by module

### `web/dashboard.py` — 35 operations

`available_guild_ids`, `is_guild_owner`, `guild_name`, `api_health_snapshot`, `dashboard_config_snapshot`, `api_update_config`, `api_audit_snapshot`, `api_sanction_cases_snapshot`, `api_record_audit`, `dashboard_submitted_appeals_snapshot`, `api_claim_appeal_by_case`, `api_release_appeal_claim_by_case`, `api_resolve_appeal_by_case`, `dashboard_presence_ping`, `dashboard_presence_viewers`, `dashboard_add_command_role`, `dashboard_remove_command_role`, `dashboard_calendar_has_duplicate_event`, `dashboard_calendar_insert_event`, `dashboard_calendar_delete_event`, `dashboard_calendar_upcoming_events`, `dashboard_audit_export_records`, `dashboard_public_templates_snapshot`, `dashboard_template_snapshot`, `dashboard_apply_template_snapshot`, `set_guild_locked`, `api_accept_appeal`, `api_notify_appeal_result`, `api_announce_calendar_event`, `api_log_dashboard_action`.

### `web/public.py` — 14 operations

`health_snapshot`, `public_status_snapshot`, `guild_name`, `api_record_audit`, `api_get_appeal`, `public_get_killswitch_token`, `public_increment_killswitch_attempts`, `public_mark_killswitch_token_used`, `public_global_lock_state`, `public_set_global_lock`, `public_submit_ban_appeal`, `api_post_appeal_for_review`, `notify_owner_bruteforce`, `notify_owner_killswitch`.

### `web/verify.py` — 6 operations

`guild_name`, `api_record_audit`, `verify_get_valid_challenge`, `verify_record_failed_attempt`, `verify_mark_used`, `api_grant_verified`.

### `web/rest_api.py` — 34 operations

`guild_presence`, `api_authenticate_key`, `api_health_snapshot`, `api_rest_config_snapshot`, `api_update_config`, `api_faq_snapshot`, `api_faq_add_entry`, `api_faq_remove_entry`, `api_faq_set_enabled`, `api_audit_snapshot`, `api_sanction_cases_snapshot`, `api_record_audit`, `api_member_warnings`, `api_warnlist_snapshot`, `api_submitted_appeals`, `api_get_appeal`, `api_claim_appeal`, `api_release_appeal_claim`, `api_resolve_appeal`, `api_guild_list_snapshot`, `api_text_channels_snapshot`, `api_roles_snapshot`, `api_members_snapshot`, `set_guild_locked`, `api_guild_stats_snapshot`, `api_unmute_member`, `api_broadcast`, `api_mute_member`, `api_ban_member`, `api_kick_member`, `api_warn_member`, `api_unwarn_member`, `api_accept_appeal`, `api_notify_appeal_result`.

### `web/auth.py` — 1 operation

`guild_presence`.

### `web/github_webhook.py` — 1 operation

`api_handle_github_release`.

## Current remote surfaces

### Signed internal endpoint — 35 explicitly dispatched operation names

- Base snapshots: `health_snapshot`, `public_status_snapshot`.
- Public state: `public_global_lock_state`.
- Identity/authorization: `is_guild_owner`, `is_guild_admin`, `api_authenticate_key`, `available_guild_ids`, `api_guild_id_candidates`, `guild_presence`, `guild_name`.
- REST reads: `api_health_snapshot`, `api_guild_list_snapshot`, `api_text_channels_snapshot`, `api_roles_snapshot`, `api_faq_snapshot`, `api_members_snapshot`, `api_rest_config_snapshot`, `api_guild_stats_snapshot`, `api_member_warnings`, `api_warnlist_snapshot`, `api_audit_snapshot`, `api_sanction_cases_snapshot`.
- Dashboard reads: `dashboard_config_snapshot`, `dashboard_calendar_upcoming_events`, `dashboard_submitted_appeals_snapshot`, `dashboard_sanction_stats_snapshot`, `dashboard_sanction_types_snapshot`, `dashboard_roles_snapshot`, `dashboard_settings_snapshot`, `dashboard_overview_snapshot`.
- Case-ID appeal actions: `api_claim_appeal_by_case`, `api_release_appeal_claim_by_case`, `api_resolve_appeal_by_case`.

All other method names are rejected. Raw `api_config_snapshot` is intentionally not exposed.

### Signed HTTP client — 31 declared interface operations

`health_snapshot`, `public_status_snapshot`, `available_guild_ids`, `is_guild_owner`, `is_guild_admin`, `guild_presence`, `guild_name`, `api_text_channels_snapshot`, `api_roles_snapshot`, `api_faq_snapshot`, `api_members_snapshot`, `api_guild_list_snapshot`, `dashboard_sanction_stats_snapshot`, `dashboard_sanction_types_snapshot`, `api_rest_config_snapshot`, `dashboard_config_snapshot`, `dashboard_calendar_upcoming_events`, `api_authenticate_key`, `api_member_warnings`, `api_warnlist_snapshot`, `api_audit_snapshot`, `dashboard_submitted_appeals_snapshot`, `api_claim_appeal_by_case`, `api_release_appeal_claim_by_case`, `api_resolve_appeal_by_case`, `api_sanction_cases_snapshot`, `dashboard_roles_snapshot`, `dashboard_settings_snapshot`, `dashboard_overview_snapshot`, `api_guild_stats_snapshot`, `api_health_snapshot`.

### Cloudflare Worker Fetch client — 31 public interface operations

`health_snapshot`, `public_status_snapshot`, `is_guild_owner`, `is_guild_admin`, `guild_presence`, `guild_name`, `api_health_snapshot`, `api_guild_list_snapshot`, `api_guild_id_candidates`, `available_guild_ids`, `api_text_channels_snapshot`, `api_roles_snapshot`, `api_guild_stats_snapshot`, `api_faq_snapshot`, `api_members_snapshot`, `api_rest_config_snapshot`, `dashboard_config_snapshot`, `dashboard_public_templates_snapshot`, `dashboard_submitted_appeals_snapshot`, `api_member_warnings`, `api_warnlist_snapshot`, `api_sanction_cases_snapshot`, `api_audit_snapshot`, `dashboard_calendar_upcoming_events`, `dashboard_overview_snapshot`, `dashboard_sanction_stats_snapshot`, `dashboard_sanction_types_snapshot`, `dashboard_roles_snapshot`, `dashboard_settings_snapshot`, `public_global_lock_state`.

The Worker Fetch client currently overlaps directly with 29 route-used operations. `api_guild_id_candidates` and `is_guild_admin` are additional service capabilities not directly called by the six route modules. This overlap is a method-name comparison only; it is not full route parity.

## Route-used operations not yet declared by the Worker Fetch client

The source-level scan currently finds **49** distinct route-used method names absent from the Worker Fetch client. Each needs an explicit decision: add a reviewed DTO/action contract, retain it on the persistent Flask/bot host, or remove it only through a separate product decision.

- `api_accept_appeal`
- `api_announce_calendar_event`
- `api_authenticate_key`
- `api_ban_member`
- `api_broadcast`
- `api_claim_appeal`
- `api_claim_appeal_by_case`
- `api_faq_add_entry`
- `api_faq_remove_entry`
- `api_faq_set_enabled`
- `api_get_appeal`
- `api_grant_verified`
- `api_handle_github_release`
- `api_kick_member`
- `api_log_dashboard_action`
- `api_mute_member`
- `api_notify_appeal_result`
- `api_post_appeal_for_review`
- `api_record_audit`
- `api_release_appeal_claim`
- `api_release_appeal_claim_by_case`
- `api_resolve_appeal`
- `api_resolve_appeal_by_case`
- `api_submitted_appeals`
- `api_unmute_member`
- `api_unwarn_member`
- `api_update_config`
- `api_warn_member`
- `dashboard_add_command_role`
- `dashboard_apply_template_snapshot`
- `dashboard_audit_export_records`
- `dashboard_calendar_delete_event`
- `dashboard_calendar_insert_event`
- `dashboard_presence_ping`
- `dashboard_presence_viewers`
- `dashboard_remove_command_role`
- `dashboard_template_snapshot`
- `notify_owner_bruteforce`
- `notify_owner_killswitch`
- `public_get_killswitch_token`
- `public_increment_killswitch_attempts`
- `public_mark_killswitch_token_used`
- `public_set_global_lock`
- `public_submit_ban_appeal`
- `set_guild_locked`
- `verify_get_valid_challenge`
- `verify_mark_used`
- `verify_record_failed_attempt`

This is a method-name comparison, not a cutover-readiness metric. Mutations and security-sensitive public flows require operation-specific threat modelling, actor identity, bot-side authorization, idempotency, audit logging, bounded schemas, and stable errors. Some methods may intentionally remain on the persistent bot host rather than move to Cloudflare.

## Recommended next sequence

1. **Continue the remaining low-risk Worker read gap** only after reviewing each source DTO. Audit, sanction-case, submitted-appeal, calendar, and public-template snapshots are now represented in the Worker Fetch client; do not infer route parity from this list.
2. **Do not blindly port mutations.** Dashboard configuration writes, role changes, calendar writes, moderation, appeal workflows, verification grants, killswitch state and webhook delivery need operation-specific authorization and idempotency review.
3. **Implement production server-side OAuth sessions** before any production Worker route uses Discord identity. The current Flask browser cookie still contains a readable access token.
4. **Only after the above**, inject the remote adapter into a separately testable Worker app and prove complete route/template/static asset parity.

## Scope and reproducibility

This inventory was refreshed from current `main` at merge commit `55a8e916715cb63a23ad03fb70a9344f1d66a163`, reviewing:
- Route call sites: `web/dashboard.py`, `web/public.py`, `web/verify.py`, `web/rest_api.py`, `web/auth.py`, `web/github_webhook.py`.
- Interface and implementation declarations: `web/bot_service.py`, `web/in_process_bot_service.py`, `web/http_bot_service.py`, `web/internal_bot_service.py`.
- Worker transport declarations: `cloudflare-spike/src/bot_service_client.py`.

The inventory should be regenerated after route or BotService contract changes. It does not replace tests or security review.
