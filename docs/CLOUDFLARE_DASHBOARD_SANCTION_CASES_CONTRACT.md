# Cloudflare dashboard sanction cases snapshot contract

Status: proposed contract for the read-only dashboard cases page. This document does not enable the remote adapter or change production routing.

## Current call site

`web/dashboard.py` calls:

```python
api_sanction_cases_snapshot(guild_id, filter_type, 200)
dashboard_sanction_types_snapshot(guild_id)
```

The cases template currently displays case number, sanction type, timestamp in UTC, target user ID, and reason. The query is scoped to the selected guild and sorted newest first in the store.

## Remote operation

- Method: `api_sanction_cases_snapshot`
- Arguments: exactly `[guild_id, filter_type, limit]`; no keyword arguments.
- `guild_id`: decimal Discord snowflake string, 1–20 digits, integer range 1..2^64-1.
- `filter_type`: string, empty for all types or a known normalized sanction type matching `[a-z][a-z0-9_]{0,31}`.
- `limit`: exact integer (booleans rejected), range 1..200. Dashboard call uses 200.
- The internal endpoint must enforce the same validation before invoking the service.

## Response DTO

Return a list of objects with exactly these fields:

```json
[
  {
    "case_id": 123,
    "type": "ban",
    "timestamp": "2026-11-28T09:30:00Z",
    "user_id": "123456789012345678",
    "reason": "Example reason"
  }
]
```

Validation requirements:

- List length must not exceed the requested limit.
- Each item must be an object with exactly `case_id`, `type`, `timestamp`, `user_id`, and `reason`.
- `case_id`: exact positive integer, not bool, bounded to 1..2^63-1.
- `type`: normalized identifier matching `[a-z][a-z0-9_]{0,31}`.
- `timestamp`: serialize as ISO-8601 UTC with a trailing `Z`. MongoDB-naive datetimes are interpreted as UTC (the driver's default decode behavior); reject other malformed timestamp values.
- `user_id`: decimal snowflake string, 1–20 digits, integer range 1..2^64-1.
- `reason`: string, bounded to 1,000 characters; empty string is allowed when absent.
- Reject duplicate case IDs and any extra fields.
- Preserve newest-first ordering; timestamps must be non-increasing.

## Privacy and security

- This is a guild-admin-only view; keep every query scoped by the validated guild ID.
- Do not return MongoDB documents, `_id`, moderator IDs/names, raw metadata, tokens, or unrelated fields.
- User ID and reason are included only because the existing cases page explicitly displays them to authorized guild administrators. Keep the reason length bounded and rely on template autoescaping; never mark it safe HTML.
- Do not log case reasons or user IDs in transport diagnostics.
- Fail closed with the existing unavailable response on malformed internal data or transport failure.

## Compatibility

The public service method should keep its existing signature and defaults where possible:

```python
api_sanction_cases_snapshot(guild_id, filter_type="", limit=50)
```

The HTTP adapter should validate inputs, dispatch the three positional arguments, validate the JSON DTO, and convert each timestamp back to an aware UTC `datetime` so the current Jinja template remains compatible. The in-process adapter may continue returning its existing store representation.

## Required tests

- Valid empty and populated series, including UTC timestamp round-trip.
- Invalid guild IDs, filter types, limits, booleans, and unexpected kwargs.
- Wrong list length, extra/missing fields, duplicate case IDs, invalid/naive timestamps, invalid user IDs, malformed types, and overlong reasons.
- Signed endpoint allowlist and rejection of invalid arguments before service invocation.
- Verify the store query remains guild-scoped, bounded, and newest-first.
- Verify failures do not fall back to local access or leak case data.
