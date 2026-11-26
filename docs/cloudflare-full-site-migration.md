## Progress checkpoint (2026-11-26 — refreshed)

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

### Remaining migration work — priority order

1. **Dashboard contract extraction:** replace dashboard's remaining direct bot/database access with explicit service/store contracts. Keep existing Flask routes and behavior while adding tests.
2. **Public and verification routes:** move kill-switch, appeal, status, and verification dependencies behind narrowly scoped contracts; identify any routes that truly require bot runtime.
3. **Runtime separation:** make the web app start/test independently of the Discord Gateway process, without changing production startup.
4. **Worker transport:** add a Worker-side signed client only after operation contracts stabilize; test signatures, nonce/replay, timeout, fail-closed behavior, and error mapping.
5. **Compatibility spike:** build an isolated, CI-checked minimal Worker/Pages scaffold and verify the actual dependency/runtime constraints before attempting a broad Flask/Jinja port. Do not treat Pages as a Flask host.
6. **Frontend/asset inventory and split:** map every template, route, static asset, cache rule, and redirect; choose Pages for static assets and Worker for dynamic routes, retaining private/no-cache behavior for dashboard, appeals, and verification.
7. **Parity and release readiness:** OAuth/session/CSRF/CSP, mobile API compatibility, config/bindings matrix, preview and rollback docs, and end-to-end coverage.

### Explicitly not done

- Cloudflare Pages/Workers deployment or resource creation.
- DNS, domains, secrets, environment changes, or traffic cutover.
- Removing or changing the existing Render production path.

This checkpoint records PR-level code and documentation progress only. Render remains the rollback path until a separately approved migration and cutover.
