# Cloudflare migration: route and runtime compatibility inventory

This inventory is the Phase 0 baseline for the full-site migration. It describes the current `main.py` + Flask + Discord bot process and identifies what must be moved behind a service boundary before the web runtime can run on Cloudflare.

## Runtime and dependency inventory

| Dependency / runtime assumption | Current use | Cloudflare Worker disposition |
|---|---|---|
| `discord.py` | Gateway client, guild/member/channel cache, moderation and role actions | **Must stay in the persistent bot service.** Do not import it in Worker code. |
| `pymongo`, `dnspython` | MongoDB Atlas client and collections | **Keep DB ownership on the persistent service initially.** Do not open PyMongo sockets from a Worker. |
| Flask | HTTP routing, request/session handling, Jinja rendering | Candidate for Python Workers WSGI adapter; verify every extension and request behavior with a Worker compatibility test. |
| Jinja templates | Server-rendered public, dashboard, verification and appeal pages | Can initially be rendered by the Worker only if the template loader/filesystem assumptions pass. Otherwise migrate selected pages to Pages assets/client rendering. |
| `waitress`, Flask development server | Long-lived WSGI listener started in a thread | Not part of Worker runtime; Worker supplies the HTTP entry point. Preserve only for existing persistent-host startup. |
| `threading.Thread`, `asyncio`, `bridge.run_coroutine` | Flask server thread and synchronous calls into Discord bot event loop | Remove from Worker path. Replace bot calls with bounded authenticated HTTPS service requests. |
| `requests` | Outbound HTTP (e.g. email provider) | Audit and migrate to Worker-supported outbound fetch; verify timeout and response handling. |
| `Pillow`, `matplotlib`, `reportlab` | Image/chart and PDF/CSV export generation | Audit native/WASM compatibility, memory and CPU limits. Prefer moving heavy generation to persistent service or replacing with Worker-safe implementations. |
| `pyyaml`, `pyotp`, `deep-translator`, `sentry-sdk[flask]`, `certifi` | Configuration, TOTP, translation, monitoring, TLS | Individually test imports and runtime paths; do not assume CPython wheel compatibility. |
| `pytest`, `mongomock` | Test-only dependencies | Keep out of Worker runtime bundle; use in CI and contract tests. |

This table is a risk classification, not a claim that every package has already been tested on Pyodide.

## HTTP surface inventory

The route list below is based on the Flask route decorators in the current modules. Duplicate paths represent distinct HTTP methods.

| Module | Routes / responsibilities | Required backend capabilities |
|---|---|---|
| `web/public.py` | `/`, `/favicon.ico`, `/status`, kill-switch links, public appeal pages | Public status; owner security workflow; appeal lookup/submission |
| `web/auth.py` | `/login`, `/callback`, `/logout` | Discord OAuth, state/session storage, guild membership/permission checks |
| `web/dashboard.py` | `/dashboard`, guild dashboard, settings, features, AutoMod, permissions, API key rotation, presence, calendar, templates, audit exports, cases and appeals | Authenticated guild reads, permission checks, config CRUD, moderation, audit and export services |
| `web/verify.py` | `/verify/<token>` | Turnstile verification, challenge state, member lookup and role grant |
| `web/rest_api.py` | `/api/health`, guilds, stats, config, lock/unlock, broadcast, member moderation, warnings, channels, roles, members, audit, cases, appeals, features and FAQ | Versioned mobile API auth, guild/member reads, Discord actions, Mongo-backed records |
| `web/github_webhook.py` | `/webhooks/github/releases` | Bounded request body, GitHub signature verification, idempotent release notification |
| `web/csrf.py` | CSRF protection registration | Worker-compatible session/CSRF behavior and regression tests |

## Main coupling points confirmed in current code

- `main.py` creates the Discord bot, initializes MongoDB, constructs the Flask app, registers all route modules, starts Waitress in a daemon thread, then runs the Discord Gateway client.
- `core.bot` exposes the live `discord.py` bot object to extracted modules.
- `core.col` and `core.db` expose MongoDB collections/database to web and bot modules.
- `bridge.run_coroutine` uses `asyncio.run_coroutine_threadsafe(..., core.bot.loop)` and blocks for the result.
- `main.py` imports both bot-specific and web-specific packages and currently owns the shared process lifecycle.
- Flask sessions use `FLASK_SECRET_KEY`; secure, HttpOnly and SameSite=Lax cookies are configured. CSRF protection and security headers are registered centrally.
- The mobile API is not a standalone stateless API today: handlers use bot guild/member/channel state and perform live Discord actions.

## Required service boundary

Before moving any route, enumerate each bot/database operation and give it a narrow service method. At minimum, contracts must cover:

1. OAuth identity and guild authorization.
2. Guild metadata, channels, roles and member summaries.
3. Dashboard configuration and feature toggles.
4. Moderation actions and sanction/appeal workflows.
5. Verification challenge validation and post-verification role grant.
6. Ticket/calendar/template/audit data required by current dashboard features.
7. Mobile API operations, preserving paths, authentication scopes, response schemas and status codes.
8. GitHub release webhook validation and notification dispatch.

The persistent bot service must enforce authorization again; the Worker must not be trusted as the only policy enforcement layer. Use signed, timestamped requests with replay protection, strict body limits, route-level authorization and short timeouts. Never expose raw database query operations or the Discord bot token.

## Migration test gates

- Import and startup smoke test for the Python Worker entry point under the supported Cloudflare runtime.
- Explicit dependency compatibility tests for every runtime dependency that remains in the Worker.
- Contract tests for bot-service success, timeout, unavailable service, invalid signature, replay, malformed payload and permission denial.
- Route parity tests for methods, paths, status codes, response JSON, cookies and redirects.
- OAuth state/CSRF/session rotation tests.
- Cache isolation tests proving authenticated dashboard, appeal and verification responses are not shared-cacheable.
- Mobile API regression tests for API-key scopes and existing response contracts.
- Worker size, CPU, memory and request-body limit checks.
- Keep the existing Render/Flask entry point unchanged until a separately approved production cutover.

## Explicitly out of scope

No Cloudflare account access, resource creation, Pages/Worker deployment, secret upload, DNS or OAuth redirect URI changes, database migration, production traffic change, or removal of the existing host.
