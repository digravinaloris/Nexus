# Cloudflare web-to-bot service contract

Status: proposal for the full-site Cloudflare migration. This document defines the boundary between the stateless web runtime (Cloudflare Pages + Python Worker) and the long-lived Nexus Discord bot service. It does not implement or enable a network endpoint.

## Architecture boundary

```text
Browser
  |
  v
Cloudflare Pages (static assets)
  |
  v
Cloudflare Python Worker (Flask-compatible web app)
  |  signed, bounded HTTPS requests
  v
Nexus Bot Service (persistent host; Discord Gateway + bot event loop)
  |
  +--> Discord API / Gateway
  |
  +--> MongoDB Atlas (initially remains owned here)
```

The Worker must not import `discord.py`, `main`, `core`, `bridge`, or any module that opens a MongoDB client. The bot service must not trust a request merely because it came from Cloudflare.

## Service API principles

- Use a dedicated versioned internal prefix, e.g. `/internal/v1`; do not expose internal routes through the public mobile API.
- JSON request and response bodies only, with strict schema validation and small per-operation size limits.
- HTTPS only. Authenticate each request with HMAC-SHA256 over the method, canonical path, timestamp, unique nonce and SHA-256 body digest.
- Enforce a narrow timestamp tolerance and persist nonce consumption for replay protection. Reject missing, malformed, stale, duplicated or invalid signatures.
- Keep the signing secret in Worker secrets and bot-host environment secrets; never in source, Pages variables exposed to clients, HTML, logs or mobile bundles.
- Use explicit per-operation scopes and guild authorization. Re-check the user's Discord identity and Administrator/manage permissions in the bot service for every privileged action.
- Apply per-route timeouts, bounded retries only for safe reads, idempotency keys for mutations, and structured audit logging with secrets and tokens redacted.
- Never accept raw MongoDB collection names, filters, update operators, Discord tokens or arbitrary Discord API paths from the Worker.
- Return stable error envelopes and avoid leaking stack traces, internal hostnames, tokens or private Discord data.

## Proposed operation groups

| Group | Example operations | Security boundary |
|---|---|---|
| Identity and authorization | Resolve current user; list guilds the user may administer; verify guild access | User identity must originate from a validated OAuth session, not a client-supplied user ID. |
| Guild read model | Guild summary, channels, roles, member summary, presence | Return only fields required by the dashboard; paginate and cap results. |
| Configuration | Read/update guild settings, feature flags, AutoMod and welcome configuration | Validate allow-listed keys and value schemas; enforce guild-level admin permission. |
| Moderation | Ban, kick, mute/unmute, warn/unwarn, lock/unlock, broadcast | Strong authorization, reason bounds, idempotency key, audit record and Discord result mapping. |
| Cases and appeals | List cases, inspect appeal, accept/deny appeal | Preserve existing atomic appeal-claim behavior; never weaken race protection. |
| Verification | Validate challenge and grant configured role | Single-use challenge, expiry, guild/member binding and role hierarchy checks. |
| Dashboard support data | Templates, FAQ, calendar, audit exports and statistics | Separate read/write scopes; bounded exports and pagination. |
| Webhook dispatch | Validated GitHub release event notification | Verify GitHub signature at ingress; use idempotent event delivery to bot service. |

The examples are conceptual. Final operation names and payloads must be derived from current handler behavior and existing tests before implementation.

## Authentication and authorization flow

1. Browser authenticates with Discord OAuth through the Worker. OAuth state is random, single-use, short-lived and bound to the browser session.
2. The Worker exchanges the authorization code server-side and stores tokens only in a server-side session store; do not put access or refresh tokens in browser-readable storage.
3. For each dashboard request, the Worker resolves the user and requested guild, then calls the bot service using a signed internal request.
4. The bot service validates the Worker signature, timestamp and nonce; resolves the Discord user and guild membership; checks the required permission; and performs the operation.
5. The bot service returns a minimal typed response. The Worker maps it to the existing HTML/JSON response contract.
6. Session rotation, logout invalidation, CSRF, secure cookies, rate limits and no-store behavior remain mandatory.

Do not assume Cloudflare Worker memory is durable or shared across isolates. Session and replay state must use an explicitly selected durable store with documented consistency and TTL semantics.

## Compatibility and failure behavior

- Bot service unavailable: return a controlled 503 response; never silently report a moderation action as successful.
- Discord permission missing or role hierarchy conflict: return a stable 403/409-style error with a safe user-facing message.
- Timeout after a mutation: use an idempotency key and operation-status lookup before retrying.
- Invalid signature, replay or expired timestamp: reject and emit a security event without logging secrets.
- MongoDB outage: bot service returns a controlled unavailable response; Worker does not attempt a fallback direct database connection.
- Partial dashboard data: preserve explicit unavailable/error states rather than returning misleading empty configuration.

## Contract tests required before migration

- Valid signature; invalid signature; altered body; altered path/method; expired/future timestamp; nonce replay.
- Missing/unknown scope; wrong guild; non-admin; user no longer in guild; bot absent from guild.
- Payload schema violations, extra fields, oversized body and invalid IDs.
- Discord timeout, rate limit, missing permissions, role hierarchy, bot offline and service unavailable.
- Mutation idempotency after client retry and ambiguous timeout.
- Appeal accept/deny races and rollback behavior.
- Response schema and status-code parity with the existing dashboard and mobile API.
- Secret/token redaction in logs and error responses.

## Rollout constraints

- Keep current Flask + bot process as the production source of truth until all contracts and parity tests pass.
- Introduce the internal service behind disabled-by-default configuration; no public traffic should call it until explicitly enabled.
- Do not change OAuth redirect URIs, DNS, production secrets, database ownership or host configuration in these PRs.
- Production cutover, Cloudflare resource creation and DNS changes require separate explicit approval.
