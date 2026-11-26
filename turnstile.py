"""Cloudflare Turnstile configuration and siteverify client.

This module is deliberately independent from the Discord bot and MongoDB so
public web routes can import it without loading the bot runtime.
"""
import os

import requests

from jsonlog import log_json

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_ATTEMPTS = 3


def site_key():
    return os.getenv("TURNSTILE_SITE_KEY", "").strip()


def _secret_key():
    return os.getenv("TURNSTILE_SECRET_KEY", "").strip()


def public_base_url():
    base = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not base:
        redirect_uri = os.getenv("DISCORD_REDIRECT_URI", "").strip()
        if redirect_uri.endswith("/callback"):
            base = redirect_uri[: -len("/callback")]
    return base.rstrip("/")


def turnstile_configured():
    """Fail closed unless both keys and a public base URL are configured."""
    return bool(site_key() and _secret_key() and public_base_url())


def verification_url(token):
    return f"{public_base_url()}/verify/{token}"


def check_turnstile(response_token, remote_ip=None):
    """Validate a response with Cloudflare; fail closed on provider errors."""
    secret = _secret_key()
    if not secret:
        return False, "not_configured"
    payload = {"secret": secret, "response": response_token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        data = requests.post(TURNSTILE_VERIFY_URL, data=payload, timeout=10).json()
    except (requests.RequestException, ValueError) as exc:
        log_json("error", "turnstile_unreachable", error=repr(exc))
        return False, "service_unavailable"
    if data.get("success") is True:
        return True, None
    return False, ",".join(data.get("error-codes", [])) or "failed"
