"""HTTP smoke tests for the disposable local Python Worker."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.request import Request, urlopen


BASE_URL = os.environ.get("NEXUS_SPIKE_URL", "http://127.0.0.1:8787")


def call(path: str, method: str = "GET", payload: dict | None = None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = Request(f"{BASE_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))



def call_with_cookie(path: str, method: str = "GET", cookie: str | None = None):
    headers = {"Cookie": f"nexus_spike_session={cookie}"} if cookie else {}
    request = Request(f"{BASE_URL}{path}", headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8")), response.headers.get("Set-Cookie")
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8")), error.headers.get("Set-Cookie")


def extract_session_cookie(set_cookie: str | None) -> str:
    require(isinstance(set_cookie, str), "Flask did not issue an opaque session cookie")
    require("httponly" in set_cookie.lower(), "Session cookie is not HttpOnly")
    require("path=/" in set_cookie.lower(), "Session cookie path is not root")
    value = set_cookie.split(";", 1)[0].split("=", 1)[1]
    require(bool(value), "Session cookie is empty")
    return value


def require(condition: bool, message: str):
    if not condition:
        raise AssertionError(message)


def main():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            status, health = call("/health")
            if status == 200 and health.get("status") == "ok":
                break
        except Exception:
            time.sleep(0.5)
    else:
        raise AssertionError("Worker did not become healthy")

    status, setup = call("/__spike/setup", "POST", {})
    require(status == 200 and setup.get("ready"), f"D1 schema setup failed: {setup}")


    # Exercise Flask's real SessionInterface, not only the lower-level D1 primitives.
    status, login, set_cookie = call_with_cookie(
        "/__spike/flask-session/login", "POST"
    )
    require(status == 200 and login.get("authenticated") is True, "Flask D1 login failed")
    old_cookie = extract_session_cookie(set_cookie)
    require(len(old_cookie) == 43, "Browser cookie is not a 256-bit opaque session ID")
    require(
        "spike-only-discord-access-token" not in set_cookie
        and "spike-only-csrf-secret" not in set_cookie,
        "Browser cookie contains server-side session data",
    )

    status, identity, _ = call_with_cookie(
        "/__spike/flask-session/me", cookie=old_cookie
    )
    require(
        status == 200
        and identity.get("authenticated") is True
        and identity.get("access_token") == "spike-only-discord-access-token",
        "D1-backed Flask session could not be restored",
    )

    status, rotated, set_cookie = call_with_cookie(
        "/__spike/flask-session/rotate", "POST", old_cookie
    )
    require(status == 200 and rotated.get("rotated") is True, "Flask session rotation failed")
    new_cookie = extract_session_cookie(set_cookie)
    require(new_cookie != old_cookie, "Flask session ID did not rotate")

    status, old_identity, _ = call_with_cookie(
        "/__spike/flask-session/me", cookie=old_cookie
    )
    require(
        status == 200 and old_identity.get("authenticated") is False,
        "Pre-rotation Flask session remained usable",
    )
    status, new_identity, _ = call_with_cookie(
        "/__spike/flask-session/me", cookie=new_cookie
    )
    require(
        status == 200
        and new_identity.get("authenticated") is True
        and new_identity.get("access_token") == "spike-rotated-discord-access-token",
        "Rotated Flask session was not persisted",
    )

    status, tampered, _ = call_with_cookie(
        "/__spike/flask-session/tamper", "POST", new_cookie
    )
    require(status == 200 and tampered.get("tampered") is True, "Flask session tamper setup failed")
    status, rejected_cookie, delete_cookie = call_with_cookie(
        "/__spike/flask-session/me", cookie=new_cookie
    )
    require(
        status == 200 and rejected_cookie.get("authenticated") is False,
        "Flask session accepted tampered ciphertext",
    )
    require(
        isinstance(delete_cookie, str) and "max-age=0" in delete_cookie.lower(),
        "Tampered session cookie was not cleared",
    )

    # Recreate a valid session and verify immediate logout revocation.
    status, login, set_cookie = call_with_cookie(
        "/__spike/flask-session/login", "POST"
    )
    require(status == 200, "Second Flask D1 login failed")
    logout_cookie = extract_session_cookie(set_cookie)
    status, logout, delete_cookie = call_with_cookie(
        "/__spike/flask-session/logout", "POST", logout_cookie
    )
    require(status == 200 and logout.get("logged_out") is True, "Flask D1 logout failed")
    require(
        isinstance(delete_cookie, str) and "max-age=0" in delete_cookie.lower(),
        "Logout did not clear the browser cookie",
    )
    status, after_logout, _ = call_with_cookie(
        "/__spike/flask-session/me", cookie=logout_cookie
    )
    require(
        status == 200 and after_logout.get("authenticated") is False,
        "Logged-out Flask session remained usable",
    )

    status, state = call("/__spike/state", "POST", {})
    require(status == 200 and isinstance(state.get("state"), str), "State creation failed")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(call, "/__spike/state/consume", "POST", {"state": state["state"]})
            for _ in range(2)
        ]
        results = [future.result() for future in futures]
    require(all(status == 200 for status, _ in results), "State consume request failed")
    require(
        sum(1 for _, result in results if result.get("consumed") is True) == 1,
        "OAuth state was not consumed exactly once under concurrency",
    )

    status, session = call("/__spike/session", "POST", {})
    require(status == 200 and isinstance(session.get("session_id"), str), "Session creation failed")
    old_id = session["session_id"]

    status, rotated = call("/__spike/session/rotate", "POST", {"session_id": old_id})
    require(status == 200 and isinstance(rotated.get("session_id"), str), "Session rotation failed")
    new_id = rotated["session_id"]

    status, old_state = call(f"/__spike/session/{old_id}")
    require(status == 200 and old_state.get("active") is False, "Old session remained active")
    status, new_state = call(f"/__spike/session/{new_id}")
    require(status == 200 and new_state.get("active") is True, "New session is not active")

    status, rollback = call("/__spike/batch-rollback", "POST", {})
    require(status == 200 and rollback.get("rolled_back") is True, "D1 batch rollback failed")

    status, encrypted = call("/__spike/encrypted-session", "POST", {})
    require(
        status == 200 and isinstance(encrypted.get("session_id"), str),
        f"Encrypted D1 session creation failed: {encrypted}",
    )
    encrypted_id = encrypted["session_id"]
    status, encrypted_state = call(f"/__spike/encrypted-session/{encrypted_id}")
    require(
        status == 200 and encrypted_state.get("active") is True,
        "Persisted AES-GCM record could not be decrypted on a later request",
    )
    status, tampered = call(
        f"/__spike/encrypted-session/{encrypted_id}/tamper", "POST", {}
    )
    require(status == 200 and tampered.get("updated") is True, "Ciphertext tamper setup failed")
    status, rejected = call(f"/__spike/encrypted-session/{encrypted_id}")
    require(
        status == 200 and rejected.get("active") is False,
        "D1 session store accepted tampered ciphertext",
    )

    status, legacy = call("/__spike/encrypted-session/legacy", "POST", {})
    require(
        status == 200 and legacy.get("key_version") == 1,
        f"Legacy encrypted session creation failed: {legacy}",
    )
    legacy_id = legacy["session_id"]
    status, legacy_state = call(f"/__spike/encrypted-session/{legacy_id}")
    require(status == 200 and legacy_state.get("active") is True, "Version-1 session unreadable")
    status, rotated = call(
        f"/__spike/encrypted-session/{legacy_id}/rotate-key", "POST", {}
    )
    require(
        status == 200 and rotated.get("rotated") is True and rotated.get("key_version") == 2,
        f"Session key rotation failed: {rotated}",
    )
    status, rotated_state = call(f"/__spike/encrypted-session/{legacy_id}")
    require(
        status == 200 and rotated_state.get("active") is True,
        "Session became unreadable after key rotation",
    )

    status, revocable = call("/__spike/encrypted-session", "POST", {})
    require(status == 200, "Revocation fixture creation failed")
    revoke_id = revocable["session_id"]
    status, revoked = call(
        f"/__spike/encrypted-session/{revoke_id}/revoke", "POST", {}
    )
    require(status == 200 and revoked.get("revoked") is True, "Session revocation failed")
    status, revoked_state = call(f"/__spike/encrypted-session/{revoke_id}")
    require(
        status == 200 and revoked_state.get("active") is False,
        "Revoked session remained active",
    )

    status, idle = call("/__spike/encrypted-session", "POST", {})
    require(status == 200, "Idle-expiry fixture creation failed")
    idle_id = idle["session_id"]
    status, expired = call(
        f"/__spike/encrypted-session/{idle_id}/expire-idle", "POST", {}
    )
    require(status == 200 and expired.get("expired") is True, "Idle-expiry setup failed")
    status, idle_state = call(f"/__spike/encrypted-session/{idle_id}")
    require(
        status == 200 and idle_state.get("active") is False,
        "Idle-expired session remained active",
    )

    status, fetch_health = call("/__spike/fetch-client/health_snapshot", "POST", {})
    require(
        status == 200
        and fetch_health.get("result")
        == {"status": "alive", "guild_count": 12},
        f"Workers Fetch health snapshot failed: {fetch_health}",
    )
    status, fetch_public = call(
        "/__spike/fetch-client/public_status_snapshot", "POST", {}
    )
    require(
        status == 200
        and fetch_public.get("result")
        == {
            "online": True,
            "guild_count": 12,
            "latency_ms": 31,
            "uptime": "3h 12m",
        },
        f"Workers Fetch public status snapshot failed: {fetch_public}",
    )

    status, fetch_owner = call("/__spike/fetch-client/is_guild_owner", "POST", {})
    require(
        status == 200 and fetch_owner.get("result") is True,
        f"Signed guild-owner authorization check failed: {fetch_owner}",
    )
    status, fetch_non_owner = call(
        "/__spike/fetch-client/is_guild_owner_denied", "POST", {}
    )
    require(
        status == 200 and fetch_non_owner.get("result") is False,
        f"Signed guild-owner authorization accepted a non-owner: {fetch_non_owner}",
    )

    status, fetch_admin = call("/__spike/fetch-client/is_guild_admin", "POST", {})
    require(status == 200 and fetch_admin.get("result") is True, f"Signed guild Administrator authorization check failed: {fetch_admin}")
    status, fetch_non_admin = call("/__spike/fetch-client/is_guild_admin_denied", "POST", {})
    require(status == 200 and fetch_non_admin.get("result") is False, f"Signed guild Administrator authorization accepted a non-admin: {fetch_non_admin}")

    status, fetch_response_validation = call(
        "/__spike/fetch-client-response-validation", "POST", {}
    )
    require(
        status == 200 and fetch_response_validation.get("all_passed") is True,
        f"Workers Fetch client accepted an unsafe HTTP response: {fetch_response_validation}",
    )
    require(
        all(fetch_response_validation.get("checks", {}).values())
        and len(fetch_response_validation.get("checks", {})) == 4,
        f"Workers Fetch response rejection coverage is incomplete: {fetch_response_validation}",
    )


    status, fetch_validation = call(
        "/__spike/fetch-client-validation", "POST", {}
    )
    require(
        status == 200 and fetch_validation.get("all_passed") is True,
        f"Workers Fetch client accepted an unsafe configuration: {fetch_validation}",
    )
    require(
        all(fetch_validation.get("checks", {}).values())
        and len(fetch_validation.get("checks", {})) == 10,
        f"Workers Fetch client validation coverage is incomplete: {fetch_validation}",
    )


    status, crypto = call("/__spike/crypto", "POST", {})
    require(status == 200, f"Web Crypto request failed: {crypto}")
    require(crypto.get("sha256_matches") is True, "Web Crypto SHA-256 mismatch")
    require(crypto.get("round_trip") is True, "AES-GCM round trip failed")
    require(
        crypto.get("tampered_ciphertext_rejected") is True,
        "AES-GCM accepted tampered ciphertext",
    )
    require(crypto.get("wrong_key_rejected") is True, "AES-GCM accepted the wrong key")
    require(crypto.get("wrong_aad_rejected") is True, "AES-GCM accepted the wrong AAD")

    print("Cloudflare Python Worker smoke tests passed")


if __name__ == "__main__":
    main()
