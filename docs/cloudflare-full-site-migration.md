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
- Follow `docs/CLOUDFLARE_WORKER_SPIKE.md` for the isolated Flask/D1/Web Crypto compatibility proof. The spike must run locally and in CI before any session backend or production route is moved; it must not import `main.py` or connect to live Cloudflare resources.
- Port/adapter-test Flask routes using Cloudflare's WSGI adapter.
- Replace direct bot/database access with the service interfaces and authenticated bot-service client.
- Replace process-local state (dashboard presence, OAuth transient state, rate limits, webhook delivery coordination) with durable Cloudflare-backed state or a safe external service.
- Review Flask sessions: secure cookie settings, token exposure, session rotation, CSRF, cookie domain/SameSite/Secure settings, and OAuth redirect URI configuration.
- **OAuth session blocker identified (2026-11-27):** `web/auth.py` currently stores the Discord `access_token` in Flask's default client-side session. Flask signs these cookies but does not encrypt their payload, so a cookie holder can read the bearer credential even though `Secure`, `HttpOnly`, and `SameSite=Lax` are enabled. PR #193 removes the unused `refresh_token` from the browser session, but the access token remains exposed until the full migration is implemented. Do not treat cookie flags or signing as confidentiality protection. Before Worker rollout, move OAuth credentials to a server-side session/token store and keep only a high-entropy opaque session identifier plus non-secret CSRF/session metadata in the browser cookie. Rotate the session identifier after successful OAuth login, expire/revoke stored credentials on logout and invalidation, and test that serialized cookies never contain either token. Choose the backing store only after its durability, TTL, availability, and Worker compatibility are established.
- The server-side session design is specified in `docs/CLOUDFLARE_BOT_SERVICE_CONTRACT.md`: opaque 256-bit cookie IDs, hashed lookup keys, application-layer authenticated encryption for OAuth credentials, atomic one-time state/PKCE consumption, login rotation, immediate revocation, and fail-closed storage outages. D1 is only the first compatibility candidate; no backend is selected until the actual Python Worker binding and atomicity tests pass. Workers KV is excluded as the authoritative session store because revocation and one-time state need stronger consistency.
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
- [x] OAuth cookie exposure reduced by removing the unused Discord refresh token from the browser session (PR #193; PR and main CI green). The access token remains in Flask's readable signed cookie; the server-side session contract is now specified but not implemented.

### Remaining migration work — priority order

The detailed current-state findings and implementation gates are recorded in [`CLOUDFLARE_CURRENT_STATE_AUDIT.md`](CLOUDFLARE_CURRENT_STATE_AUDIT.md). The next steps are:

1. **Complete route-to-service inventory:** enumerate every BotService method used by each route module, classify reads/mutations/authorization/persistence, and map existing adapter and test coverage.
2. **Complete remote BotService parity:** add narrowly scoped DTO operations to the signed endpoint and Worker Fetch adapter, with contract tests, server-side authorization, idempotency for mutations, audit logging, bounded payloads, and stable errors.
3. **Implement production server-side OAuth sessions:** the production Flask session still stores the Discord access token in a readable signed cookie. The isolated D1 session spike is proof code only; production cookie/session integration, key provisioning and rotation, schema lifecycle, revocation, expiry, outage behavior, and OAuth regression tests remain a release blocker.
4. **Separate web and bot runtime configuration:** create a separately testable Worker app entry point and dependency allowlist while keeping the existing Render startup path unchanged.
5. **Prove route and asset parity:** validate all dashboard, OAuth, public, verification, appeals, webhook, and mobile API routes; inventory templates, static assets, cache rules, redirects, and dependency compatibility.
6. **Complete operational readiness:** document production bindings and environment variables, secrets rotation, D1 migrations/backups, monitoring, preview, rollback, and cutover checks. Request separate approval before any Cloudflare resource creation or deployment.

The isolated Worker spike has already validated key runtime primitives in CI; do not repeat it as though it were still an unstarted phase. Its validation does not establish production route parity or make the spike itself deployable.

### Explicitly not done

- Cloudflare Pages/Workers deployment or resource creation.
- DNS, domains, secrets, environment changes, or traffic cutover.
- Removing or changing the existing Render production path.

This checkpoint records PR-level code and documentation progress only. The route layer is separated from the runtime, but the in-process adapter still depends on the bot process and MongoDB. Render remains the rollback path until a separately approved migration and cutover.


## Progress checkpoint (2026-11-28 — refreshed)

### Additional work completed since the previous checkpoint

- [x] PRs #328-#329: introduced and transported a dedicated REST-safe configuration DTO; raw Mongo configuration remains local-only.
- [x] PR #331: introduced an allow-listed dashboard configuration read model and switched dashboard routes away from the raw configuration snapshot.
- [x] PRs #332-#335: transported dashboard config and upcoming calendar snapshots, and added remote owner/admin checks plus candidate-guild filtering.
- [x] PRs #336-#343: moved root compatibility shims into `compat/` and extracted Mongo initialization, web server startup, SIGTERM handling, thread startup, Mongo cleanup, and runtime service startup helpers from `main.py`.

### Current migration blockers

1. Production OAuth still stores the Discord access token in Flask's readable signed-cookie session. The isolated D1 session interface remains proof code, not production integration.
2. Production route handlers still resolve the in-process BotService adapter. The signed HTTP adapter and Cloudflare Fetch adapter are not injected into the production Worker app.
3. The Cloudflare Fetch adapter still supports only a subset of the now-expanded signed internal operation allowlist. It needs parity in reviewed, read-only slices before dashboard routes can run remotely.
4. The route-to-service inventory is historical and its old counts must not be used as current coverage figures. Refresh it from current `main`.
5. Full route, template, static-asset, environment/binding, and rollback parity remain unproven.

### Next implementation sequence

1. Refresh the route-to-service inventory against current `main`, comparing interface, in-process adapter, signed endpoint, HTTP adapter, Worker Fetch adapter, and tests.
2. Extend the Cloudflare Fetch adapter with already-reviewed read-only dashboard DTO operations in small PRs, preserving strict guild scope, exact schemas, bounds, and fail-closed behavior.
3. Complete production server-side OAuth session integration only after the Worker D1 binding, key lifecycle, cookie behavior, outage handling, and full OAuth regression tests are proven.
4. Inject the remote adapter into a separate Worker app entry point only after those gates pass; retain the current Render startup and in-process adapter unchanged.
5. Finish route/assets parity and operational readiness before requesting separate deployment approval.

No Cloudflare resources, secrets, DNS, production settings, or deployment were changed. The current Render path remains untouched.


## Progress checkpoint (2026-11-28 — after PR #420)

### Additional groundwork completed

- [x] PRs #375 and #385–#400 expanded the isolated Worker session proof: atomic OAuth state consumption, revision/CAS conflict handling, expiry, opaque-cookie policy, malformed/oversized payload rejection, key-version migration, missing-key fail-closed behavior, and regression coverage for concurrent rotation and stale writes. This remains a compatibility spike, not the production session backend.
- [x] PRs #401–#418 expanded the isolated Worker Fetch transport with presence heartbeat and strict DTO validation for health/status, guild lists, channel/role collections, member/calendar/dashboard snapshots, duplicate IDs, and bounded payloads.
- [x] PR #419 rejects non-finite numeric values in dashboard and REST configuration DTOs.
- [x] PR #420 corrected the signed internal BotService endpoint contract documentation to distinguish read-only snapshots from its explicitly scoped appeal-claim workflow and presence heartbeat.
- [x] The active `main` baseline for this checkpoint is `247f8a1cce1a336788e85854c582d8f0d32604f1`.

### Remaining release blockers (unchanged)

1. Production Flask OAuth still stores the Discord access token (and transient OAuth/CSRF values) in the default readable signed-cookie session. The D1 session implementation is still isolated spike code; production integration and its full acceptance suite are not complete.
2. Production routes still use the in-process BotService adapter. The signed HTTP and Cloudflare Fetch clients are not yet injected into a separately testable Worker app, and method presence is not proof of route/authorization parity.
3. The Worker Fetch surface still lacks reviewed operations needed by some routes. Mutations and security-sensitive flows must receive individual authorization, actor, idempotency, audit, schema, and failure-semantics reviews; do not bulk-forward them.
4. Full template/static asset parity, Worker dependency compatibility, environment/binding matrix, monitoring, rollback and end-to-end route tests remain outstanding.

No Cloudflare account resources, secrets, DNS, OAuth redirect settings, production hosting configuration, or deployment were changed. The existing Render path remains untouched. Cloudflare migration should begin only after the blockers above pass their tests and a separate explicit deployment/cutover approval is received.
