# Cloudflare full-site migration plan

## Goal

Move the complete Nexus web experience to Cloudflare, including the public website, Discord OAuth login, server dashboard, verification and appeal pages, and the mobile REST API. Keep the Discord Gateway bot on a persistent host. This plan authorizes preparation and reviewable pull requests only: **no Cloudflare account changes, secrets changes, DNS changes, production cutover, or deployment**.

## Current architecture

- Flask application and Discord bot are started together from `main.py`.
- Flask routes are split across `web/auth.py`, `web/dashboard.py`, `web/public.py`, `web/verify.py`, `web/rest_api.py`, and `web/github_webhook.py`.
- Web route modules (dashboard, REST API, public, verification, OAuth, and GitHub webhook) use the transport-neutral `BotService` interface; they do not import the Discord client, bot runtime, BSON, or PyMongo directly.
- The current `InProcessBotService` adapter still owns the legacy integration: it reads `core.bot` / `core.col`, imports `discord.py`, accesses storage helpers, and uses `bridge.run_coroutine` for operations on the bot's long-lived asyncio loop.
- The mobile API and dashboard therefore remain operationally coupled to the bot process through that adapter, even though the route layer is separated from those implementation details.
- Calendar event IDs now cross the web/service boundary as hexadecimal strings; BSON `ObjectId` parsing stays in the persistence layer.
- The Flask app currently relies on server-side Python dependencies and templates from the repository.

## Target architecture

1. **Cloudflare Pages** serves the public frontend/static assets.
2. **Cloudflare Python Worker** owns HTTP entry points: Discord OAuth, dashboard pages/API, Turnstile verification, appeals, GitHub webhook intake, and mobile API compatibility routes. Flask can be retained initially through Cloudflare's WSGI adapter, subject to compatibility tests.
3. **Nexus bot service** remains on a persistent host and owns the Discord Gateway connection, Discord.py objects, bot event loop, and MongoDB connection. It exposes a narrowly scoped, authenticated HTTPS internal API for web-originated reads/actions.
4. **MongoDB Atlas** remains the source of truth initially. Do not move or migrate user data as part of the hosting migration.
5. **Cloudflare bindings** hold Worker secrets/configuration and provide suitable state/rate-limit primitives. OAuth session and temporary state must not depend on process memory.

The Worker must never import or instantiate `discord.py`, access `core.bot`, call `bridge.run_coroutine`, or open a direct PyMongo socket. Bot-only operations must cross the authenticated bot-service boundary.

## Migration phases and PR sequence

### Phase 0 — compatibility and architecture guardrails
- Add this migration plan and a dependency/route inventory.
- Identify all imports and runtime assumptions that are incompatible with Workers/Pyodide (especially Discord.py, PyMongo, threads, subprocesses, filesystem access, and native wheels).
- Define a contract for the internal bot service, including authentication, request validation, timeouts, error mapping, and fail-closed behavior.
- Add CI checks that prevent Cloudflare Worker code from importing bot runtime modules.

### Phase 1 — isolate web contracts from the bot runtime
- Introduce typed/validated service interfaces for guild metadata, dashboard authorization, moderation actions, tickets, verification role grants, appeals, calendar, and mobile API operations.
- Implement the interfaces against the existing in-process bot first, preserving current Flask behavior.
- Add contract tests and regression tests for existing dashboard/API behavior.
- Keep MongoDB ownership on the persistent bot service; route all web-required data operations through explicit service methods rather than exposing arbitrary database queries.

### Phase 2 — authenticated bot-service API
- Add a private internal HTTP API to the persistent Nexus process.
- Authenticate Worker-to-bot calls with a dedicated rotating secret or signed requests; enforce timestamp/replay protections, payload limits, per-route authorization, and strict timeouts.
- Expose only the minimum operations required by the website; never expose raw MongoDB queries or Discord credentials.
- Add integration tests for unavailable bot, invalid signatures, replay attempts, malformed payloads, and permission boundaries.

### Phase 3 — Worker-compatible web runtime
- Add a separate Worker entry point and Wrangler configuration without changing the existing Render entry point.
- Port/adapter-test Flask routes using Cloudflare's WSGI adapter.
- Replace direct bot/database access with the service interfaces and authenticated bot-service client.
- Replace process-local state (dashboard presence, OAuth transient state, rate limits, webhook delivery coordination) with durable Cloudflare-backed state or a safe external service.
- Review Flask sessions: secure cookie settings, token exposure, session rotation, CSRF, cookie domain/SameSite/Secure settings, and OAuth redirect URI configuration.
- **OAuth session blocker identified (2026-11-27):** `web/auth.py` currently stores the Discord `access_token` in Flask's default client-side session. Flask signs these cookies but does not encrypt their payload, so a cookie holder can read the bearer credential even though `Secure`, `HttpOnly`, and `SameSite=Lax` are enabled. PR #193 removes the unused `refresh_token` from the browser session, but the access token remains exposed until the full migration is implemented. Do not treat cookie flags or signing as confidentiality protection. Before Worker rollout, move OAuth credentials to a server-side session/token store and keep only a high-entropy opaque session identifier plus non-secret CSRF/session metadata in the browser cookie. Rotate the session identifier after successful OAuth login, expire/revoke stored credentials on logout and invalidation, and test that serialized cookies never contain either token. Choose the backing store only after its durability, TTL, availability, and Worker compatibility are established.
- Audit every dependency against Python Workers/Pyodide compatibility; replace unsupported socket/native/thread/process dependencies rather than silently dropping functionality.

### Phase 4 — Pages frontend and asset/template migration
- Inventory templates, static assets, generated images/reports, and URL generation.
- Decide which pages remain server-rendered by the Worker and which become static/client-rendered Pages routes.
- Preserve all current URLs or provide tested redirects.
- Keep personal pages (dashboard, appeals, verification) uncacheable unless a route is explicitly safe to cache.
- Add CSP, security headers, cache rules, and error pages.

### Phase 5 — API and OAuth parity
- Preserve mobile API paths, request/response formats, API-key scope rules, and status codes.
- Preserve Discord OAuth scopes, PKCE, state validation, and callback behavior.
- Verify Turnstile, GitHub release webhook signature verification, appeals, and dashboard permissions through the new service boundary.
- Add end-to-end tests for login, logout, guild selection, dashboard actions, mobile endpoints, and failure cases.

### Phase 6 — release readiness (no deployment in this work)
- Produce a complete environment-variable and Cloudflare-binding matrix.
- Document Pages/Worker configuration, domains, OAuth redirect URI updates, secrets, rollback plan, and operational monitoring.
- Add CI-only build/compatibility checks and local preview instructions.
- Require explicit user approval before any Cloudflare login, resource creation, secret upload, DNS update, production deployment, or traffic cutover.

## Non-negotiable safety requirements

- No production deployment or DNS/domain change in these PRs.
- No secrets or tokens committed to Git.
- The existing Render deployment remains untouched and is the rollback path until a separately approved cutover.
- Do not remove the existing Flask/Render startup path until Cloudflare parity is proven and cutover is explicitly approved.
- No loss of Discord bot functionality: the Gateway process remains persistent and separate from the Worker.
- Preserve authorization checks server-side; never trust dashboard UI state or client-supplied guild permissions.
- Fail closed when the bot service, Discord permission lookup, or session validation is unavailable.

## Known technical risks to resolve before implementation

- Python Workers execute through Pyodide/WebAssembly. Flask has a supported WSGI adapter, but that does not make every CPython package or OS feature compatible.
- PyMongo's normal socket-based connection model and Discord.py's Gateway/event-loop model are not suitable for running inside the request-scoped Worker runtime. They stay behind the persistent bot service.
- The route layer is now behind `BotService`, but the in-process adapter is still tightly coupled to `core.bot`, `core.col`, Discord.py, storage modules, and `bridge.run_coroutine`. Replacing this adapter with a separately hosted authenticated HTTP client is the main remaining runtime boundary.
- Process-local dictionaries and filesystem assumptions are not durable across Worker isolates.
- Flask's default signed client-side session is readable by the browser; Discord OAuth bearer tokens must not remain in that cookie. A server-side session/token store is a release blocker, not an optional optimization.
- Flask/Jinja templates and generated reports need an explicit asset strategy; Pages static hosting alone cannot execute the current Flask application.

## Acceptance criteria

- Existing Render behavior remains unchanged throughout the migration PR series.
- Worker code contains no direct Discord.py, PyMongo, bot event-loop, or arbitrary database access.
- All existing dashboard, OAuth, public, verification, appeals, webhook, and mobile API tests continue to pass.
- OAuth access/refresh tokens are stored server-side; browser session cookies contain no bearer credentials, and login rotates the session identifier.
- New contract, Worker compatibility, security, and end-to-end tests cover success and failure paths.
- The final documentation includes a complete configuration matrix and a tested rollback procedure.
- Production remains on the existing host until a separate, explicit deployment approval.


## Progress checkpoint (2026-11-27 — refreshed)

### Completed groundwork

- [x] Migration architecture, compatibility inventory, and bot-service contract documented.
- [x] In-process bot-service adapter and REST service boundary introduced.
- [x] REST health, guild discovery, guild configuration, channel/role/member snapshots, and guild stats routed through the service.
- [x] REST moderation actions (lock/unlock, broadcast, mute/unmute, ban/kick, warn/unwarn) routed through the service.
- [x] Appeals action flow moved behind the service, including notification delivery.
- [x] API-key guild discovery no longer reads Discord guild objects from REST authentication.
- [x] REST API module no longer imports the Discord transport or legacy coroutine bridge.
- [x] REST warning/sanction/appeal read queries extracted into store/service boundaries.
- [x] FAQ persistence shared between REST endpoints and Discord cog through a dedicated store.
- [x] Dashboard sanction and FAQ persistence/query paths have begun using dedicated stores (PRs #117–#119 merged and green).
- [x] Dashboard and public/verification/OAuth/webhook route modules use the transport-neutral `BotService` interface rather than importing bot runtime modules.
- [x] Dashboard viewer heartbeats now cross the `BotService` contract instead of being stored in the dashboard route module (PR #189; CI green). The current in-process adapter still uses process memory, so this is a boundary refactor—not durable multi-isolate storage.
- [x] Dashboard calendar event IDs cross the service boundary as strings; BSON parsing is confined to the calendar persistence layer (PR #178 merged).
- [x] Architecture CI guard rejects direct BSON imports from web route modules (PR #179 merged).
- [x] REST API hardening: generic internal-error responses for handled operations (PR #176), bounded API-key header length (PR #175), oversized request-body rejection (PR #177), and a blueprint-level fallback for unexpected exceptions while preserving HTTP exceptions (PR #190; PR and main CI green).

### Remaining migration work — priority order

1. **Adapter contract completion:** audit every method used by dashboard, public, verification, OAuth, webhook, and REST routes; ensure inputs/outputs are transport-safe DTOs (strings, numbers, booleans, lists, and dictionaries), never Discord or BSON objects.
2. **In-process adapter hardening:** progressively move direct collection access and Discord-specific operations behind dedicated stores and narrow adapter methods; preserve existing Flask behavior and add contract tests.
3. **Runtime separation:** make the web app start and test independently of the Discord Gateway process, then replace the in-process adapter with an authenticated remote client without changing production startup prematurely.
4. **Worker transport:** add a Worker-side signed client only after operation contracts stabilize; test signatures, nonce/replay, timeout, fail-closed behavior, and error mapping.
5. **Compatibility spike:** build an isolated, CI-checked minimal Worker/Pages scaffold and verify the actual dependency/runtime constraints before attempting a broad Flask/Jinja port. Do not treat Pages as a Flask host.
6. **Frontend/asset inventory and split:** map every template, route, static asset, cache rule, and redirect; choose Pages for static assets and Worker for dynamic routes, retaining private/no-cache behavior for dashboard, appeals, and verification.
7. **Parity and release readiness:** OAuth/session/CSRF/CSP, mobile API compatibility, config/bindings matrix, preview and rollback docs, and end-to-end coverage.

### Explicitly not done

- Cloudflare Pages/Workers deployment or resource creation.
- DNS, domains, secrets, environment changes, or traffic cutover.
- Removing or changing the existing Render production path.

This checkpoint records PR-level code and documentation progress only. The route layer is separated from the runtime, but the in-process adapter still depends on the bot process and MongoDB. Render remains the rollback path until a separately approved migration and cutover.
