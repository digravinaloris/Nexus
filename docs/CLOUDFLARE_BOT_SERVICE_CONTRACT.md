# Cloudflare web-to-bot service contract

Status: architecture contract for the full-site Cloudflare migration. The isolated Flask/Python Worker, D1 and Web Crypto primitives have now been validated in CI; the production session store and signed network endpoint are not implemented or enabled.

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

## Verified implementation progress (2026-11-27)

The following PRs validate isolated building blocks. The internal endpoint remains disabled unless an operator explicitly enables its environment flag; none of these PRs deploys Cloudflare resources or changes production hosting configuration.

- [#196](https://github.com/digravinaloris/Nexus/pull/196): Flask WSGI startup, local D1 prepared statements, concurrent one-time state consumption, session rotation and atomic batch rollback in the actual Python Workers runtime.
- [#197](https://github.com/digravinaloris/Nexus/pull/197): Web Crypto SHA-256 and AES-256-GCM through Python FFI, including tamper, wrong-key and wrong-AAD rejection.
- [#198](https://github.com/digravinaloris/Nexus/pull/198): encrypted D1 fixture persistence across requests, digest-only session lookup keys and fail-closed ciphertext tamper handling.
- [#199](https://github.com/digravinaloris/Nexus/pull/199): versioned v1-to-v2 key rotation, key-version-bound AAD, idle/absolute expiry and immediate revocation.
- [#200](https://github.com/digravinaloris/Nexus/pull/200): standalone `web.app_factory.create_web_app()`, importable without `main.py`, discord.py or PyMongo.
- [#201](https://github.com/digravinaloris/Nexus/pull/201): app-scoped BotService injection while preserving the existing in-process fallback.
- [#203](https://github.com/digravinaloris/Nexus/pull/203): versioned HMAC-SHA256 signing primitives with timestamp, body/path binding and key rotation.
- [#204](https://github.com/digravinaloris/Nexus/pull/204): signed internal dispatch with durable Mongo nonce replay protection.
- [#205](https://github.com/digravinaloris/Nexus/pull/205): explicit all-or-nothing app-factory registration for the internal endpoint.
- [#206](https://github.com/digravinaloris/Nexus/pull/206): partial signed HTTP client for the read-only health snapshot.
- [#207](https://github.com/digravinaloris/Nexus/pull/207): strict environment keyring parsing and disabled-by-default endpoint configuration in `main.py`.
- [#208](https://github.com/digravinaloris/Nexus/pull/208): adds the read-only public status snapshot to the signed client and endpoint.
- [#209](https://github.com/digravinaloris/Nexus/pull/209): synchronizes the migration checkpoint and signed BotService status.
- [#210](https://github.com/digravinaloris/Nexus/pull/210): validates the Workers Fetch FFI transport against a local signed mock.
- [#211](https://github.com/digravinaloris/Nexus/pull/211): adds fail-closed configuration validation for the Worker Fetch client.
- [#212](https://github.com/digravinaloris/Nexus/pull/212): verifies redirect, non-200, malformed JSON and oversized-response rejection in the Worker runtime.

Still not implemented: production OAuth server-side sessions, production HMAC key provisioning/rotation, full BotService method parity, complete route parity on Workers, and Cloudflare resource creation or deployment. The local D1 spike uses disposable fixtures and a local database only.

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

## Current transport proof (2026-11-27)

- PR #203 adds the versioned HMAC-SHA256 helper in `web.bot_service_signing`. The canonical message binds `NEXUS-BOT-SERVICE-V1`, key ID, POST method, path, timestamp, nonce and SHA-256 digest of the exact body. Headers are `X-Nexus-Key-Id`, `X-Nexus-Timestamp`, `X-Nexus-Nonce` and `X-Nexus-Signature`; timestamp skew is limited to 60 seconds and bodies to 1 MiB.
- PR #204 adds `POST /internal/v1/dispatch`, verifies signatures before parsing, and consumes the nonce before dispatch. `MongoNonceStore` uses a unique compound `(key_id, nonce)` index and a TTL index on `expires_at`.
- PR #205 makes app-factory registration all-or-nothing: a service, keyring and nonce store must all be explicitly supplied.
- PR #207 adds the opt-in `NEXUS_BOT_SERVICE_INTERNAL_ENABLED=true` gate and strict `NEXUS_BOT_SERVICE_HMAC_KEYS` parser. When enabled, `main.py` injects the in-process BotService and a lazy Mongo nonce store; missing or malformed key material fails startup closed. With the flag unset, the endpoint is not registered.
- The endpoint allowlist exposes read-only `health_snapshot`, `public_status_snapshot`, `guild_presence`, `guild_name`, `api_health_snapshot`, `api_guild_list_snapshot`, `api_guild_id_candidates`, `available_guild_ids`, `api_text_channels_snapshot`, `api_roles_snapshot`, `api_guild_stats_snapshot`, `api_faq_snapshot`, and `api_members_snapshot` methods. PR #213 adds the narrowly scoped `is_guild_owner(guild_id, user_id)` query, and PR #214 adds `is_guild_admin(guild_id, user_id)` for owner-or-Administrator checks; both require bounded decimal IDs and reject arbitrary kwargs or argument shapes. PR #246 extends the Worker Fetch client with the same bounded read-only operations; PRs #247-#248 add text-channel snapshots; PR #249 adds role snapshots; PR #250 adds a bounded guild-statistics DTO; PR #251 adds sanitized FAQ data; this change adds a capped member snapshot with strict field, ID, URL, and boolean validation. These changes do not add mutation dispatch.
- PRs #206 and #208 add a partial `SignedHttpBotService` client for the two snapshots. PR #210 adds a Worker-native Fetch/FFI adapter; PRs #211 and #212 validate its unsafe-configuration and HTTP-error rejection paths. PR #213 extends that Worker adapter with the explicit owner-check operation, and PR #214 adds the owner-or-Administrator check. These adapters are still isolated proofs and are not wired into the production Cloudflare web app.

The endpoint remains disabled by default. Enabling it requires the explicit environment flag and a correctly provisioned keyring; no hosting environment or production secret has been changed in these PRs. Add methods only with explicit schemas, permission checks and idempotency rules; never dispatch arbitrary BotService method names.

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

## Server-side OAuth session contract

OAuth session storage is a Worker/web-runtime responsibility; it must not be hidden inside the Discord bot service. The bot service must never receive or persist browser session IDs, Discord OAuth access tokens, refresh tokens, or CSRF tokens as a shortcut for web session management.

### Browser cookie and record shape

- The browser receives only a cryptographically random opaque session ID (at least 256 bits of entropy). Store a SHA-256 digest of that ID as the session record key; never store the raw cookie value.
- The cookie is host-only, Secure, HttpOnly, SameSite=Lax, and Path=/. Do not set a broad parent-domain cookie unless a tested cross-subdomain requirement exists.
- The durable record contains the minimum server-side session state: encrypted Discord access token, token expiry, validated Discord user identity, CSRF secret, creation/last-seen timestamps, and absolute/idle expiry.
- Encrypt bearer credentials at the application layer with authenticated encryption (for example AES-GCM) using a dedicated secret unavailable to browsers and separate from Flask's signing key. Prefer the Workers Web Crypto runtime API through Python Workers' FFI rather than assuming a native Python crypto wheel is compatible. Bind the session ID and record version as authenticated associated data. Never log plaintext tokens or ciphertext payloads.
- OAuth state and PKCE verifier must also be server-side, single-use, short-lived, and bound to the pre-authentication session. Consume them atomically before accepting the callback.
- On successful OAuth callback, issue a new opaque session ID and atomically invalidate the pre-authentication ID. This is the session-rotation boundary; merely calling session.clear() on a client-side cookie is not server-side revocation.
- Logout, Discord 401 responses, account/session invalidation, and expiry must revoke/delete the server-side record. Cookie deletion alone is not sufficient.
- Apply both an idle timeout and an absolute lifetime; enforce expiry on every read, not only through background cleanup. Use no-store cache headers for authenticated pages and session responses.

### Storage decision gates

Do not use process memory or Cloudflare Workers KV as the authoritative session store: session revocation and one-time OAuth state consumption need immediate, well-defined consistency.

| Candidate | Strength | Risk / validation required |
|---|---|---|
| Cloudflare D1 | Durable SQL records, indexed lookup and atomic batches; PRs #196-#199 validate local Python Worker integration, one-time state consumption, rotation, encrypted records, expiry and revocation | Production migrations, key management, backup/restore, cleanup, outage behavior and full OAuth lifecycle still require implementation and operational validation. |
| Durable Objects | Per-session strongly coordinated state and transactional storage | Validate Python Worker integration, object lifecycle, per-session routing, cost and operational complexity. |
| Existing MongoDB through bot service | Reuses the current durable database | Couples web login availability to the bot-service network/API and expands the bot service's trust boundary; not the default choice. |
| Workers KV | Simple global key/value API | Eventual consistency makes it unsuitable as the authority for immediate logout/revocation and atomic one-time state consumption. |

D1 remains the leading candidate. The isolated compatibility spike now demonstrates Flask WSGI + D1 in the actual Python Worker runtime, conditional one-time state consumption, atomic batch rollback, encrypted record persistence, key-version rotation, idle/absolute expiry and revocation. This is stronger than API-availability evidence, but it is still not a production session store: production key provisioning, schema migrations, backup/restore, storage-outage behavior, cookie integration and full OAuth lifecycle tests remain mandatory. Re-evaluate Durable Objects only if production D1 consistency or operational requirements cannot be met. References: https://developers.cloudflare.com/d1/examples/query-d1-from-python-workers/ and https://developers.cloudflare.com/workers/languages/python/ffi/.

### Required session tests

- Serialized browser cookies contain no Discord access token, refresh token, OAuth state, PKCE verifier, or CSRF secret.
- Login rotates the session ID; the old ID cannot read or mutate the new session.
- OAuth state and PKCE verifier are one-time and cannot be replayed concurrently.
- Logout and Discord 401 immediately invalidate the server-side record.
- Idle and absolute expiry are enforced on every request.
- Session ID guessing, malformed IDs, unknown IDs, tampered ciphertext, and wrong encryption-key versions fail closed.
- Storage outage returns a controlled 503 and never falls back to an unsigned/client-side session.
- Concurrent callback, logout, and refresh/revocation operations have deterministic outcomes.
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


## Guild configuration snapshot security gate (PRs #328-#329)

The raw `api_config_snapshot(guild_id)` method remains an in-process integration detail and must not be forwarded to the Worker. PR #328 introduced the dedicated `api_rest_config_snapshot(guild_id)` projection and switched the mobile REST config, feature-list and guild-stats routes to it.

### Implemented boundary

- The in-process adapter projects only an explicit top-level allowlist of REST-safe fields.
- The projection excludes MongoDB metadata, per-guild API keys, webhook URLs (including `log_webhook_url`), and unknown future fields.
- The signed internal dispatch endpoint exposes only `api_rest_config_snapshot` for this configuration read, with a single validated guild ID argument.
- `SignedHttpBotService` exposes the same operation and validates the returned guild scope, reviewed top-level keys, JSON-compatible value bounds, nested secret-like key names, and a 64 KiB serialized response limit.
- Regression tests cover secret sentinels, unreviewed fields, nested secret-like keys, invalid guild arguments, and cross-guild responses.

### Remaining configuration gates

- The REST DTO is intentionally not a complete dashboard settings DTO. Dashboard handlers still use the in-process raw configuration snapshot and remain on the existing production adapter.
- Before dashboard routes move to the Worker, inventory every field each dashboard handler reads and define separate minimal DTOs where needed. Do not reuse the mobile REST DTO as a blanket dashboard DTO.
- Add explicit nested schemas for complex configuration fields (such as ticket panels and command templates) before broadening their remote use.
- Do not expose `api_config_snapshot` through the internal endpoint or HTTP client.

The new REST DTO and signed transport are implemented, but this does not establish full dashboard config parity or authorize production cutover. The internal endpoint remains disabled by default; no Cloudflare resources, secrets, DNS, production settings, or deployment were changed.
