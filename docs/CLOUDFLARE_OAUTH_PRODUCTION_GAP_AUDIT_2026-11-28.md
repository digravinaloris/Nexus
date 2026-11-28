# Production OAuth session gap audit

Audit date: 2026-11-28  
Audited baseline: `main` at `bb5c53d41855a7e6a266a9f6bf843dd35159e36a`.

This is a source-level audit of the production Flask OAuth/session path. It supplements `CLOUDFLARE_PRODUCTION_SESSION_DESIGN.md`; it does not change runtime behavior or certify Cloudflare readiness.

## Executive finding

The production web app still uses Flask's default client-side signed-cookie session unless a custom `session_interface` is explicitly injected into `create_web_app()`. The `__Host-nexus_session` cookie name and Secure/HttpOnly/SameSite attributes are good browser-policy settings, but they do not make Flask's default cookie contents confidential or opaque.

The current OAuth flow places all of the following in the Flask session:

- OAuth state, state creation timestamp, and PKCE verifier before login.
- Discord access token and user profile after successful login.
- Dashboard CSRF token after successful login (and lazily when a template requests one).

Flask's default secure-cookie signing provides integrity, not encryption. Therefore the access token, OAuth state/PKCE verifier, and CSRF token must be treated as readable by the browser until a server-side session interface is actually installed.

## Source evidence

### Session interface selection

`web/app_factory.py` configures `SESSION_COOKIE_NAME="__Host-nexus_session"`, `Secure`, `HttpOnly`, `Path=/`, no Domain, and `SameSite=Lax`. It only replaces Flask's session interface when the caller passes a non-null `session_interface` argument. The normal application factory path does not construct or install the Cloudflare D1 spike adapter.

### OAuth start and callback

`web/auth.py::dash_login` writes `oauth_state`, `oauth_state_created_at`, and `oauth_code_verifier` into `session`.

`dash_callback` validates state freshness, constant-time equality, and PKCE verifier syntax. It invokes `consume_oauth_state` only if the active session interface exposes that optional hook. If the hook is absent, it removes the three values from the in-memory Flask session and continues with the legacy cookie-session flow. This fallback is not an atomic, cross-request consume operation.

After Discord token exchange and user lookup, the callback invokes the optional `rotate_session` hook, then stores `access_token`, `token_expires_at`, `dash_user`, and `csrf_token` in the session. A backend implementation must guarantee that rotation and the subsequent save cannot leave credentials attached to the pre-authentication identifier.

### Logout, expiry, and authorization

- `dash_logout` checks the session CSRF token, calls the optional `revoke_session` hook, clears local session state, and expires the session through Flask's response handling.
- `dash_login_required` checks the access-token expiry and clears local session state when invalid. It attempts optional backend revocation but deliberately does not preserve credentials if revocation raises.
- A Discord 401 in `get_user_admin_guilds` similarly attempts revocation and clears local session state.
- Guild administrator permissions are fetched from Discord on each authorization check; network failures and non-200 responses fail closed rather than trusting cached guild permissions.
- `web/csrf.py` stores the CSRF token in the Flask session and validates unsafe dashboard requests against it.

These are useful lifecycle and authorization behaviors, but the optional hooks do not themselves provide durable revocation, atomicity, encryption, or outage semantics. Those guarantees must come from the installed production session interface.

## Required production acceptance criteria

Before moving authenticated routes to a Worker:

1. Install a production server-side session interface by default in the actual Worker application entry point; no implicit fallback to Flask's signed-cookie interface.
2. Keep only a high-entropy opaque session identifier in the browser cookie. Store only its digest as the lookup key.
3. Encrypt serialized session contents with authenticated encryption and versioned Worker-held keys. Do not persist OAuth tokens, OAuth state, PKCE verifier, or CSRF secrets in browser-visible cookie data.
4. Make OAuth state + timestamp + PKCE verifier consumption atomic. Concurrent callbacks must prove exactly one successful consume and token exchange.
5. Rotate the session identifier after login; revoke the old identifier. Ensure a failed rotation or save cannot produce an authenticated response or leave a reusable pre-authentication session.
6. Make logout, expiry, Discord 401, malformed records, unknown key versions, missing keys, D1 outages, and revision conflicts fail closed with stable errors and cookie clearing where applicable.
7. Test actual `Set-Cookie` output and decoded browser cookie bytes, not just cookie signatures or configured cookie attributes.
8. Preserve existing route-level guild permission checks and CSRF validation; add explicit regression tests for both through the production session adapter.
9. Keep the existing persistent Flask deployment active until the independent Worker app and session backend pass the complete acceptance suite and a separately approved cutover.

## Non-goals

This audit does not wire the D1 spike into production, change OAuth redirect URIs, create Cloudflare resources or secrets, alter DNS, or move production traffic. The Worker spike remains an isolated implementation reference, not a production dependency.
