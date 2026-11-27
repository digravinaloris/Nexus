"""Opt-in signed HTTP dispatch for the internal Nexus BotService boundary."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from flask import Blueprint, jsonify, request

from web.bot_service_signing import (
    BotServiceAuthError,
    CLOCK_SKEW_SECONDS,
    MAX_BODY_BYTES,
    verify_internal_request,
)


class NonceStore(Protocol):
    def consume(self, key_id: str, nonce: str, expires_at: int) -> bool:
        """Atomically claim a nonce; return False when it was already used."""


class MongoNonceStore:
    """Durable replay store backed by a Mongo collection with unique and TTL indexes."""

    def __init__(self, collection_provider: Callable[[], Any]):
        self._collection_provider = collection_provider
        self._indexes_ready = False
        self._index_lock = threading.Lock()

    def _collection(self):
        collection = self._collection_provider()
        if collection is None:
            raise RuntimeError("BotService nonce collection is unavailable")
        if not self._indexes_ready:
            with self._index_lock:
                if not self._indexes_ready:
                    collection.create_index(
                        [("key_id", 1), ("nonce", 1)],
                        unique=True,
                        name="uniq_bot_service_nonce",
                    )
                    collection.create_index(
                        "expires_at",
                        expireAfterSeconds=0,
                        name="ttl_bot_service_nonce",
                    )
                    self._indexes_ready = True
        return collection

    def consume(self, key_id: str, nonce: str, expires_at: int) -> bool:
        from pymongo.errors import DuplicateKeyError

        collection = self._collection()
        expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc)
        try:
            collection.insert_one(
                {
                    "key_id": key_id,
                    "nonce": nonce,
                    "expires_at": expiry,
                }
            )
        except DuplicateKeyError:
            return False
        return True


def create_internal_bot_service_blueprint(
    *,
    service,
    keyring: Mapping[str, bytes],
    nonce_store: NonceStore,
) -> Blueprint:
    """Create the opt-in v1 dispatch endpoint.

    Only the read-only health_snapshot method is exposed initially. Mutating
    operations must be added explicitly with their own DTO and authorization
    rules; arbitrary method names are never dispatched.
    """
    blueprint = Blueprint("nexus_internal_bot_service", __name__)

    @blueprint.post("/internal/v1/dispatch")
    def dispatch():
        if request.query_string:
            return jsonify({"error": "invalid internal request"}), 400
        if request.content_length is None or request.content_length > MAX_BODY_BYTES:
            return jsonify({"error": "invalid internal request"}), 413
        if request.mimetype != "application/json":
            return jsonify({"error": "unsupported content type"}), 415

        body = request.get_data(cache=False)
        if len(body) > MAX_BODY_BYTES:
            return jsonify({"error": "invalid internal request"}), 413

        try:
            claims = verify_internal_request(
                keyring=keyring,
                method=request.method,
                path=request.path,
                body=body,
                headers=request.headers,
            )
        except BotServiceAuthError:
            return jsonify({"error": "unauthorized"}), 401

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return jsonify({"error": "invalid internal request"}), 400

        if (
            not isinstance(payload, dict)
            or set(payload) != {"method", "args", "kwargs"}
            or payload.get("method") != "health_snapshot"
            or payload.get("args") != []
            or payload.get("kwargs") != {}
        ):
            return jsonify({"error": "unsupported internal operation"}), 403

        try:
            consumed = nonce_store.consume(
                claims.key_id,
                claims.nonce,
                claims.timestamp + CLOCK_SKEW_SECONDS,
            )
        except Exception:
            # Replay-store outages must never fall back to process memory.
            return jsonify({"error": "internal service unavailable"}), 503
        if not consumed:
            return jsonify({"error": "request already used"}), 409

        try:
            result = service.health_snapshot()
            return jsonify({"ok": True, "result": result})
        except Exception:
            return jsonify({"error": "internal service unavailable"}), 503

    return blueprint
