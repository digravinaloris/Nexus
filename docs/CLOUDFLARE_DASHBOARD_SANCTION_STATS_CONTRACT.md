# Cloudflare dashboard sanction statistics snapshot contract

## Purpose

Define the remote, read-only contract for `dashboard_sanction_stats_snapshot` before adding it to the signed BotService transport. The dashboard guild page currently requests a 30-day series and renders it as a sparkline.

## Existing behavior

The in-process adapter delegates to `get_sanction_stats(guild_id, days=30)`. The store aggregates sanction documents scoped to the supplied guild and returns one entry per UTC calendar day, including zero-count days. Each entry is currently a two-item tuple:

- display label formatted as `DD/MM`
- non-negative integer count

The dashboard uses `any(v for _, v in stats_series)`, builds the sparkline from the series, and uses the first and last labels as chart boundaries. The remote contract must preserve that behavior without transporting MongoDB documents or timestamps.

## Request contract

- Operation: `dashboard_sanction_stats_snapshot`
- Arguments: exactly `[guild_id, days]`
- `guild_id`: decimal Discord snowflake string, 1–20 digits, integer range 1 through `2^64 - 1`.
- `days`: exact JSON integer (booleans are not accepted), bounded from 1 through 30.
- No keyword arguments.
- The internal endpoint validates both arguments before calling the bot service.
- The HTTP adapter rejects invalid arguments locally and does not make a network request.

The dashboard currently requests 30 days. The explicit bounded argument keeps the operation reusable while preventing unbounded database scans and oversized responses.

## Response DTO

Return a JSON array with exactly `days` entries, oldest to newest:

```json
[
  {"label": "01/11", "count": 0},
  {"label": "02/11", "count": 3}
]
```

Each entry must have exactly these fields:

- `label`: non-empty string matching `^[0-3][0-9]/[0-1][0-9]$`, maximum 5 characters.
- `count`: exact integer (not boolean), minimum 0 and bounded to a defensible per-day maximum (for example 100000).

The array length must equal the requested `days` and must never exceed 30. Reject malformed entries, duplicate/unexpected fields, invalid labels, negative or non-integer counts, and mismatched lengths with a fail-closed service error. The adapter may convert the validated DTO back to the existing tuple representation at its public method boundary if required for dashboard compatibility.

## Security and privacy

- This is a read-only aggregate, scoped to one validated guild ID on the persistent bot service.
- Do not return user IDs, moderator IDs, reasons, case IDs, raw sanction documents, MongoDB metadata, or credentials.
- Do not accept a guild ID supplied by an untrusted browser without the existing server-side guild authorization checks.
- No mutation operations, raw configuration snapshots, OAuth/session secrets, or Discord objects cross this boundary.
- Keep the production dashboard on the in-process adapter until remote route parity and OAuth/session hardening gates are complete.

## Required tests before implementation

1. Valid 30-day series preserves oldest-to-newest order and tuple compatibility.
2. Valid shorter series returns exactly the requested number of entries.
3. Reject non-string, zero, negative, malformed, and out-of-range guild IDs without network calls.
4. Reject days values outside 1–30, strings, floats, and booleans.
5. Reject malformed DTOs: wrong length, missing/extra fields, invalid labels, boolean/negative/string counts, and oversized counts.
6. Verify signed endpoint dispatch accepts only this explicit read-only operation and rejects malformed arguments before service invocation.
7. Verify offline/malformed responses fail closed without leaking internal exception details.

## Scope

This PR documents the contract only. It does not change runtime behavior, dependencies, the production adapter, Cloudflare resources, DNS, secrets, or deployment configuration.
