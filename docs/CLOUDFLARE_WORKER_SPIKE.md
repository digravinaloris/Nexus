# Cloudflare Python Worker compatibility spike

Status: implementation gate; no production integration is authorized by this document.

## Purpose

Prove the smallest useful runtime path before adapting Nexus routes:

1. A minimal Flask application can be served through `workers.wsgi.entrypoint`.
2. A Python Worker can access a locally simulated D1 binding through the documented Python FFI.
3. D1 can enforce one-time state consumption and session rotation with conditional SQL and atomic batches.
4. The Worker can use Web Crypto (SHA-256 and AES-GCM) from Python without relying on an unverified native crypto wheel.
5. The same smoke checks run in GitHub Actions without Cloudflare account credentials or remote resources.

The spike must remain isolated from the existing application. It must not import `main.py`, `core`, `bridge`, `discord.py`, PyMongo, or any production route module.

## Verified platform contracts

- Flask is supported through `from workers import wsgi` and `Default = wsgi.entrypoint(app)`. Cloudflare's Flask example uses a `pyproject.toml`, `workers-py`, `workers-runtime-sdk`, and `uv run pywrangler dev`.
- Python Workers can access D1 through `self.env.DB`; prepared statements use `prepare(...).bind(...)`, and D1 operations are awaitable from Python.
- `D1Database.batch([...])` runs prepared statements sequentially as one SQL transaction. If a statement fails, D1 aborts or rolls back the batch. The Python binding API documents the same interface.
- Workers Web Crypto supports SHA-256 and AES-GCM. Python Workers expose JavaScript runtime APIs, including the Fetch API, through Pyodide FFI. Flask's synchronous handlers can bridge supported async runtime calls with `pyodide.ffi.run_sync`; the Worker-to-BotService Fetch path is now covered by a local runtime smoke test.
- Local Wrangler bindings use simulated resources by default. The spike must use local D1 only; it must not configure remote bindings or a real database ID.

Official references:

- Flask on Python Workers: https://developers.cloudflare.com/workers/languages/python/packages/flask/
- Python Workers FFI: https://developers.cloudflare.com/workers/languages/python/ffi/
- Query D1 from Python Workers: https://developers.cloudflare.com/d1/examples/query-d1-from-python-workers/
- D1 binding API and atomic batches: https://developers.cloudflare.com/d1/worker-api/d1-database/
- Workers Web Crypto: https://developers.cloudflare.com/workers/runtime-apis/web-crypto/
- Workers Fetch API: https://developers.cloudflare.com/workers/runtime-apis/fetch/

## Required isolated layout

The compatibility project should live under a dedicated directory (for example `cloudflare-spike/`) with its own `pyproject.toml`, Wrangler config, Worker entry point, SQL migration, and smoke-test script. Do not add Worker-only packages to the root bot requirements.

The local D1 configuration must use a clearly documented placeholder/local database identifier and local persistence directory. Do not enable `remote = true`, add production bindings, or create Cloudflare resources.

## Session proof cases

The spike is not a production session implementation. It must nevertheless prove the primitives needed by the later implementation:

- Generate at least 32 bytes of cryptographically secure random session-ID material; send only its opaque encoding to the browser and persist only its SHA-256 digest as the lookup key.
- Store only encrypted token test fixtures. Use AES-GCM with a fresh nonce and authenticated additional data that binds the ciphertext to the session digest and record/key version. Never use real Discord credentials in the spike.
- Create, read, rotate, and revoke records using D1 prepared statements.
- Consume an OAuth-state test record once with a conditional SQL write; concurrent/replayed attempts must produce exactly one successful consumer.
- Rotate a pre-auth session to a new session in an atomic D1 batch. Verify the old ID cannot read the new record and cannot be reused.
- Enforce idle and absolute expiry in the read query itself.
- A D1 failure must fail closed; no in-memory or client-cookie fallback is permitted.

D1 batches are transactional, but a successful batch does not by itself prove a conditional operation changed a row. Check returned change counts/rows and use conditional SQL plus uniqueness constraints for replay-sensitive operations. Add concurrent tests rather than assuming a sequential unit test proves race safety.

## CI acceptance criteria

- Install the isolated Worker dependencies without modifying the root `requirements.txt`.
- Start the local Worker with Wrangler and the local D1 binding.
- Exercise Flask routing, D1 schema setup, prepared statements, atomic batch rollback, one-time state consumption, rotation, expiry, and revocation over HTTP.
- Exercise SHA-256 and AES-GCM through the actual Worker runtime; include tampered-ciphertext and wrong-key failures.
- Run a parallel callback/replay test and assert exactly one request succeeds.
- Confirm the Worker bundle/import path does not load `main.py`, `core`, `bridge`, `discord.py`, or PyMongo.
- Keep the existing root pytest workflow unchanged and green.

If CI cannot execute the actual Python Worker runtime, mark the runtime proof as blocked and do not claim compatibility based only on static tests or CPython unit tests.

## Explicit non-goals

- No production route migration or OAuth session replacement in this spike.
- No Discord OAuth calls, real tokens, secrets, or credentials.
- No Cloudflare login, remote D1 binding, resource creation, deployment, DNS change, or production cutover.
- No change to the existing Flask/Waitress/Render startup path.

## Validation status

- PR #196 validates the isolated Flask WSGI entry point, local D1 prepared statements, conditional one-time state consumption under concurrency, session rotation, and transactional batch rollback in the actual Python Workers runtime.
- PR #197 validates Workers Web Crypto through Python FFI: SHA-256 parity, AES-256-GCM round-trip, and rejection of tampered ciphertext, an incorrect key, and incorrect authenticated additional data.
- PR #198 validates encrypted D1 record persistence across separate requests using only a local non-secret test key; the stored lookup key is a SHA-256 digest, ciphertext is AES-GCM protected with session/version-bound AAD, and tampering fails closed.
- PR #199 validates version-1 to version-2 key rotation, key-version-bound AAD, sliding idle expiry plus absolute expiry, and immediate revocation in the local Worker/D1 runtime.
- PR #200 extracts Flask construction into `web.app_factory.create_web_app()` and verifies the factory imports without `main.py`, discord.py, or PyMongo.
- PR #201 adds app-scoped BotService injection while preserving the existing in-process fallback; a signed remote adapter is still future work.
- PRs #203-#205 add versioned HMAC signing, Mongo-backed nonce replay protection and all-or-nothing app-factory registration. PR #207 adds strict environment configuration with a disabled-by-default gate; the endpoint remains inactive unless the operator explicitly supplies the enable flag and a valid keyring.
- PRs #206 and #208 add a signed `requests`-based client for non-Worker Python runtimes, limited to read-only health and public-status snapshots. That client is not used inside Cloudflare Workers.
- The current spike adds a separate `CloudflareFetchBotService` adapter that uses the Workers Fetch API through Python FFI, signs the exact request body, requires HTTPS except for an explicit local-only HTTP test option, rejects redirects/non-200 responses, and caps response bodies. Its local smoke test calls a mock internal endpoint running in the same Wrangler process; it does not contact the production bot host. The smoke suite also runs negative configuration checks inside the Worker runtime: it rejects plain HTTP, URL credentials/path/query/fragment, invalid key IDs, short HMAC secrets, non-local HTTP even with the test opt-in, local HTTP without the opt-in, and unallowlisted BotService methods. It also verifies the adapter rejects real local Fetch responses for redirects, non-200 status codes, malformed JSON, and response bodies over the configured 64 KiB limit.
- The D1/Web Crypto and Fetch transport proofs run in the dedicated local Wrangler GitHub Actions workflow without Cloudflare credentials or remote resources.

These results validate the runtime primitives only. They do not constitute a production session store: key provisioning and rotation, persistent ciphertext records, cookie integration, operational recovery, and full OAuth lifecycle tests remain separate requirements. No production routes or real Discord tokens have been migrated.


The app-factory extraction does not make the production web surface Worker-ready: route handlers still use the in-process BotService adapter for bot operations, and the current `main.py` process still owns the production Waitress server. The Cloudflare Fetch adapter is currently only a local runtime proof and is not wired into the production Worker app. Full BotService method parity, user-context authorization, OAuth server-side sessions, and the remaining persistence/runtime boundaries are still required.