"""Cloudflare Turnstile configuration and siteverify client.

This module is deliberately independent from the Discord bot and MongoDB so
public web routes can import it without loading the bot runtime.
"""
import importlib
import os

from jsonlog import log_json

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_ATTEMPTS = 3


def site_key(value=None):
    raw = os.getenv("TURNSTILE_SITE_KEY", "") if value is None else value
    return str(raw).strip()


def _secret_key(value=None):
    raw = os.getenv("TURNSTILE_SECRET_KEY", "") if value is None else value
    return str(raw).strip()


def public_base_url(value=None):
    if value is None:
        base = os.getenv("PUBLIC_BASE_URL", "").strip()
        if not base:
            redirect_uri = os.getenv("DISCORD_REDIRECT_URI", "").strip()
            if redirect_uri.endswith("/callback"):
                base = redirect_uri[: -len("/callback")]
    else:
        base = str(value).strip()
    return base.rstrip("/")


def turnstile_configured(site_key_value=None, secret_key_value=None, public_base_url_value=None):
    """Fail closed unless both keys and a public base URL are configured."""
    return bool(
        site_key(site_key_value)
        and _secret_key(secret_key_value)
        and public_base_url(public_base_url_value)
    )


def verification_url(token):
    return f"{public_base_url()}/verify/{token}"


def check_turnstile(response_token, remote_ip=None, http_client=None, secret_key=None):
    """Validate a response with Cloudflare; fail closed on provider errors.

    http_client may provide a requests-compatible post method. Keeping the
    default import lazy lets the standalone web app inject a Worker-native
    transport without importing the requests package.
    """
    secret = _secret_key(secret_key)
    if not secret:
        return False, "not_configured"
    payload = {"secret": secret, "response": response_token}
    if remote_ip:
        payload["remoteip"] = remote_ip
    try:
        client = http_client if http_client is not None else importlib.import_module("requests")
        data = client.post(TURNSTILE_VERIFY_URL, data=payload, timeout=10).json()
    except Exception as exc:
        log_json("error", "turnstile_unreachable", error=repr(exc))
        return False, "service_unavailable"
    if not isinstance(data, dict):
        log_json("error", "turnstile_invalid_response", response_type=type(data).__name__)
        return False, "service_unavailable"
    if data.get("success") is True:
        return True, None
    error_codes = data.get("error-codes", [])
    if not isinstance(error_codes, list) or not all(isinstance(code, str) for code in error_codes):
        return False, "failed"
    return False, ",".join(error_codes) or "failed"
