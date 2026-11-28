# Cloudflare dashboard appeals review contract

Status: proposed contract for the dashboard's moderator appeals queue. This document does not enable remote routing or change production behavior.

## Current call sites and data

The dashboard appeals page calls `api_submitted_appeals(guild_id)`. The current store query filters by the selected guild and `status="submitted"`, sorting newest first. The template displays case ID, sanction type, user name and ID, original sanction reason, and appeal text. Its accept/deny forms currently embed the appeal's database token in the URL.

The current token is also used by public appeal workflows. It is a sensitive lookup capability, not a harmless display field, and must not be copied into a remote dashboard DTO or exposed in page URLs as part of the Cloudflare migration.

## Read-only remote operation

- Method: `dashboard_submitted_appeals_snapshot`
- Arguments: exactly `[guild_id, limit]`.
- `guild_id`: decimal Discord snowflake string, 1–20 digits, integer range 1..2^64-1.
- `limit`: exact integer (booleans rejected), range 1..100.
- The internal signed endpoint must enforce the same validation before invoking the service.
- Store query must remain scoped by both guild ID and submitted status, ordered newest first, and bounded by limit.

## Response DTO

Return a list of objects with exactly these fields:

```json
[
  {
    "case_id": 123,
    "sanction_type": "ban",
    "user_name": "ExampleUser",
    "user_id": "123456789012345678",
    "ban_reason": "Original moderation reason",
    "appeal_text": "The user's appeal text",
    "submitted_at": "2026-11-28T09:30:00Z"
  }
]
```

Validation requirements:

- List length must not exceed the requested limit.
- Each object must contain exactly the seven fields above; reject missing or extra fields.
- `case_id`: exact positive integer, not bool, bounded to 1..2^63-1; unique in the returned list.
- `sanction_type`: normalized identifier matching `[a-z][a-z0-9_]{0,31}`.
- `user_name`: string, 1–100 characters.
- `user_id`: decimal Discord snowflake string, 1–20 digits, integer range 1..2^64-1.
- `ban_reason`: string, at most 1,000 characters; empty string allowed.
- `appeal_text`: string, 1–4,000 characters.
- `submitted_at`: serialize as ISO-8601 UTC with a trailing `Z`. MongoDB-naive datetimes are interpreted as UTC; reject other malformed timestamp values.
- Preserve newest-first ordering; timestamps must be non-increasing.

## Token and action security

- Never include `token`, `_id`, `guild_id`, internal processing metadata, or unrelated MongoDB fields in the DTO.
- Do not use the public appeal token as a dashboard route parameter, hidden form value, log field, or client-side identifier.
- Replace dashboard accept/deny addressing with guild-scoped case-ID operations. The internal service must atomically locate a submitted appeal using both the validated guild ID and case ID, transition it to processing, and return only the minimum fields needed for the action.
- Resolve/release operations must remain guild-scoped and bound to the same case ID; reject stale, already claimed, already resolved, or cross-guild requests.
- Recheck dashboard admin authorization server-side for every action. Keep CSRF protection on POST routes.
- Never trust case ID alone without the guild ID. Verify uniqueness of case IDs within a guild before relying on them as the action key; if that invariant is not guaranteed, introduce a separate random opaque dashboard action reference that is not the public appeal token.
- Do not log appeal text, original reasons, user IDs, or tokens in transport diagnostics.
- Fail closed on malformed internal records or transport failures.

## Compatibility and migration order

1. Add and test the read-only DTO snapshot first; retain the in-process implementation.
2. Introduce guild-scoped claim, release, and resolve operations addressed by case ID (or a dedicated opaque dashboard reference after confirming uniqueness).
3. Update dashboard routes and forms to remove the public token from browser-visible URLs and form values.
4. Only then enable remote routing for the appeals page. Do not send raw MongoDB documents to a Worker.

## Required tests

- Valid empty/populated DTO and UTC timestamp round-trip.
- Invalid guild IDs, limits, booleans, unexpected arguments, and kwargs.
- Wrong list length, missing/extra fields, duplicate case IDs, malformed types, invalid user IDs, oversized text, and invalid timestamps.
- Signed endpoint allowlist, guild-scoped query, submitted-only filtering, bounded newest-first ordering.
- Verify response contains no token, MongoDB `_id`, guild ID, or internal processing fields.
- Verify cross-guild, stale, duplicate-claim, and already-resolved actions fail closed.
- Verify dashboard POST actions remain admin-authorized and CSRF-protected.
