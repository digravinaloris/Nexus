# Cloudflare migration: route-to-service contract inventory

Audit date: 2026-11-27  
Baseline: `main` at `6877836060cc57f27e63a41c7b72861e1911615f`

## Executive summary

The Flask route layer currently calls **72 distinct BotService methods** across 6 route modules. The service interface declares **73 methods**. The in-process adapter implements the full route-facing surface, while the remote transport is materially incomplete: only **2 of 72 route-used methods** have matching methods in the current internal/HTTP adapter code.

This is an inventory of method names and call sites, not proof that each method's DTO, authorization, error semantics, or side effects are safe for remote execution. The next implementation step should be a read-only contract slice, not a bulk remote adapter port.

## Route usage by module

### `web/dashboard.py` (36 methods)

- `api_accept_appeal`
- `api_announce_calendar_event`
- `api_audit_snapshot`
- `api_claim_appeal`
- `api_config_snapshot`
- `api_get_appeal`
- `api_health_snapshot`
- `api_log_dashboard_action`
- `api_notify_appeal_result`
- `api_record_audit`
- `api_release_appeal_claim`
- `api_resolve_appeal`
- `api_sanction_cases_snapshot`
- `api_submitted_appeals`
- `api_update_config`
- `available_guild_ids`
- `dashboard_add_command_role`
- `dashboard_apply_template_snapshot`
- `dashboard_audit_export_records`
- `dashboard_calendar_delete_event`
- `dashboard_calendar_has_duplicate_event`
- `dashboard_calendar_insert_event`
- `dashboard_calendar_upcoming_events`
- `dashboard_overview_snapshot`
- `dashboard_presence_ping`
- `dashboard_presence_viewers`
- `dashboard_public_templates_snapshot`
- `dashboard_remove_command_role`
- `dashboard_roles_snapshot`
- `dashboard_sanction_stats_snapshot`
- `dashboard_sanction_types_snapshot`
- `dashboard_settings_snapshot`
- `dashboard_template_snapshot`
- `guild_name`
- `is_guild_owner`
- `set_guild_locked`

### `web/public.py` (14 methods)

- `api_get_appeal`
- `api_post_appeal_for_review`
- `api_record_audit`
- `guild_name`
- `health_snapshot`
- `notify_owner_bruteforce`
- `notify_owner_killswitch`
- `public_get_killswitch_token`
- `public_global_lock_state`
- `public_increment_killswitch_attempts`
- `public_mark_killswitch_token_used`
- `public_set_global_lock`
- `public_status_snapshot`
- `public_submit_ban_appeal`

### `web/verify.py` (6 methods)

- `api_grant_verified`
- `api_record_audit`
- `guild_name`
- `verify_get_valid_challenge`
- `verify_mark_used`
- `verify_record_failed_attempt`

### `web/rest_api.py` (34 methods)

- `api_accept_appeal`
- `api_audit_snapshot`
- `api_ban_member`
- `api_broadcast`
- `api_claim_appeal`
- `api_config_snapshot`
- `api_faq_add_entry`
- `api_faq_remove_entry`
- `api_faq_set_enabled`
- `api_faq_snapshot`
- `api_get_appeal`
- `api_guild_id_candidates`
- `api_guild_list_snapshot`
- `api_guild_stats_snapshot`
- `api_health_snapshot`
- `api_kick_member`
- `api_member_warnings`
- `api_members_snapshot`
- `api_mute_member`
- `api_notify_appeal_result`
- `api_record_audit`
- `api_release_appeal_claim`
- `api_resolve_appeal`
- `api_roles_snapshot`
- `api_sanction_cases_snapshot`
- `api_submitted_appeals`
- `api_text_channels_snapshot`
- `api_unmute_member`
- `api_unwarn_member`
- `api_update_config`
- `api_warn_member`
- `api_warnlist_snapshot`
- `guild_presence`
- `set_guild_locked`

### `web/auth.py` (1 methods)

- `guild_presence`

### `web/github_webhook.py` (1 methods)

- `api_handle_github_release`

## Adapter implementation coverage

| Adapter | Route-used methods implemented | Missing route-used methods |
|---|---:|---:|
| In-process adapter | 72/72 | 0 |
| Internal signed endpoint | 0/72 | 72 |
| HTTP client | 2/72 | 70 |
| Either remote layer (union) | 2/72 | 70 |

### Methods declared but not directly called by route modules

- `is_guild_admin`

### Route-used methods not yet represented by either remote adapter

- `guild_presence`
- `available_guild_ids`
- `api_guild_id_candidates`
- `is_guild_owner`
- `guild_name`
- `api_health_snapshot`
- `api_config_snapshot`
- `api_update_config`
- `api_faq_snapshot`
- `api_faq_add_entry`
- `api_faq_remove_entry`
- `api_faq_set_enabled`
- `api_audit_snapshot`
- `api_sanction_cases_snapshot`
- `api_record_audit`
- `api_member_warnings`
- `api_warnlist_snapshot`
- `api_submitted_appeals`
- `api_get_appeal`
- `public_get_killswitch_token`
- `public_increment_killswitch_attempts`
- `public_mark_killswitch_token_used`
- `public_global_lock_state`
- `public_set_global_lock`
- `public_submit_ban_appeal`
- `verify_get_valid_challenge`
- `verify_record_failed_attempt`
- `verify_mark_used`
- `api_claim_appeal`
- `api_release_appeal_claim`
- `api_resolve_appeal`
- `api_guild_list_snapshot`
- `api_text_channels_snapshot`
- `api_roles_snapshot`
- `dashboard_presence_ping`
- `dashboard_presence_viewers`
- `dashboard_settings_snapshot`
- `dashboard_roles_snapshot`
- `dashboard_overview_snapshot`
- `dashboard_add_command_role`
- `dashboard_remove_command_role`
- `dashboard_sanction_stats_snapshot`
- `dashboard_sanction_types_snapshot`
- `dashboard_calendar_has_duplicate_event`
- `dashboard_calendar_insert_event`
- `dashboard_calendar_delete_event`
- `dashboard_calendar_upcoming_events`
- `dashboard_audit_export_records`
- `dashboard_public_templates_snapshot`
- `dashboard_template_snapshot`
- `dashboard_apply_template_snapshot`
- `api_handle_github_release`
- `api_members_snapshot`
- `set_guild_locked`
- `api_guild_stats_snapshot`
- `api_unmute_member`
- `api_broadcast`
- `api_mute_member`
- `api_ban_member`
- `api_kick_member`
- `api_warn_member`
- `api_unwarn_member`
- `api_accept_appeal`
- `api_notify_appeal_result`
- `api_announce_calendar_event`
- `api_grant_verified`
- `api_log_dashboard_action`
- `api_post_appeal_for_review`
- `notify_owner_bruteforce`
- `notify_owner_killswitch`

## Recommended transport sequencing

1. **Read-only, low-risk snapshots:** health/public status, guild presence/name, guild lists, text channels, roles, dashboard overview/settings, FAQ and audit snapshots. Keep responses to explicit JSON DTOs; never serialize Discord objects or raw MongoDB documents.
2. **Read-only sensitive data:** appeals, sanction cases, member/warning lists, audit exports. Require the same server-side user/guild authorization as the existing route and enforce guild scoping in the bot service, not just in Worker input validation.
3. **Idempotent persistence operations:** configuration updates, FAQ edits, calendar CRUD, appeal claim/release/resolve, and template application. Add operation IDs/idempotency keys, validation, audit events, bounded payloads, and stable conflict errors.
4. **Discord side effects:** moderation actions, broadcasts, verification role grants, notifications, and calendar announcements. Execute only on the persistent bot service, re-check actor permissions and bot permissions at execution time, and record an audit event.
5. **Kill-switch and public security flows:** retain atomic token consumption, rate limits, fail-closed behavior, and owner notification semantics. Do not expose generic arbitrary method dispatch.

## Contract safety requirements

- Use an explicit allowlist of operation names and versioned request/response DTOs; no `getattr`-based arbitrary dispatch.
- Authenticate Worker-to-bot requests with timestamped signatures, replay protection, bounded body size, and key rotation.
- Bind authenticated user identity to the request context; the bot service must independently authorize every guild-scoped read and mutation.
- Apply idempotency to retried mutations and return stable error codes for conflict, forbidden, unavailable, and invalid input.
- Never pass raw Flask session cookies, Discord OAuth tokens, API keys, MongoDB documents, or Discord runtime objects through the internal transport.
- Add contract tests for success, invalid DTO, expired signature, replay, unauthorized guild, bot offline, duplicate operation, and partial Discord failure.

## Scope and limitations

- Documentation-only inventory; no runtime behavior or dependencies changed.
- Method presence is detected from Python definitions and route call sites in the audited files. It does not assert that every remote operation is wired through the Worker spike or production routes.
- This inventory does not replace an endpoint-by-endpoint authorization review or a DTO schema review.

## Files reviewed

- `web/bot_service.py`
- `web/dashboard.py`
- `web/public.py`
- `web/verify.py`
- `web/rest_api.py`
- `web/auth.py`
- `web/github_webhook.py`
- `web/internal_bot_service.py`
- `web/http_bot_service.py`
- `web/in_process_bot_service.py`
