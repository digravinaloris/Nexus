"""Authenticated GitHub release webhook receiver for Nexus."""
import hashlib
import hmac
import json
import re
from urllib.parse import urlsplit

from flask import current_app, jsonify, request

from web.bot_service import get_bot_service
from web.routing import Routes

routes = Routes()
MAX_PAYLOAD_BYTES = 256 * 1024
REPOSITORY = "digravinaloris/Nexus"
RELEASE_PATH_RE = re.compile(r"^/digravinaloris/Nexus/releases/(?:tag/)?[^/]+$")


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
    if (
        isinstance(release.get("id"), bool)
        or not isinstance(release.get("id"), int)
        or release["id"] <= 0
        or not isinstance(release.get("tag_name"), str)
        or not release["tag_name"].strip()
        or len(release["tag_name"]) > 255
    ):
        return None
    url = release.get("html_url")
    if not isinstance(url, str) or len(url) > 500:
        return None
    try:
        parsed_url = urlsplit(url)
    except ValueError:
        return None
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "github.com"
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.port not in (None, 443)
        or parsed_url.query
        or parsed_url.fragment
        or not RELEASE_PATH_RE.fullmatch(parsed_url.path)
    ):
        return None
    return release


@routes.route("/webhooks/github/releases", methods=["POST"])
def github_release_webhook():
    secret = current_app.config.get("GITHUB_RELEASE_WEBHOOK_SECRET", "")
    if not secret:
        return jsonify({"error": "Webhook is not configured"}), 503
    if request.content_length is not None and request.content_length > MAX_PAYLOAD_BYTES:
        return jsonify({"error": "Payload too large"}), 413
    # Read at most one byte beyond the limit so chunked or lengthless requests
    # cannot force an unbounded body allocation before validation.
    raw = request.stream.read(MAX_PAYLOAD_BYTES + 1)
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

    result = get_bot_service().api_handle_github_release(delivery_id, release)
    if result == "unavailable":
        return jsonify({"error": "Bot unavailable"}), 503
    if result == "duplicate":
        return jsonify({"duplicate": True}), 200
    if result == "handler_unavailable":
        return jsonify({"error": "Release handler unavailable"}), 503
    if result == "delivery_failed":
        return jsonify({"error": "Release delivery failed"}), 503
    if result == "ignored":
        return jsonify({"ignored": "not configured or already announced"}), 200
    if result == "record_failed":
        return jsonify({"ok": True, "duplicate_delivery_recorded": False}), 200
    return jsonify({"ok": True}), 200
