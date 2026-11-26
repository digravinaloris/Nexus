"""Authenticated GitHub release webhook receiver for Nexus."""
import hashlib
import hmac
import json
import os

from flask import jsonify, request

import core
from bridge import run_coroutine
from web.routing import Routes

routes = Routes()
MAX_PAYLOAD_BYTES = 256 * 1024
REPOSITORY = "digravinaloris/Nexus"
RELEASE_PREFIX = "https://github.com/digravinaloris/Nexus/releases/"


def verify_signature(raw_body, signature, secret):
    if not isinstance(signature, str) or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def validate_release(payload):
    if not isinstance(payload, dict) or payload.get("action") != "published":
        return None
    repo = payload.get("repository")
    release = payload.get("release")
    if not isinstance(repo, dict) or repo.get("full_name") != REPOSITORY:
        return None
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        return None
    if not release.get("id") or not isinstance(release.get("tag_name"), str):
        return None
    url = release.get("html_url")
    if not isinstance(url, str) or len(url) > 500 or not url.startswith(RELEASE_PREFIX):
        return None
    return release


@routes.route("/webhooks/github/releases", methods=["POST"])
def github_release_webhook():
    secret = os.getenv("GITHUB_RELEASE_WEBHOOK_SECRET", "")
    if not secret:
        return jsonify({"error": "Webhook is not configured"}), 503
    if request.content_length is not None and request.content_length > MAX_PAYLOAD_BYTES:
        return jsonify({"error": "Payload too large"}), 413
    raw = request.get_data(cache=False)
    if len(raw) > MAX_PAYLOAD_BYTES:
        return jsonify({"error": "Payload too large"}), 413
    if not verify_signature(raw, request.headers.get("X-Hub-Signature-256"), secret):
        return jsonify({"error": "Invalid signature"}), 401
    if request.headers.get("X-GitHub-Event") != "release":
        return jsonify({"ignored": "event"}), 202
    delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()
    if not delivery_id or len(delivery_id) > 128:
        return jsonify({"error": "Missing or invalid delivery ID"}), 400
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return jsonify({"error": "Invalid JSON"}), 400
    release = validate_release(payload)
    if release is None:
        return jsonify({"ignored": "release"}), 202

    if core.db is None or core.bot is None or not core.bot.is_ready():
        return jsonify({"error": "Bot unavailable"}), 503
    deliveries = core.db["github_webhook_deliveries"]
    if deliveries.find_one({"_id": delivery_id}):
        return jsonify({"duplicate": True}), 200
    cog = core.bot.get_cog("GitHubUpdates")
    if cog is None:
        return jsonify({"error": "Release handler unavailable"}), 503
    try:
        sent = run_coroutine(cog.handle_webhook_release(release))
    except Exception:
        return jsonify({"error": "Release delivery failed"}), 503
    if not sent:
        return jsonify({"ignored": "not configured or already announced"}), 200
    try:
        deliveries.insert_one({"_id": delivery_id})
    except Exception:
        # The Discord message is already sent; a duplicate retry may be caught by release ID state.
        return jsonify({"ok": True, "duplicate_delivery_recorded": False}), 200
    return jsonify({"ok": True}), 200
