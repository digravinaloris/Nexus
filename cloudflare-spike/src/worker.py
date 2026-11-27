"""Local-only Flask/D1 runtime spike. Never import Nexus bot modules here."""

from __future__ import annotations

import hashlib
import secrets
import time

from flask import Flask, jsonify, request
from pyodide.ffi import run_sync
from workers import wsgi

from crypto import aes_gcm_decrypt, aes_gcm_encrypt, sha256 as webcrypto_sha256


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


def _test_encryption_key() -> bytes:
    """Read the disposable key supplied by the local smoke workflow only."""
    raw_key = str(request.environ["workers.env"].SPIKE_TEST_ENCRYPTION_KEY)
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


@app.post("/__spike/encrypted-session")
def create_encrypted_session():
    """Persist only ciphertext; the raw opaque ID is returned once for the test."""
    raw_id = secrets.token_urlsafe(32)
    session_hash = _digest(raw_id)
    key_version = 1
    nonce = secrets.token_bytes(12)
    fixture = b"nexus-cloudflare-spike-persisted-non-sensitive-fixture"
    try:
        ciphertext = aes_gcm_encrypt(
            _test_encryption_key(),
            nonce,
            fixture,
            _session_aad(session_hash, key_version),
        )
        now = int(time.time())
        _await(
            _db()
            .prepare(
                """
                INSERT INTO spike_encrypted_sessions
                    (session_hash, ciphertext_hex, nonce_hex, key_version, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """
            )
            .bind(
                session_hash,
                ciphertext.hex(),
                nonce.hex(),
                key_version,
                now + 3600,
            )
            .run()
        )
    except Exception as exc:
        print(f"Local encrypted D1 session creation failed: {type(exc).__name__}: {exc}", flush=True)
        return jsonify({"error": "local encrypted session unavailable"}), 503
    return jsonify({"session_id": raw_id})


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
                SELECT ciphertext_hex, nonce_hex, key_version
                FROM spike_encrypted_sessions
                WHERE session_hash = ? AND revoked_at IS NULL AND expires_at > ?
                LIMIT 1
                """
            )
            .bind(session_hash, now)
            .first()
        )
        if row is None:
            return jsonify({"active": False})
        key_version = int(_row_value(row, "key_version"))
        ciphertext = bytes.fromhex(_row_value(row, "ciphertext_hex"))
        nonce = bytes.fromhex(_row_value(row, "nonce_hex"))
        plaintext = aes_gcm_decrypt(
            _test_encryption_key(),
            nonce,
            ciphertext,
            _session_aad(session_hash, key_version),
        )
    except Exception:
        # Missing keys, malformed rows, and authentication failures all fail closed.
        return jsonify({"active": False})
    fixture = b"nexus-cloudflare-spike-persisted-non-sensitive-fixture"
    return jsonify({"active": plaintext == fixture})


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
