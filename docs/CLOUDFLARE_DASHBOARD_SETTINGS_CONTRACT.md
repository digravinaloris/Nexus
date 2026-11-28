# Dashboard settings snapshot contract

## Purpose

Define the next read-only BotService transport slice for the dashboard settings pages. This contract is preparation only: it does not switch the production dashboard to a remote adapter, change the current Flask startup path, or authorize deployment.

The existing `InProcessBotService.dashboard_settings_snapshot(guild_id)` returns a compact guild DTO used by dashboard settings pages. It is a better next transport slice than configuration documents: it contains Discord-derived selection data and does not require exposing MongoDB configuration.

## Request

- Operation: `dashboard_settings_snapshot`
- Arguments: exactly one string `guild_id`.
- `guild_id` must be a positive Discord snowflake represented as 1–20 decimal digits and bounded by unsigned 64-bit range.
- No keyword arguments.

## Response DTO

Return `null` when the bot is offline or the guild is not present. Otherwise return exactly:

```json
{
  "id": "123456789012345678",
  "name": "Nexus",
  "owner_id": "234567890123456789",
  "text_channels": [{"id": "345678901234567890", "name": "general"}],
  "voice_channels": [{"id": "456789012345678901", "name": "General"}],
  "roles": [{"id": "567890123456789012", "name": "Moderator"}]
}
```

### Validation

- The top-level object must contain exactly `id`, `name`, `owner_id`, `text_channels`, `voice_channels`, and `roles`.
- Guild and owner IDs, plus every channel and role ID, must be valid positive unsigned 64-bit decimal snowflakes.
- Guild name is a non-empty string of at most 256 characters.
- Channel and role names are non-empty strings of at most 100 characters.
- Each of the three arrays is a list of at most 500 entries.
- Each channel/role entry must contain exactly `id` and `name`.
- Reject booleans, numbers, malformed IDs, extra fields, oversized arrays, and malformed nested values. Do not silently coerce values.

## Data minimization and security

- Never serialize Discord guild, channel, role, member, or permission objects directly.
- Do not include raw MongoDB configuration, API keys, webhook URLs, OAuth tokens, cookies, CSRF material, or internal secrets.
- The operation is read-only and must remain on the explicit internal operation allowlist.
- The bot service remains authoritative for Discord cache lookup. A missing guild returns `null`; it must not fall back to a global channel or role listing.
- Preserve signed request authentication, timestamp checks, one-time nonce consumption, response-size limits, and fail-closed behavior.
- Keep this operation separate from `dashboard_overview_snapshot`: the latter includes permission flags and ticket-category channels for the overview page and has a distinct DTO.

## Implementation and test plan

1. Add the operation to the internal signed endpoint allowlist with exactly one validated guild ID.
2. Add the HTTP adapter wrapper and validate the response DTO independently on the client.
3. Add positive tests for empty and populated snapshots, plus malformed top-level keys, invalid IDs, invalid names, wrong field types, and arrays over 500 entries.
4. Verify offline/missing-guild `null` behavior and reject unexpected arguments/kwargs.
5. Keep all dashboard routes on the in-process adapter until production OAuth session hardening, remote authorization parity, and route/Worker compatibility gates are complete.

## Explicit non-goals

- No remote configuration read or write.
- No dashboard POST/action transport.
- No production adapter switch.
- No Cloudflare resource creation, secrets, DNS, deployment, or traffic cutover.
