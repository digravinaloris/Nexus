"""Local-only Flask/D1 runtime spike. Never import Nexus bot modules here."""

from __future__ import annotations

import hashlib
import secrets
import time

from flask import Flask, jsonify, request
from pyodide.ffi import run_sync
from workers import wsgi


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


Default = wsgi.entrypoint(app)
