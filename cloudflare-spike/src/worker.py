"""Local-only Flask/D1 runtime spike. Never import Nexus bot modules here."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time

from flask import Flask, Response, jsonify, request
from pyodide.ffi import run_sync
from workers import wsgi

from crypto import aes_gcm_decrypt, aes_gcm_encrypt, sha256 as webcrypto_sha256
from bot_service_client import (
    CloudflareBotServiceError,
    CloudflareFetchBotService,
    MAX_RESPONSE_BYTES,
)


app = Flask(__name__)


def _db():
    """Return the D1 binding supplied to this Worker request."""
    return request.environ["workers.env"].DB


def _await(promise):
    """Bridge a Workers async binding call into a synchronous Flask view."""
    return run_sync(promise)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_body():
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else {}


def _test_bot_service_secret() -> bytes:
    raw = str(request.environ["workers.env"].SPIKE_TEST_BOT_SERVICE_HMAC_SECRET)
    secret = bytes.fromhex(raw)
    if len(secret) < 32:
        raise RuntimeError("local BotService test key must contain at least 32 bytes")
    return secret


@app.post("/internal/v1/dispatch")
def mock_internal_bot_service():
    """Local-only mock of the remote bot host; verifies the real signed wire format."""
    body = request.get_data(cache=False)
    headers = request.headers
    key_id = headers.get("X-Nexus-Key-Id", "")
    timestamp_text = headers.get("X-Nexus-Timestamp", "")
    nonce = headers.get("X-Nexus-Nonce", "")
    signature = headers.get("X-Nexus-Signature", "")
    if (
        key_id not in {
            "spike-test",
            "spike-redirect",
            "spike-unavailable",
            "spike-malformed",
            "spike-oversized",
        }
        or not re.fullmatch(r"0|[1-9][0-9]{0,11}", timestamp_text)
        or not re.fullmatch(r"[A-Za-z0-9_-]{24,128}", nonce)
        or not re.fullmatch(r"[a-f0-9]{64}", signature)
        or abs(int(time.time()) - int(timestamp_text)) > 60
        or len(body) > 1_048_576
    ):
        return jsonify({"error": "unauthorized"}), 401

    canonical = "\n".join(
        (
            "NEXUS-BOT-SERVICE-V1",
            key_id,
            request.method,
            "/internal/v1/dispatch",
            timestamp_text,
            nonce,
            hashlib.sha256(body).hexdigest(),
        )
    ).encode("utf-8")
    expected = hmac.new(_test_bot_service_secret(), canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return jsonify({"error": "unauthorized"}), 401

    # These response modes are disposable local fixtures used only by the smoke test.
    if key_id == "spike-redirect":
        return Response("", status=302, headers={"Location": "https://bot.example/"})
    if key_id == "spike-unavailable":
        return jsonify({"error": "unavailable"}), 503
    if key_id == "spike-malformed":
        return Response("{malformed-json", status=200, mimetype="application/json")
    if key_id == "spike-oversized":
        oversized = '{"result":"' + ("x" * (MAX_RESPONSE_BYTES + 32)) + '"}'
        return Response(oversized, status=200, mimetype="application/json")

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"error": "invalid request"}), 400
    if (
        not isinstance(payload, dict)
        or set(payload) != {"method", "args", "kwargs"}
        or not isinstance(payload.get("args"), list)
        or not isinstance(payload.get("kwargs"), dict)
        or payload.get("kwargs") != {}
    ):
        return jsonify({"error": "unsupported operation"}), 403

    results = {
        "health_snapshot": {"status": "alive", "guild_count": 12},
        "public_status_snapshot": {
            "online": True,
            "guild_count": 12,
            "latency_ms": 31,
            "uptime": "3h 12m",
        },
    }
    method = payload.get("method")
    args = payload["args"]
    if isinstance(method, str) and method in results and args == []:
        return jsonify({"ok": True, "result": results[method]})
    if (
        isinstance(method, str)
        and method in {"is_guild_owner", "is_guild_admin"}
        and len(args) == 2
        and all(isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value) for value in args)
    ):
        return jsonify({"ok": True, "result": args[0] == "123" and args[1] in {"42", "43"}})
    return jsonify({"error": "unsupported operation"}), 403


@app.post("/__spike/fetch-client/<method>")
def exercise_fetch_bot_service_client(method):
    """Call the local mock bot host through Workers Fetch from inside the Worker."""
    try:
        env = request.environ["workers.env"]
        client = CloudflareFetchBotService(
            base_url="http://127.0.0.1:8787",
            key_id="spike-test",
            secret=_test_bot_service_secret(),
            allow_local_http=True,
        )
        if method == "health_snapshot":
            result = client.health_snapshot()
        elif method == "public_status_snapshot":
            result = client.public_status_snapshot()
        elif method == "is_guild_owner":
            result = client.is_guild_owner("123", "42")
        elif method == "is_guild_owner_denied":
            result = client.is_guild_owner("123", "43")
        elif method == "is_guild_admin":
            result = client.is_guild_admin("123", "43")
        elif method == "is_guild_admin_denied":
            result = client.is_guild_admin("123", "44")
        else:
            return jsonify({"error": "unsupported operation"}), 403
    except Exception as exc:
        print(f"Local Worker Fetch BotService probe failed: {type(exc).__name__}", flush=True)
        return jsonify({"error": "local Worker Fetch transport unavailable"}), 503
    return jsonify({"result": result})


@app.post("/__spike/fetch-client-response-validation")
def validate_fetch_bot_service_error_responses():
    """Exercise rejected HTTP responses through the actual Workers Fetch adapter."""
    checks = {}
    for key_id, name in (
        ("spike-redirect", "rejects_redirect"),
        ("spike-unavailable", "rejects_non_200"),
        ("spike-malformed", "rejects_malformed_json"),
        ("spike-oversized", "rejects_oversized_body"),
    ):
        try:
            client = CloudflareFetchBotService(
                base_url="http://127.0.0.1:8787",
                key_id=key_id,
                secret=_test_bot_service_secret(),
                allow_local_http=True,
            )
            client.health_snapshot()
            checks[name] = False
        except CloudflareBotServiceError:
            checks[name] = True
        except Exception as exc:
            print(
                f"Unexpected local Fetch rejection type: {type(exc).__name__}",
                flush=True,
            )
            checks[name] = False
    return jsonify({"checks": checks, "all_passed": all(checks.values())})


@app.post("/__spike/fetch-client-validation")
def validate_fetch_bot_service_configuration():
    """Exercise fail-closed client configuration checks in the Worker runtime."""
    valid = {
        "key_id": "spike-test",
        "secret": b"x" * 32,
    }
    checks = {}

    def rejected(name, **overrides):
        options = {**valid, **overrides}
        try:
            CloudflareFetchBotService(
                base_url=options.pop("base_url", "https://bot.example"),
                **options,
            )
        except ValueError:
            checks[name] = True
        else:
            checks[name] = False

    rejected("rejects_plain_http_origin", base_url="http://bot.example")
    rejected("rejects_origin_credentials", base_url="https://user:pass@bot.example")
    rejected("rejects_origin_path", base_url="https://bot.example/internal")
    rejected("rejects_origin_query", base_url="https://bot.example?debug=1")
    rejected("rejects_origin_fragment", base_url="https://bot.example#fragment")
    rejected("rejects_invalid_key_id", key_id="invalid/key")
    rejected("rejects_short_secret", secret=b"x" * 31)
    rejected("rejects_local_http_without_opt_in", base_url="http://127.0.0.1:8787")
    rejected(
        "rejects_nonlocal_http_with_opt_in",
        base_url="http://bot.example",
        allow_local_http=True,
    )

    try:
        client = CloudflareFetchBotService(
            base_url="https://bot.example",
            key_id=valid["key_id"],
            secret=valid["secret"],
        )
        client._dispatch_snapshot("api_ban_member")
        checks["rejects_unallowlisted_operation"] = False
    except ValueError:
        checks["rejects_unallowlisted_operation"] = True

    return jsonify({"checks": checks, "all_passed": all(checks.values())})


@app.get("/")
@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "nexus-cloudflare-spike"})


@app.post("/__spike/setup")
def setup_schema():
    """Create disposable local-only tables used by the smoke test."""
    schema = [
        """
        CREATE TABLE IF NOT EXISTS spike_oauth_states (
            state_hash TEXT PRIMARY KEY,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS spike_sessions (
            session_hash TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked_at INTEGER,
            rotated_to TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS spike_batch_probe (
            probe_key TEXT PRIMARY KEY
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS spike_encrypted_sessions (
            session_hash TEXT PRIMARY KEY,
            ciphertext_hex TEXT NOT NULL,
            nonce_hex TEXT NOT NULL,
            key_version INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            idle_expires_at INTEGER NOT NULL,
            revoked_at INTEGER
        )
        """,
    ]
    try:
        db = _db()
        for statement in schema:
            _await(db.prepare(statement).run())
    except Exception as exc:
        print(f"Local D1 schema setup failed: {type(exc).__name__}: {exc}", flush=True)
        return jsonify({"error": "local D1 setup failed", "cause": type(exc).__name__}), 503
    return jsonify({"ready": True})


@app.post("/__spike/state")
def create_state():
    raw_state = secrets.token_urlsafe(32)
    now = int(time.time())
    try:
        _await(
            _db()
            .prepare(
                "INSERT INTO spike_oauth_states (state_hash, expires_at) VALUES (?, ?)"
            )
            .bind(_digest(raw_state), now + 120)
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"state": raw_state})


@app.post("/__spike/state/consume")
def consume_state():
    raw_state = _json_body().get("state")
    if not isinstance(raw_state, str) or not raw_state or len(raw_state) > 256:
        return jsonify({"error": "invalid state"}), 400

    now = int(time.time())
    try:
        row = _await(
            _db()
            .prepare(
                """
                UPDATE spike_oauth_states
                SET consumed_at = ?
                WHERE state_hash = ? AND consumed_at IS NULL AND expires_at > ?
                RETURNING state_hash
                """
            )
            .bind(now, _digest(raw_state), now)
            .first()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"consumed": row is not None})


@app.post("/__spike/session")
def create_session():
    raw_id = secrets.token_urlsafe(32)
    now = int(time.time())
    try:
        _await(
            _db()
            .prepare(
                """
                INSERT INTO spike_sessions (session_hash, payload, expires_at)
                VALUES (?, ?, ?)
                """
            )
            .bind(_digest(raw_id), "non-sensitive-spike-fixture", now + 3600)
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"session_id": raw_id})


@app.post("/__spike/session/rotate")
def rotate_session():
    old_id = _json_body().get("session_id")
    if not isinstance(old_id, str) or not old_id or len(old_id) > 256:
        return jsonify({"error": "invalid session"}), 400

    old_hash = _digest(old_id)
    new_id = secrets.token_urlsafe(32)
    new_hash = _digest(new_id)
    now = int(time.time())
    db = _db()
    try:
        # The conditional update claims the old record for this unique new ID.
        # The following INSERT is conditional on that exact claim. D1 batch
        # makes both statements one transaction, so partial rotation rolls back.
        _await(
            db.batch(
                [
                    db.prepare(
                        """
                        UPDATE spike_sessions
                        SET revoked_at = ?, rotated_to = ?
                        WHERE session_hash = ? AND revoked_at IS NULL AND expires_at > ?
                        """
                    ).bind(now, new_hash, old_hash, now),
                    db.prepare(
                        """
                        INSERT INTO spike_sessions (session_hash, payload, expires_at)
                        SELECT ?, ?, ?
                        WHERE EXISTS (
                            SELECT 1 FROM spike_sessions
                            WHERE session_hash = ? AND rotated_to = ?
                        )
                        """
                    ).bind(
                        new_hash,
                        "non-sensitive-spike-fixture",
                        now + 3600,
                        old_hash,
                        new_hash,
                    ),
                ]
            )
        )
        created = _await(
            db.prepare(
                """
                SELECT session_hash FROM spike_sessions
                WHERE session_hash = ? AND revoked_at IS NULL AND expires_at > ?
                LIMIT 1
                """
            )
            .bind(new_hash, now)
            .first()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503

    if created is None:
        return jsonify({"error": "session could not be rotated"}), 409
    return jsonify({"session_id": new_id})


@app.get("/__spike/session/<session_id>")
def read_session(session_id):
    if not isinstance(session_id, str) or len(session_id) > 256:
        return jsonify({"active": False})
    now = int(time.time())
    try:
        row = _await(
            _db()
            .prepare(
                """
                SELECT session_hash FROM spike_sessions
                WHERE session_hash = ? AND revoked_at IS NULL AND expires_at > ?
                LIMIT 1
                """
            )
            .bind(_digest(session_id), now)
            .first()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"active": row is not None})


@app.post("/__spike/batch-rollback")
def batch_rollback():
    """Prove that a failed D1 batch does not leave its first write committed."""
    marker = secrets.token_hex(16)
    db = _db()
    try:
        _await(
            db.batch(
                [
                    db.prepare(
                        "INSERT INTO spike_batch_probe (probe_key) VALUES (?)"
                    ).bind(marker),
                    db.prepare(
                        "INSERT INTO spike_batch_probe (probe_key) VALUES (?)"
                    ).bind(marker),
                ]
            )
        )
        return jsonify({"rolled_back": False}), 500
    except Exception:
        try:
            count = _await(
                db.prepare(
                    "SELECT COUNT(*) AS count FROM spike_batch_probe WHERE probe_key = ?"
                )
                .bind(marker)
                .first("count")
            )
        except Exception:
            return jsonify({"error": "local D1 unavailable"}), 503
        return jsonify({"rolled_back": count == 0})


def _test_encryption_key(key_version: int) -> bytes:
    """Read one disposable versioned key supplied by the local smoke workflow."""
    env = request.environ["workers.env"]
    raw_key = str(getattr(env, f"SPIKE_TEST_ENCRYPTION_KEY_V{key_version}"))
    try:
        key = bytes.fromhex(raw_key)
    except ValueError as exc:
        raise RuntimeError("local test encryption key is malformed") from exc
    if len(key) != 32:
        raise RuntimeError("local test encryption key must be 32 bytes")
    return key

def _session_aad(session_hash: str, key_version: int) -> bytes:
    return f"nexus-spike:session:v{key_version}:{session_hash}".encode("ascii")


def _row_value(row, name):
    try:
        return row[name]
    except (KeyError, TypeError):
        return getattr(row, name)


def _create_encrypted_session(key_version: int):
    raw_id = secrets.token_urlsafe(32)
    session_hash = _digest(raw_id)
    nonce = secrets.token_bytes(12)
    fixture = b"nexus-cloudflare-spike-persisted-non-sensitive-fixture"
    now = int(time.time())
    ciphertext = aes_gcm_encrypt(
        _test_encryption_key(key_version),
        nonce,
        fixture,
        _session_aad(session_hash, key_version),
    )
    _await(
        _db()
        .prepare(
            """
            INSERT INTO spike_encrypted_sessions
                (session_hash, ciphertext_hex, nonce_hex, key_version,
                 expires_at, idle_expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """
        )
        .bind(
            session_hash,
            ciphertext.hex(),
            nonce.hex(),
            key_version,
            now + 3600,
            now + 900,
        )
        .run()
    )
    return raw_id


@app.post("/__spike/encrypted-session")
def create_encrypted_session():
    """Create new records with the active local test key (version 2)."""
    try:
        raw_id = _create_encrypted_session(2)
    except Exception as exc:
        print(f"Local encrypted D1 session creation failed: {type(exc).__name__}: {exc}", flush=True)
        return jsonify({"error": "local encrypted session unavailable"}), 503
    return jsonify({"session_id": raw_id, "key_version": 2})


@app.post("/__spike/encrypted-session/legacy")
def create_legacy_encrypted_session():
    """Create a version-1 fixture to exercise a version-1 to version-2 rotation."""
    try:
        raw_id = _create_encrypted_session(1)
    except Exception:
        return jsonify({"error": "local encrypted session unavailable"}), 503
    return jsonify({"session_id": raw_id, "key_version": 1})

@app.get("/__spike/encrypted-session/<session_id>")
def read_encrypted_session(session_id):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        return jsonify({"active": False})
    now = int(time.time())
    session_hash = _digest(session_id)
    try:
        row = _await(
            _db()
            .prepare(
                """
                UPDATE spike_encrypted_sessions
                SET idle_expires_at = MIN(expires_at, ?)
                WHERE session_hash = ? AND revoked_at IS NULL
                  AND expires_at > ? AND idle_expires_at > ?
                RETURNING ciphertext_hex, nonce_hex, key_version
                """
            )
            .bind(now + 900, session_hash, now, now)
            .first()
        )
        if row is None:
            return jsonify({"active": False})
        key_version = int(_row_value(row, "key_version"))
        ciphertext = bytes.fromhex(_row_value(row, "ciphertext_hex"))
        nonce = bytes.fromhex(_row_value(row, "nonce_hex"))
        plaintext = aes_gcm_decrypt(
            _test_encryption_key(key_version),
            nonce,
            ciphertext,
            _session_aad(session_hash, key_version),
        )
    except Exception:
        # Missing keys, malformed rows, expiry, and authentication failures fail closed.
        return jsonify({"active": False})
    fixture = b"nexus-cloudflare-spike-persisted-non-sensitive-fixture"
    return jsonify({"active": plaintext == fixture})


@app.post("/__spike/encrypted-session/<session_id>/rotate-key")
def rotate_encrypted_session_key(session_id):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        return jsonify({"rotated": False}), 400
    now = int(time.time())
    session_hash = _digest(session_id)
    db = _db()
    try:
        row = _await(
            db.prepare(
                """
                SELECT ciphertext_hex, nonce_hex, key_version
                FROM spike_encrypted_sessions
                WHERE session_hash = ? AND revoked_at IS NULL
                  AND expires_at > ? AND idle_expires_at > ?
                LIMIT 1
                """
            )
            .bind(session_hash, now, now)
            .first()
        )
        if row is None:
            return jsonify({"rotated": False}), 404
        old_version = int(_row_value(row, "key_version"))
        if old_version == 2:
            return jsonify({"rotated": False, "key_version": 2})
        ciphertext = bytes.fromhex(_row_value(row, "ciphertext_hex"))
        old_nonce = bytes.fromhex(_row_value(row, "nonce_hex"))
        plaintext = aes_gcm_decrypt(
            _test_encryption_key(old_version),
            old_nonce,
            ciphertext,
            _session_aad(session_hash, old_version),
        )
        new_version = 2
        new_nonce = secrets.token_bytes(12)
        new_ciphertext = aes_gcm_encrypt(
            _test_encryption_key(new_version),
            new_nonce,
            plaintext,
            _session_aad(session_hash, new_version),
        )
        _await(
            db.prepare(
                """
                UPDATE spike_encrypted_sessions
                SET ciphertext_hex = ?, nonce_hex = ?, key_version = ?
                WHERE session_hash = ? AND key_version = ? AND revoked_at IS NULL
                  AND expires_at > ? AND idle_expires_at > ?
                """
            )
            .bind(
                new_ciphertext.hex(),
                new_nonce.hex(),
                new_version,
                session_hash,
                old_version,
                now,
                now,
            )
            .run()
        )
        current = _await(
            db.prepare(
                "SELECT key_version FROM spike_encrypted_sessions WHERE session_hash = ?"
            )
            .bind(session_hash)
            .first()
        )
        current_version = int(_row_value(current, "key_version")) if current else None
    except Exception:
        return jsonify({"error": "local key rotation unavailable"}), 503
    if current_version != new_version:
        return jsonify({"rotated": False}), 409
    return jsonify({"rotated": True, "key_version": new_version})


@app.post("/__spike/encrypted-session/<session_id>/revoke")
def revoke_encrypted_session(session_id):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        return jsonify({"revoked": False}), 400
    try:
        _await(
            _db()
            .prepare(
                "UPDATE spike_encrypted_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL"
            )
            .bind(int(time.time()), _digest(session_id))
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"revoked": True})


@app.post("/__spike/encrypted-session/<session_id>/expire-idle")
def expire_encrypted_session_idle(session_id):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        return jsonify({"expired": False}), 400
    try:
        _await(
            _db()
            .prepare(
                "UPDATE spike_encrypted_sessions SET idle_expires_at = ? WHERE session_hash = ?"
            )
            .bind(int(time.time()) - 1, _digest(session_id))
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"expired": True})

@app.post("/__spike/encrypted-session/<session_id>/tamper")
def tamper_encrypted_session(session_id):
    if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
        return jsonify({"updated": False}), 400
    try:
        result = _await(
            _db()
            .prepare(
                """
                UPDATE spike_encrypted_sessions
                SET ciphertext_hex = '00'
                WHERE session_hash = ? AND revoked_at IS NULL
                """
            )
            .bind(_digest(session_id))
            .run()
        )
    except Exception:
        return jsonify({"error": "local D1 unavailable"}), 503
    return jsonify({"updated": True})


@app.post("/__spike/crypto")
def crypto_compatibility():
    """Exercise Workers Web Crypto through Python FFI with disposable fixtures."""
    import hashlib

    fixture = b"nexus-cloudflare-spike-non-sensitive-fixture"
    key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    aad = b"nexus-spike:v1:session-digest-fixture"
    try:
        digest_matches = webcrypto_sha256(fixture) == hashlib.sha256(fixture).digest()
        ciphertext = aes_gcm_encrypt(key, nonce, fixture, aad)
        round_trip = aes_gcm_decrypt(key, nonce, ciphertext, aad) == fixture

        tampered = bytearray(ciphertext)
        tampered[-1] ^= 1
        try:
            aes_gcm_decrypt(key, nonce, bytes(tampered), aad)
            tampered_rejected = False
        except Exception:
            tampered_rejected = True

        wrong_key = bytearray(key)
        wrong_key[0] ^= 1
        try:
            aes_gcm_decrypt(bytes(wrong_key), nonce, ciphertext, aad)
            wrong_key_rejected = False
        except Exception:
            wrong_key_rejected = True

        try:
            aes_gcm_decrypt(key, nonce, ciphertext, aad + b"-wrong")
            wrong_aad_rejected = False
        except Exception:
            wrong_aad_rejected = True
    except Exception as exc:
        print(f"Local Web Crypto compatibility failed: {type(exc).__name__}: {exc}", flush=True)
        return jsonify({"error": "local Web Crypto unavailable", "cause": type(exc).__name__}), 503

    return jsonify(
        {
            "sha256_matches": digest_matches,
            "round_trip": round_trip,
            "tampered_ciphertext_rejected": tampered_rejected,
            "wrong_key_rejected": wrong_key_rejected,
            "wrong_aad_rejected": wrong_aad_rejected,
        }
    )


Default = wsgi.entrypoint(app)
