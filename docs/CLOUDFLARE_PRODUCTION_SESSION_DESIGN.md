# Production OAuth session design for Cloudflare

Status: proposed implementation contract; not deployed or wired into production  
Date: 2026-11-28  
Baseline: `main` at `39fc54e2f3274514176a8482b5991c8ec7cb271d`

## Decision summary

The Cloudflare migration must replace Flask's readable client-side session cookie before any authenticated dashboard or OAuth traffic is moved. The isolated `cloudflare-spike/` D1 adapter is a useful implementation reference, not a production-ready component.

The intended production model is:

- A browser cookie contains only a cryptographically random opaque session identifier.
- The identifier is high entropy (at least 256 random bits); only its SHA-256 digest is used as the database lookup key.
- Session contents are serialized server-side and encrypted with authenticated encryption (AES-256-GCM or an equivalently reviewed AEAD).
- Encryption keys are held only as Worker secrets, are versioned, and are never stored in D1 or returned to the browser.
- D1 stores ciphertext, nonce, key version, creation/absolute-expiry/idle-expiry timestamps, revocation timestamp, and a monotonic revision for compare-and-swap updates.
- OAuth state is consumed atomically. Successful authentication rotates the session identifier and revokes the pre-authentication session.
- Storage, key, decryption, or replay-protection failures fail closed; authenticated routes must not continue with a partially loaded session.

This document defines the target contract. It does not select production bindings, provision D1, change cookie settings, or activate a Worker.

## Cookie contract

Production cookie requirements:

- Name: `__Host-nexus_session`.
- `Secure`, `HttpOnly`, `Path=/`, `SameSite=Lax`.
- No `Domain` attribute (host-only).
- Value: URL-safe random opaque identifier only; never JSON or serialized Flask session data.
- The `__Host-` prefix requires HTTPS and the cookie attributes above. Local HTTP smoke tests must use an explicitly separate test cookie name/configuration; do not weaken production settings to accommodate local tests.

The cookie must not contain or encode:

- Discord access or refresh tokens.
- OAuth state, PKCE verifier, or authorization code.
- CSRF secret/token.
- User profile, guild permissions, or other session payload fields.

A browser-cookie regression test must inspect the actual Set-Cookie value and prove these values are absent, not merely assert that the cookie is signed.

## Server-side record contract

Recommended D1 record fields:

| Field | Purpose |
| --- | --- |
| `session_hash` | SHA-256 digest of the opaque cookie ID; unique primary lookup key |
| `ciphertext`, `nonce` | AEAD-encrypted serialized session payload |
| `key_version` | Selects the active or retained decryption key |
| `created_at` | Absolute session lifetime anchor |
| `expires_at` | Absolute expiry |
| `idle_expires_at` | Sliding idle expiry, never later than absolute expiry |
| `revoked_at` | Logout, rotation, invalidation, or security revocation |
| `revision` | Monotonic compare-and-swap version for concurrent writes |

The database must enforce uniqueness for `session_hash`. Expired and revoked records require bounded cleanup; cleanup must not be part of the authentication critical path.

Session payload size must be bounded before encryption and after decryption. Malformed JSON, invalid payload types, unsupported key versions, AEAD authentication failures, and oversized payloads must never become authenticated sessions.

## Lifetime and rotation

The spike currently demonstrates 15-minute idle and 24-hour absolute limits. These are candidate defaults, not an approved product policy. Before production wiring, choose and document final durations for:

- Idle expiry.
- Absolute expiry.
- OAuth state validity (currently 10 minutes in the application helper).
- Any elevated/step-up authentication lifetime, if introduced.

Required lifecycle behavior:

1. New anonymous session: create a fresh opaque ID and server-side record only when needed.
2. OAuth start: store state, creation time, and PKCE verifier in the encrypted server-side record.
3. OAuth callback: validate state and timestamp, then atomically consume state + timestamp + verifier using a revision compare-and-swap before exchanging the authorization code.
4. Successful login: create a new opaque session ID, persist the authenticated payload, and revoke the pre-login session. Never upgrade the pre-login ID in place.
5. Logout: revoke the server-side record and expire the browser cookie.
6. Discord 401 or expiry: clear local request state and revoke the record. Revocation failure must not leave the browser authenticated; surface storage failure where safe and fail closed.
7. Concurrent requests: revision conflicts must not overwrite newer session state or resurrect a revoked/rotated session.

OAuth state consumption and session rotation must be covered by concurrent tests proving exactly one callback can consume a state value.

## Key management and rotation

- Keep encryption keys in Worker secrets, not D1 rows, source control, Wrangler config, or browser data.
- Every ciphertext records a key version. New writes use the active version; reads may use a bounded set of retained versions.
- Rotation procedure: provision the new secret, switch the active version, retain the previous key for the maximum absolute session lifetime, then retire it after records have expired or been re-encrypted.
- Missing key material or unknown key versions are availability/security failures, not a reason to fall back to plaintext or client-side sessions.
- Document emergency key compromise response: revoke all sessions encrypted under the compromised version and require reauthentication.

The key provisioning and rotation procedure must be tested in a non-production environment before production secrets are created.

## Failure behavior

The session layer must fail closed for:

- D1 unavailable, timed out, or returning malformed results.
- Key secret missing or unavailable.
- AEAD decryption/authentication failure.
- Revision/CAS conflict during state consumption, rotation, or session save.
- Session record expired or revoked.

A storage outage must never silently fall back to Flask's signed-cookie session interface or an in-memory session store. Return a stable service-unavailable response for operations that require session persistence. Public static routes may remain available only if they do not depend on session state.

## CSRF and OAuth interaction

- Keep existing CSRF protections and route-level authorization semantics.
- CSRF state must remain server-side or be derived using a separately reviewed design; it must not be copied into the opaque cookie value.
- Preserve PKCE S256 and existing OAuth redirect validation.
- OAuth state is one-time and must be consumed atomically before token exchange.
- Session ID rotation occurs after successful authentication and on any future privilege elevation.
- Do not change Discord OAuth redirect URIs as part of this design-only step.

## Production integration gates

Before wiring this into `create_web_app()`:

1. Implement a production session adapter against the chosen Cloudflare runtime bindings; do not import the spike's Worker-specific adapter blindly.
2. Add schema migrations and verify D1 constraints/indexes, atomic update semantics, backup/restore, and cleanup.
3. Configure cookie policy, key versions, idle/absolute lifetimes, and maximum payload size explicitly.
4. Test cookie contents, OAuth start/callback, duplicate concurrent callbacks, rotation, logout, Discord 401, expiry, key rotation, malformed ciphertext, D1 outage, and revision conflicts.
5. Prove no access token, refresh token, OAuth state, PKCE verifier, or CSRF secret appears in any browser cookie.
6. Prove all existing Flask auth/dashboard tests still pass and route-level permissions are unchanged.
7. Keep production on the current host until a separate cutover approval is given.

## Explicit non-goals

This proposal does not:

- Wire D1 sessions into the current production Flask app.
- Change the current Render startup path or production cookie format.
- Create Cloudflare Pages/Workers/D1 resources or secrets.
- Change DNS, OAuth redirect URIs, or production traffic routing.
- Move the Discord Gateway or MongoDB ownership into a Worker.
- Treat local spike smoke tests as production readiness evidence.

## Source implementation references

- `web/auth.py` and `web/oauth_utils.py`: current OAuth state/PKCE flow and timestamp validation.
- `web/app_factory.py`: production session-interface injection point.
- `cloudflare-spike/src/d1_session_interface.py`: Worker-only prototype for opaque cookies, encrypted records, expiry, revision CAS, state consumption, and revocation.
- `cloudflare-spike/src/worker.py`: local D1 and Web Crypto integration harness.
