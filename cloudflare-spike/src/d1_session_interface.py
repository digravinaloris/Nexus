"""Flask server-side sessions backed by D1 for the isolated Worker spike.

This adapter is deliberately Worker-only: D1 and Web Crypto are injected by the
Worker entry point, so importing this module never touches bot/runtime modules.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time

from flask.sessions import CallbackDict, SessionInterface, SessionMixin

from crypto import aes_gcm_decrypt, aes_gcm_encrypt


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_MAX_SESSION_BYTES = 16_384


class SessionStorageUnavailable(RuntimeError):
    """A durable session operation failed; callers must return HTTP 503."""


class D1ServerSession(CallbackDict, SessionMixin):
    """Mutable Flask session carrying only server-side metadata in Python."""

    def __init__(
        self,
        initial=None,
        *,
        sid,
        new=False,
        invalid=False,
        unavailable=False,
        created_at=None,
        expires_at=None,
        revision=0,
    ):
        def on_update(_self):
            _self.modified = True

        self.sid = sid
        self.new = new
        self.modified = False
        self.invalid = invalid
        self.unavailable = unavailable
        self.created_at = created_at
        self.expires_at = expires_at
        self.revision = revision
        super().__init__(initial or {}, on_update)


class D1SessionInterface(SessionInterface):
    """Opaque-cookie Flask sessions stored as AES-GCM ciphertext in D1."""

    session_class = D1ServerSession

    def __init__(
        self,
        *,
        db_getter,
        awaiter,
        key_getter,
        active_key_version=2,
        idle_timeout=900,
        absolute_timeout=86400,
    ):
        self._db_getter = db_getter
        self._awaiter = awaiter
        self._key_getter = key_getter
        self.active_key_version = active_key_version
        self.idle_timeout = idle_timeout
        self.absolute_timeout = absolute_timeout

    @staticmethod
    def _digest(sid):
        return hashlib.sha256(sid.encode("ascii")).hexdigest()

    @staticmethod
    def _aad(session_hash, key_version):
        return f"nexus-flask-session:v{key_version}:{session_hash}".encode("ascii")

    @staticmethod
    def _row_value(row, name):
        try:
            return row[name]
        except (KeyError, TypeError):
            return getattr(row, name)

    def _new_session(self, *, invalid=False, unavailable=False):
        now = int(time.time())
        return self.session_class(
            sid=secrets.token_urlsafe(32),
            new=True,
            invalid=invalid,
            unavailable=unavailable,
            created_at=now,
            expires_at=now + self.absolute_timeout,
        )

    def _encrypt(self, sid, payload):
        session_hash = self._digest(sid)
        key_version = self.active_key_version
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        if len(plaintext) > _MAX_SESSION_BYTES:
            raise ValueError("session payload exceeds the configured limit")
        ciphertext = aes_gcm_encrypt(
            self._key_getter(key_version),
            nonce,
            plaintext,
            self._aad(session_hash, key_version),
        )
        return session_hash, ciphertext.hex(), nonce.hex(), key_version

    def open_session(self, app, request):
        cookie_name = app.config.get("SESSION_COOKIE_NAME", "__Host-nexus_session")
        sid = request.cookies.get(cookie_name)
        if not sid:
            return self._new_session()
        if not isinstance(sid, str) or not _SESSION_ID_RE.fullmatch(sid):
            return self._new_session(invalid=True)

        now = int(time.time())
        session_hash = self._digest(sid)
        try:
            row = self._awaiter(
                self._db_getter()
                .prepare(
                    """
                    UPDATE spike_flask_sessions
                    SET idle_expires_at = MIN(expires_at, ?)
                    WHERE session_hash = ? AND revoked_at IS NULL
                      AND expires_at > ? AND idle_expires_at > ?
                    RETURNING ciphertext_hex, nonce_hex, key_version,
                              created_at, expires_at, revision
                    """
                )
                .bind(now + self.idle_timeout, session_hash, now, now)
                .first()
            )
        except Exception:
            return self._new_session(unavailable=True)

        if row is None:
            return self._new_session(invalid=True)

        try:
            key_version = int(self._row_value(row, "key_version"))
            key = self._key_getter(key_version)
        except Exception:
            return self._new_session(unavailable=True)

        try:
            ciphertext = bytes.fromhex(self._row_value(row, "ciphertext_hex"))
            nonce = bytes.fromhex(self._row_value(row, "nonce_hex"))
            plaintext = aes_gcm_decrypt(
                key,
                nonce,
                ciphertext,
                self._aad(session_hash, key_version),
            )
            if len(plaintext) > _MAX_SESSION_BYTES:
                raise ValueError("stored session payload exceeds the configured limit")
            payload = json.loads(plaintext.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("stored session payload is not an object")
            return self.session_class(
                payload,
                sid=sid,
                new=False,
                created_at=int(self._row_value(row, "created_at")),
                expires_at=int(self._row_value(row, "expires_at")),
                revision=int(self._row_value(row, "revision")),
            )
        except Exception:
            # Corrupt or unauthenticated records are never treated as sessions.
            try:
                self._awaiter(
                    self._db_getter()
                    .prepare(
                        "UPDATE spike_flask_sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL"
                    )
                    .bind(now, session_hash)
                    .run()
                )
            except Exception:
                return self._new_session(unavailable=True)
            return self._new_session(invalid=True)

    def save_session(self, app, session, response):
        cookie_name = app.config.get("SESSION_COOKIE_NAME", "__Host-nexus_session")
        cookie_secure = app.config.get("SESSION_COOKIE_SECURE", True)

        if session.unavailable:
            response.status_code = 503
            response.set_data(b'{"error":"session storage unavailable"}')
            response.headers["Content-Type"] = "application/json"
            return

        if not session:
            if not session.new:
                try:
                    self.revoke_session(session)
                except SessionStorageUnavailable:
                    response.status_code = 503
                    response.set_data(b'{"error":"session storage unavailable"}')
                    response.headers["Content-Type"] = "application/json"
                    return
            if session.invalid or not session.new:
                response.delete_cookie(
                    cookie_name,
                    path="/",
                    domain=app.config.get("SESSION_COOKIE_DOMAIN"),
                    secure=cookie_secure,
                    httponly=True,
                    samesite="Lax",
                )
            return

        if not session.modified and not session.new:
            return

        now = int(time.time())
        created_at = session.created_at or now
        expires_at = session.expires_at or (now + self.absolute_timeout)
        if expires_at <= now:
            response.delete_cookie(cookie_name, path="/", secure=cookie_secure, httponly=True, samesite="Lax")
            return

        try:
            session_hash, ciphertext_hex, nonce_hex, key_version = self._encrypt(
                session.sid, dict(session)
            )
            idle_expires_at = min(expires_at, now + self.idle_timeout)
            if session.new:
                self._awaiter(
                    self._db_getter()
                    .prepare(
                        """
                        INSERT INTO spike_flask_sessions
                            (session_hash, ciphertext_hex, nonce_hex, key_version,
                             created_at, expires_at, idle_expires_at, revision)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                        """
                    )
                    .bind(
                        session_hash,
                        ciphertext_hex,
                        nonce_hex,
                        key_version,
                        created_at,
                        expires_at,
                        idle_expires_at,
                    )
                    .run()
                )
            else:
                updated = self._awaiter(
                    self._db_getter()
                    .prepare(
                        """
                        UPDATE spike_flask_sessions
                        SET ciphertext_hex = ?, nonce_hex = ?, key_version = ?,
                            idle_expires_at = MIN(expires_at, ?), revision = revision + 1
                        WHERE session_hash = ? AND revoked_at IS NULL
                          AND expires_at > ? AND idle_expires_at > ? AND revision = ?
                        RETURNING revision
                        """
                    )
                    .bind(
                        ciphertext_hex,
                        nonce_hex,
                        key_version,
                        now + self.idle_timeout,
                        session_hash,
                        now,
                        now,
                        session.revision,
                    )
                    .first()
                )
                if updated is None:
                    response.status_code = 503
                    response.set_data(b'{"error":"session storage unavailable"}')
                    response.headers["Content-Type"] = "application/json"
                    response.delete_cookie(
                        cookie_name,
                        path="/",
                        domain=app.config.get("SESSION_COOKIE_DOMAIN"),
                        secure=cookie_secure,
                        httponly=True,
                        samesite="Lax",
                    )
                    return
                session.revision = int(self._row_value(updated, "revision"))
        except Exception:
            response.status_code = 503
            response.set_data(b'{"error":"session storage unavailable"}')
            response.headers["Content-Type"] = "application/json"
            return

        session.new = False
        session.modified = False
        response.set_cookie(
            cookie_name,
            session.sid,
            max_age=max(0, expires_at - now),
            httponly=True,
            secure=cookie_secure,
            samesite="Lax",
            path="/",
            domain=app.config.get("SESSION_COOKIE_DOMAIN"),
        )

    def consume_oauth_state(self, session, expected_state):
        """Atomically consume the one-time OAuth state using a revision CAS.

        Concurrent callbacks may both load the same cookie, but only the first
        request can replace the encrypted payload at the revision it observed.
        """
        state = session.get("oauth_state")
        if (
            not isinstance(expected_state, str)
            or not expected_state
            or not isinstance(state, str)
            or not secrets.compare_digest(state, expected_state)
        ):
            return False

        payload = dict(session)
        payload.pop("oauth_state", None)
        payload.pop("oauth_state_created_at", None)
        payload.pop("oauth_code_verifier", None)
        session_hash, ciphertext_hex, nonce_hex, key_version = self._encrypt(
            session.sid, payload
        )
        now = int(time.time())
        try:
            updated = self._awaiter(
                self._db_getter()
                .prepare(
                    """
                    UPDATE spike_flask_sessions
                    SET ciphertext_hex = ?, nonce_hex = ?, key_version = ?,
                        revision = revision + 1
                    WHERE session_hash = ? AND revision = ? AND revoked_at IS NULL
                      AND expires_at > ? AND idle_expires_at > ?
                    RETURNING revision
                    """
                )
                .bind(
                    ciphertext_hex,
                    nonce_hex,
                    key_version,
                    session_hash,
                    session.revision,
                    now,
                    now,
                )
                .first()
            )
        except Exception as exc:
            raise SessionStorageUnavailable("D1 OAuth state consumption failed") from exc

        if updated is None:
            return False

        session.pop("oauth_state", None)
        session.pop("oauth_state_created_at", None)
        session.pop("oauth_code_verifier", None)
        session.revision = int(self._row_value(updated, "revision"))
        # The encrypted payload was already committed by the CAS above.
        session.modified = False
        return True

    def rotate_session(self, session):
        """Atomically revoke the pre-auth ID and create its replacement."""
        old_sid = session.sid
        old_hash = self._digest(old_sid)
        new_sid = secrets.token_urlsafe(32)
        new_hash = self._digest(new_sid)
        now = int(time.time())
        expires_at = now + self.absolute_timeout
        try:
            session_hash, ciphertext_hex, nonce_hex, key_version = self._encrypt(
                new_sid, dict(session)
            )
            db = self._db_getter()
            self._awaiter(
                db.batch(
                    [
                        db.prepare(
                            """
                            UPDATE spike_flask_sessions
                            SET revoked_at = ?, rotated_to = ?, revision = revision + 1
                            WHERE session_hash = ? AND revoked_at IS NULL
                              AND expires_at > ? AND idle_expires_at > ? AND revision = ?
                            """
                        ).bind(now, new_hash, old_hash, now, now, session.revision),
                        db.prepare(
                            """
                            INSERT INTO spike_flask_sessions
                                (session_hash, ciphertext_hex, nonce_hex, key_version,
                                 created_at, expires_at, idle_expires_at)
                            SELECT ?, ?, ?, ?, ?, ?, ?
                            WHERE EXISTS (
                                SELECT 1 FROM spike_flask_sessions
                                WHERE session_hash = ? AND rotated_to = ? AND revision = ?
                            )
                            """
                        ).bind(
                            new_hash,
                            ciphertext_hex,
                            nonce_hex,
                            key_version,
                            now,
                            expires_at,
                            now + self.idle_timeout,
                            old_hash,
                            new_hash,
                            session.revision + 1,
                        ),
                    ]
                )
            )
            created = self._awaiter(
                db.prepare(
                    """
                    SELECT session_hash FROM spike_flask_sessions
                    WHERE session_hash = ? AND revoked_at IS NULL AND expires_at > ?
                    LIMIT 1
                    """
                )
                .bind(new_hash, now)
                .first()
            )
        except Exception as exc:
            raise SessionStorageUnavailable("D1 session rotation failed") from exc
        if created is None:
            raise SessionStorageUnavailable("D1 session rotation was not committed")

        session.sid = new_sid
        session.new = False
        session.created_at = now
        session.expires_at = expires_at
        session.revision = 0
        session.modified = True

    def revoke_session(self, session):
        """Immediately revoke the opaque ID in D1."""
        sid = getattr(session, "sid", None)
        if not isinstance(sid, str) or not _SESSION_ID_RE.fullmatch(sid):
            return
        try:
            self._awaiter(
                self._db_getter()
                .prepare(
                    """
                    UPDATE spike_flask_sessions
                    SET revoked_at = ?
                    WHERE session_hash = ? AND revoked_at IS NULL
                    """
                )
                .bind(int(time.time()), self._digest(sid))
                .run()
            )
        except Exception as exc:
            raise SessionStorageUnavailable("D1 session revocation failed") from exc
