# Cloudflare migration: current-state audit

Audit date: 2026-11-27  
Audited baseline: `main` at `dcef55450e5c96b4916fcbd29a6b67d98bc61636`

## Executive summary

Nexus already has substantial migration groundwork. The Flask route layer is separated from `main.py` and consumes a transport-neutral `BotService`; the isolated Cloudflare Python Worker spike has exercised Flask WSGI, local D1, Web Crypto, encrypted session records, expiry, rotation, revocation, and a signed Worker Fetch transport in the actual local Worker runtime.

**The application is not ready to move to Cloudflare.** The production web app still runs in the same process as the Discord Gateway bot, its active adapter is in-process, the production OAuth session still places the Discord access token in Flask's readable signed cookie, and the production remote adapter does not implement the route contract. The Worker spike is a compatibility proof, not a deployable copy of the Nexus web application.

This audit is documentation-only. It does not change runtime behavior, dependencies, secrets, hosting, DNS, or Cloudflare resources.

## What is already in place

### Web application boundary

- `web/app_factory.py` constructs the Flask application without importing `main.py`; it registers public, OAuth, dashboard, verification, webhook, REST, CSRF, and security-header behavior.
- Web route modules use `get_bot_service()` rather than importing the Discord bot directly.
- `web/in_process_bot_service.py` preserves the current deployment by adapting the existing bot, stores, and event loop.
- The REST API has request-size limits, bounded API-key handling, stable error responses, and a generic unexpected-error handler.
- Dashboard and authenticated routes receive no-store cache headers; baseline browser security headers are registered centrally.

### Cloudflare proof work

- `cloudflare-spike/` is isolated from the production bot application and has its own Wrangler/Python project.
- The documented CI spike validates Flask WSGI startup, local D1 operations, atomic one-time state consumption, session rotation/revocation, Web Crypto SHA-256/AES-GCM, encrypted record persistence, and expiry behavior.
- The spike includes a Worker-native Fetch client for signed requests to a local mock endpoint, including configuration and response failure checks.
- The internal bot endpoint is opt-in and disabled unless explicitly configured. The spike does not connect to a production bot host or Cloudflare account.

## Confirmed blockers

### 1. Production OAuth sessions are still client-side

The production app factory uses Flask's default session interface unless a custom interface is injected. The current OAuth flow stores `oauth_state`, its timestamp, and the PKCE verifier in the session, then stores the Discord `access_token` after login. Flask's default signed-cookie session is readable by the browser: signing protects integrity, not confidentiality.

The removal of the unused refresh token reduced exposure, but it did not solve the access-token exposure. The server-side D1 session implementation in `cloudflare-spike/` is disposable proof code and is not wired into the production app.

**Gate:** implement a production session interface with opaque high-entropy cookies, hashed lookup keys, authenticated encryption for credentials, atomic OAuth state consumption and login rotation, expiry, logout/401 revocation, and fail-closed storage errors. Test that serialized browser cookies never contain access tokens, refresh tokens, OAuth state, PKCE verifiers, or CSRF secrets.

### 2. The production web app is still coupled to the bot process

`main.py` creates the Discord bot and MongoDB client, builds the Flask app, starts Waitress in a thread, and then runs the Discord Gateway. The production adapter still reaches the live bot, MongoDB-backed stores, and bot event loop.

The route/service boundary is useful and should be preserved, but it does not yet make the web app independently deployable. Cloudflare must not host the Discord Gateway process or open the current PyMongo connection.

**Gate:** keep Discord Gateway and MongoDB ownership on a persistent Nexus bot service. The Worker must call a narrowly scoped, authenticated HTTPS API and must never import bot runtime modules, access raw collections, or invoke the bot event loop.

### 3. Remote BotService parity is incomplete

The `BotService` interface contains many operations used by dashboard, public, verification, OAuth, webhook, and mobile REST routes. The existing `SignedHttpBotService` implementation currently dispatches only `health_snapshot` and `public_status_snapshot`. The Worker Fetch adapter and internal endpoint have a larger but still deliberately allow-listed read-only subset; mutation and full dashboard workflows are not transported.

A successful health request therefore does not demonstrate that dashboard, appeals, verification, moderation, calendar, templates, audit exports, or mobile API behavior can run remotely.

**Gate:** generate a method-by-method route usage matrix, then add narrow DTO schemas and transport tests in small groups. Add privileged mutations only with server-side identity/permission checks, idempotency, audit logging, bounded payloads, and stable error mapping.

### 4. Worker proof is not production route parity

The current Worker entry point is a local spike with fixture routes and test-only keys. It is not the production `create_web_app()`, does not prove all current Jinja templates/assets work under the Worker runtime, and does not establish parity for the complete HTTP surface.

The root `requirements.txt` also describes the combined bot/web process, including Discord.py, PyMongo, Waitress, image/PDF/reporting libraries, and other packages that must not be blindly bundled into the Worker.

**Gate:** maintain a separate Worker dependency manifest and prove the production app import graph is free of bot/database runtime modules. Audit each dependency and template/static asset path. Keep heavy or incompatible operations on the persistent service where appropriate.

### 5. Pages/Worker split and operational readiness remain open

The existing inventory identifies route groups, but the complete template/static asset map, cache policy per route, redirects, environment/binding matrix, production migrations, backup/restore, monitoring, and rollback procedure still need implementation-level validation.

Pages should serve static assets where appropriate; the Worker should handle dynamic Flask-compatible routes. Do not treat Pages as a host for the existing Flask process.

## Recommended implementation order

1. **Finish the route-to-service inventory.** Enumerate every `get_bot_service()` call in all route modules and classify it as read, mutation, authorization, persistence, or external delivery. Record the corresponding adapter and test coverage. This is the next safest code-adjacent task because it defines the actual scope of remote parity.
2. **Complete transport contracts in small slices.** Extend the signed internal endpoint and Worker Fetch adapter from read-only snapshots toward the operations required by the dashboard and REST API. Start with low-risk read-only dashboard support, then narrowly scoped writes. Add contract tests with every operation.
3. **Implement production server-side OAuth sessions.** Reuse the proven primitives only after adapting them to production configuration, cookie policy, key provisioning/rotation, migrations, backup/restore, and outage handling. This is a security release blocker.
4. **Separate web and bot runtime configuration.** Add a separately testable Worker app entry point and explicit dependency allowlist without changing the current Render startup path.
5. **Prove route and asset parity.** Test OAuth, dashboard permissions/actions, appeals, verification, GitHub webhook, and every mobile API route; inventory templates, static assets, cache rules, and redirects.
6. **Complete release readiness.** Document bindings/environment variables, secrets rotation, monitoring, backups, rollback, and operator steps. Only then request separate approval for Cloudflare resource creation or production cutover.

## Non-goals and safety constraints

- No deployment, Cloudflare account/resource creation, secret upload, DNS change, OAuth redirect URI change, or production traffic cutover.
- Do not modify or remove the current Render/Flask startup path during preparation.
- Do not move the Discord Gateway bot into a request-scoped Worker.
- Do not connect a Worker directly to MongoDB Atlas through PyMongo.
- Do not treat the isolated spike's test keys, D1 schema, or session implementation as production-ready.
- Preserve existing route paths, mobile API contracts, authorization checks, CSRF behavior, and private-response cache controls.

## Audit evidence

Reviewed on the audited `main` baseline:

- `main.py`, `requirements.txt`
- `web/app_factory.py`, `web/auth.py`, `web/bot_service.py`, `web/in_process_bot_service.py`, `web/http_bot_service.py`, `web/rest_api.py`, `web/dashboard.py`
- `cloudflare-spike/src/worker.py`, `cloudflare-spike/src/bot_service_client.py`, `cloudflare-spike/src/d1_session_interface.py`, `cloudflare-spike/wrangler.jsonc`
- `docs/cloudflare-full-site-migration.md`, `docs/CLOUDFLARE_COMPATIBILITY_INVENTORY.md`, `docs/CLOUDFLARE_WORKER_SPIKE.md`, `docs/CLOUDFLARE_BOT_SERVICE_CONTRACT.md`
