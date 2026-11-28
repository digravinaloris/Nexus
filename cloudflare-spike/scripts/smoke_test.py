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



def call_with_cookie(
    path: str,
    method: str = "GET",
    cookie: str | None = None,
    payload: dict | None = None,
):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Cookie": f"nexus_spike_session={cookie}"} if cookie else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"{BASE_URL}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8")), response.headers.get("Set-Cookie")
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8")), error.headers.get("Set-Cookie")


def extract_session_cookie(set_cookie: str | None) -> str:
    require(isinstance(set_cookie, str), "Flask did not issue an opaque session cookie")
    require("httponly" in set_cookie.lower(), "Session cookie is not HttpOnly")
    require("path=/" in set_cookie.lower(), "Session cookie path is not root")
    require("samesite=lax" in set_cookie.lower(), "Session cookie SameSite policy is not Lax")
    require("secure" not in set_cookie.lower(), "Local HTTP spike unexpectedly marked cookie Secure; local test transport is plain HTTP")
    require("domain=" not in set_cookie.lower(), "Session cookie is not host-only")
    require(set_cookie.split(";", 1)[0].startswith("nexus_spike_session="), "Unexpected session cookie name")
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


    # Malformed browser-supplied IDs must never be treated as authenticated
    # sessions, and the unusable cookie must be cleared immediately.
    status, malformed_identity, malformed_delete = call_with_cookie(
        "/__spike/flask-session/me", cookie="not-a-valid-session-id"
    )
    require(
        status == 200 and malformed_identity.get("authenticated") is False,
        "Malformed opaque session cookie was accepted",
    )
    require(
        isinstance(malformed_delete, str) and "max-age=0" in malformed_delete.lower(),
        "Malformed opaque session cookie was not cleared",
    )

    # Exercise Flask's real SessionInterface, not only the lower-level D1 primitives.
    status, login, set_cookie = call_with_cookie(
        "/__spike/flask-session/login", "POST"
    )
    require(status == 200 and login.get("authenticated") is True, "Flask D1 login failed")
    old_cookie = extract_session_cookie(set_cookie)
    cookie_attributes = set_cookie.lower()
    require("httponly" in cookie_attributes, "Session cookie is not HttpOnly")
    require("samesite=lax" in cookie_attributes, "Session cookie does not use SameSite=Lax")
    require("path=/" in cookie_attributes, "Session cookie path is not root")
    require("domain=" not in cookie_attributes, "Session cookie unexpectedly sets a Domain attribute")
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
    require(len(new_cookie) == 43, "Rotated browser cookie is not a 256-bit opaque session ID")
    require(
        "spike-rotated-discord-access-token" not in set_cookie
        and "spike-only-csrf-secret" not in set_cookie,
        "Rotated browser cookie contains server-side session data",
    )

    status, old_identity, old_cookie_delete = call_with_cookie(
        "/__spike/flask-session/me", cookie=old_cookie
    )
    require(
        status == 200 and old_identity.get("authenticated") is False,
        "Pre-rotation Flask session remained usable",
    )
    require(
        isinstance(old_cookie_delete, str)
        and "max-age=0" in old_cookie_delete.lower(),
        "Revoked pre-rotation browser cookie was not cleared",
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

    # Two concurrent requests using the same pre-rotation cookie must
    # never both rotate it successfully or create two active replacements.
    status, _, set_cookie = call_with_cookie(
        "/__spike/flask-session/login", "POST"
    )
    require(status == 200, "Concurrent-rotation Flask session setup failed")
    race_cookie = extract_session_cookie(set_cookie)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                call_with_cookie,
                "/__spike/flask-session/rotate",
                "POST",
                race_cookie,
            )
            for _ in range(2)
        ]
        race_results = [future.result() for future in futures]
    successful_rotations = [
        result for result in race_results
        if result[0] == 200 and result[1].get("rotated") is True
    ]
    failed_rotations = [
        result for result in race_results
        if result[0] == 503
        and result[1].get("error") == "session storage unavailable"
    ]
    require(
        len(successful_rotations) == 1 and len(failed_rotations) == 1,
        f"Concurrent session rotation was not single-winner: {race_results}",
    )
    race_new_cookie = extract_session_cookie(successful_rotations[0][2])
    require(
        race_new_cookie != race_cookie,
        "Concurrent session rotation reused the pre-auth cookie",
    )
    status, race_old_identity, race_old_cookie_delete = call_with_cookie(
        "/__spike/flask-session/me", cookie=race_cookie
    )
    require(
        status == 200 and race_old_identity.get("authenticated") is False,
        "Pre-rotation cookie remained usable after concurrent rotation",
    )
    require(
        isinstance(race_old_cookie_delete, str)
        and "max-age=0" in race_old_cookie_delete.lower(),
        "Concurrent rotation did not clear the revoked browser cookie",
    )
    status, race_new_identity, _ = call_with_cookie(
        "/__spike/flask-session/me", cookie=race_new_cookie
    )
    require(
        status == 200 and race_new_identity.get("authenticated") is True,
        "Winning concurrent rotation did not persist its replacement session",
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
    require("httponly" in delete_cookie.lower(), "Logout deletion cookie is not HttpOnly")
    require("samesite=lax" in delete_cookie.lower(), "Logout deletion cookie does not use SameSite=Lax")
    require("path=/" in delete_cookie.lower(), "Logout deletion cookie path is not root")

    # A session encrypted with an unavailable key version must fail closed
    # as a storage/configuration error, never become an anonymous success.
    status, unknown_key_fixture, set_cookie = call_with_cookie(
        "/__spike/flask-session/create-unknown-key", "POST"
    )
    require(
        status == 200 and unknown_key_fixture.get("key_version") == 99,
        f"Unknown-key session fixture failed: {unknown_key_fixture}",
    )
    unknown_key_cookie = extract_session_cookie(set_cookie)
    status, unknown_key_response, _ = call_with_cookie(
        "/__spike/flask-session/me", cookie=unknown_key_cookie
    )
    require(
        status == 503
        and unknown_key_response.get("error") == "session storage unavailable",
        f"Missing encryption key did not fail closed: {unknown_key_response}",
    )

    # A legacy Flask session must remain readable and be re-encrypted
    # with the active key version on its next successful write.
    status, legacy_flask, set_cookie = call_with_cookie(
        "/__spike/flask-session/create-legacy-key", "POST"
    )
    require(
        status == 200 and legacy_flask.get("key_version") == 1,
        f"Legacy Flask session fixture failed: {legacy_flask}",
    )
    legacy_flask_cookie = extract_session_cookie(set_cookie)
    status, legacy_version_before, _ = call_with_cookie(
        "/__spike/flask-session/key-version", cookie=legacy_flask_cookie
    )
    require(
        status == 200 and legacy_version_before.get("key_version") == 1,
        "Legacy Flask session was not stored with key version 1",
    )
    status, legacy_identity, _ = call_with_cookie(
        "/__spike/flask-session/me", cookie=legacy_flask_cookie
    )
    require(
        status == 200
        and legacy_identity.get("authenticated") is True
        and legacy_identity.get("access_token") == "spike-legacy-key-access-token",
        "Legacy-key Flask session could not be decrypted",
    )
    status, touched, _ = call_with_cookie(
        "/__spike/flask-session/touch-key-version",
        "POST",
        legacy_flask_cookie,
    )
    require(status == 200 and touched.get("touched") is True, "Legacy Flask session update failed")
    status, legacy_version_after, _ = call_with_cookie(
        "/__spike/flask-session/key-version", cookie=legacy_flask_cookie
    )
    require(
        status == 200 and legacy_version_after.get("key_version") == 2,
        "Updated legacy Flask session was not re-encrypted with active key version 2",
    )
    status, migrated_identity, _ = call_with_cookie(
        "/__spike/flask-session/me", cookie=legacy_flask_cookie
    )
    require(
        status == 200
        and migrated_identity.get("authenticated") is True
        and migrated_identity.get("access_token") == "spike-legacy-key-access-token",
        "Session became unreadable after encryption key migration",
    )

    # Invalid or oversized server-side payloads must fail closed without
    # issuing an authenticated browser cookie.
    for payload_kind in ("unserializable", "oversized"):
        status, invalid_payload, invalid_cookie = call_with_cookie(
            f"/__spike/flask-session/invalid-payload/{payload_kind}", "POST"
        )
        require(
            status == 503
            and invalid_payload.get("error") == "session storage unavailable",
            f"{payload_kind} session payload did not fail closed: {invalid_payload}",
        )
        require(
            not invalid_cookie or "nexus_spike_session=" not in invalid_cookie,
            f"{payload_kind} session payload unexpectedly issued a browser cookie",
        )

    # A stale request must not overwrite a newer D1 session revision.
    status, _, set_cookie = call_with_cookie(
        "/__spike/flask-session/login", "POST"
    )
    require(status == 200, "Stale-write Flask session setup failed")
    stale_cookie = extract_session_cookie(set_cookie)
    status, stale_write, stale_delete = call_with_cookie(
        "/__spike/flask-session/stale-write", "POST", stale_cookie
    )
    require(status == 200 and stale_write.get("stale") is True, "Stale-write conflict setup failed")
    require(
        isinstance(stale_delete, str) and "max-age=0" in stale_delete.lower(),
        "Revision-conflicted response did not clear its stale browser cookie",
    )
    status, stale_identity, _ = call_with_cookie(
        "/__spike/flask-session/me", cookie=stale_cookie
    )
    require(
        status == 200
        and stale_identity.get("authenticated") is True
        and "stale_write_probe" not in stale_identity,
        "Revision-conflicted write overwrote the persisted session payload",
    )

    status, storage_failure, _ = call_with_cookie(
        "/__spike/flask-session/storage-failure", "POST", logout_cookie
    )
    require(
        status == 503
        and storage_failure.get("error") == "session storage unavailable",
        "D1 session storage failure did not return a controlled 503",
    )

    status, after_logout, _ = call_with_cookie(
        "/__spike/flask-session/me", cookie=logout_cookie
    )
    require(
        status == 200 and after_logout.get("authenticated") is False,
        "Logged-out Flask session remained usable",
    )

    # Exercise idle and absolute expiry through Flask's real D1 SessionInterface.
    status, _, set_cookie = call_with_cookie(
        "/__spike/flask-session/login", "POST"
    )
    require(status == 200, "Idle-expiry Flask session setup failed")
    idle_cookie = extract_session_cookie(set_cookie)
    status, expired_idle, _ = call_with_cookie(
        "/__spike/flask-session/expire-idle", "POST", idle_cookie
    )
    require(status == 200 and expired_idle.get("expired") is True, "Flask idle-expiry setup failed")
    status, idle_identity, delete_cookie = call_with_cookie(
        "/__spike/flask-session/me", cookie=idle_cookie
    )
    require(
        status == 200 and idle_identity.get("authenticated") is False,
        "Idle-expired Flask session remained authenticated",
    )
    require(
        isinstance(delete_cookie, str) and "max-age=0" in delete_cookie.lower(),
        "Idle-expired Flask session cookie was not cleared",
    )

    status, _, set_cookie = call_with_cookie(
        "/__spike/flask-session/login", "POST"
    )
    require(status == 200, "Absolute-expiry Flask session setup failed")
    absolute_cookie = extract_session_cookie(set_cookie)
    status, expired_absolute, _ = call_with_cookie(
        "/__spike/flask-session/expire-absolute", "POST", absolute_cookie
    )
    require(
        status == 200 and expired_absolute.get("expired") is True,
        "Flask absolute-expiry setup failed",
    )
    status, absolute_identity, delete_cookie = call_with_cookie(
        "/__spike/flask-session/me", cookie=absolute_cookie
    )
    require(
        status == 200 and absolute_identity.get("authenticated") is False,
        "Absolute-expired Flask session remained authenticated",
    )
    require(
        isinstance(delete_cookie, str) and "max-age=0" in delete_cookie.lower(),
        "Absolute-expired Flask session cookie was not cleared",
    )

    # Two concurrent requests sharing the same pre-auth cookie must not
    # both consume the same OAuth state/PKCE verifier.
    status, oauth_setup, set_cookie = call_with_cookie(
        "/__spike/flask-session/oauth-login", "POST"
    )
    require(status == 200, "OAuth state session setup failed")
    oauth_cookie = extract_session_cookie(set_cookie)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                call_with_cookie,
                "/__spike/flask-session/oauth-consume",
                "POST",
                oauth_cookie,
                {"state": oauth_setup["state"]},
            )
            for _ in range(2)
        ]
        consume_results = [future.result() for future in futures]
    require(
        all(status == 200 for status, _, _ in consume_results),
        "Concurrent OAuth state consumption request failed",
    )
    require(
        sum(1 for _, result, _ in consume_results if result.get("consumed") is True) == 1,
        "OAuth state was not consumed exactly once by the D1 session CAS",
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

    status, fetch_lock_state = call("/__spike/fetch-client/public_global_lock_state", "POST", {})
    require(
        status == 200 and fetch_lock_state.get("result") is False,
        f"Signed public global lock state read failed: {fetch_lock_state}",
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

    status, fetch_presence = call("/__spike/fetch-client/guild_presence", "POST", {})
    require(
        status == 200
        and fetch_presence.get("result") == {"bot_ready": True, "guild_present": True},
        f"Signed guild presence read failed: {fetch_presence}",
    )
    status, fetch_name = call("/__spike/fetch-client/guild_name", "POST", {})
    require(
        status == 200 and fetch_name.get("result") == "Spike Test Server",
        f"Signed guild name read failed: {fetch_name}",
    )
    status, fetch_api_health = call("/__spike/fetch-client/api_health_snapshot", "POST", {})
    require(
        status == 200
        and fetch_api_health.get("result") == {"online": True, "locked": False, "guild_count": 1},
        f"Signed API health read failed: {fetch_api_health}",
    )
    status, fetch_api_guilds = call("/__spike/fetch-client/api_guild_list_snapshot", "POST", {})
    require(
        status == 200
        and fetch_api_guilds.get("result") == [{"id": "123", "name": "Spike Test Server"}],
        f"Signed API guild-list read failed: {fetch_api_guilds}",
    )
    status, fetch_candidates = call("/__spike/fetch-client/api_guild_id_candidates", "POST", {})
    require(
        status == 200 and fetch_candidates.get("result") == ["123", "456"],
        f"Signed API-key guild candidates read failed: {fetch_candidates}",
    )
    status, available = call("/__spike/fetch-client/available_guild_ids", "POST", {})
    require(
        status == 200 and available.get("result") == ["123"],
        f"Signed available guild filtering failed: {available}",
    )

    status, fetch_channels = call(
        "/__spike/fetch-client/api_text_channels_snapshot", "POST", {}
    )
    require(
        status == 200
        and fetch_channels.get("result") == [
            {"id": "456", "name": "general"},
            {"id": "789", "name": "staff-chat"},
        ],
        f"Signed text-channel snapshot failed: {fetch_channels}",
    )
    status, fetch_roles = call(
        "/__spike/fetch-client/api_roles_snapshot", "POST", {}
    )
    require(
        status == 200
        and fetch_roles.get("result") == [
            {"id": "321", "name": "Moderator"},
            {"id": "654", "name": "Member"},
        ],
        f"Signed role snapshot failed: {fetch_roles}",
    )
    status, invalid_roles = call(
        "/__spike/fetch-client/api_roles_snapshot_invalid", "POST", {}
    )
    require(
        status == 503
        and invalid_roles.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch client accepted malformed role DTOs: {invalid_roles}",
    )
    status, fetch_stats = call(
        "/__spike/fetch-client/api_guild_stats_snapshot", "POST", {}
    )
    expected_stats = {
        "guild_id": "123",
        "name": "Spike Test Server",
        "member_count": 25,
        "ban_count": 2,
        "muted_count": 1,
        "open_tickets_count": 3,
        "warned_count": 4,
        "warns_today": 1,
        "activity_7d": [0, 1, 2, 3, 4, 5, 6],
        "locked": False,
    }
    require(
        status == 200 and fetch_stats.get("result") == expected_stats,
        f"Signed guild stats snapshot failed: {fetch_stats}",
    )
    status, fetch_stats_with_category = call(
        "/__spike/fetch-client/api_guild_stats_snapshot_with_ticket_category",
        "POST",
        {},
    )
    require(
        status == 200 and fetch_stats_with_category.get("result") == expected_stats,
        f"Signed guild stats snapshot with ticket category failed: {fetch_stats_with_category}",
    )
    status, invalid_stats = call(
        "/__spike/fetch-client/api_guild_stats_snapshot_invalid", "POST", {}
    )
    require(
        status == 503
        and invalid_stats.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch client accepted malformed guild stats: {invalid_stats}",
    )
    status, invalid_stats_args = call(
        "/__spike/fetch-client/api_guild_stats_snapshot_invalid_args", "POST", {}
    )
    require(
        status == 200 and invalid_stats_args.get("result") == {"rejected": True},
        f"Workers Fetch client accepted invalid guild stats arguments: {invalid_stats_args}",
    )
    status, fetch_faq = call(
        "/__spike/fetch-client/api_faq_snapshot", "POST", {}
    )
    expected_faq = {
        "enabled": True,
        "entries": [
            {
                "id": "a" * 32,
                "keywords": ["help", "support"],
                "response": "Contact the support team.",
            }
        ],
    }
    require(
        status == 200 and fetch_faq.get("result") == expected_faq,
        f"Signed FAQ snapshot failed: {fetch_faq}",
    )
    status, invalid_faq = call(
        "/__spike/fetch-client/api_faq_snapshot_invalid", "POST", {}
    )
    require(
        status == 503
        and invalid_faq.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch client accepted malformed FAQ data: {invalid_faq}",
    )
    status, invalid_faq_args = call(
        "/__spike/fetch-client/api_faq_snapshot_invalid_args", "POST", {}
    )
    require(
        status == 200 and invalid_faq_args.get("result") == {"rejected": True},
        f"Workers Fetch client accepted invalid FAQ arguments: {invalid_faq_args}",
    )
    status, dashboard_config = call("/__spike/fetch-client/dashboard_config_snapshot", "POST", {})
    expected_dashboard_config = {"guild_id": "123", "language": "en", "tickets_enabled": True, "banned_words": ["spam"], "command_roles": {"ban": [321]}}
    require(status == 200 and dashboard_config.get("result") == expected_dashboard_config, f"Signed dashboard config snapshot failed: {dashboard_config}")
    status, invalid_dashboard_config = call("/__spike/fetch-client/dashboard_config_snapshot_invalid", "POST", {})
    require(status == 503 and invalid_dashboard_config.get("error") == "local Worker Fetch transport unavailable", f"Workers Fetch accepted unsafe dashboard config: {invalid_dashboard_config}")
    status, invalid_dashboard_args = call("/__spike/fetch-client/dashboard_config_snapshot_invalid_args", "POST", {})
    require(status == 200 and invalid_dashboard_args.get("result") == {"rejected": True}, f"Workers Fetch accepted invalid dashboard config arguments: {invalid_dashboard_args}")

    status, submitted_appeals = call("/__spike/fetch-client/dashboard_submitted_appeals_snapshot", "POST", {})
    require(
        status == 200
        and submitted_appeals.get("result") == [{
            "case_id": 12,
            "sanction_type": "ban",
            "user_name": "ExampleUser",
            "user_id": "123456789012345678",
            "ban_reason": "spam",
            "appeal_text": "Please review this ban.",
            "submitted_at": "Sat, 28 Nov 2026 09:30:00 GMT",
        }],
        f"Signed submitted appeals snapshot failed: {submitted_appeals}",
    )
    for suffix in ("invalid", "duplicate", "order"):
        status, rejected_appeals = call(f"/__spike/fetch-client/dashboard_submitted_appeals_snapshot_{suffix}", "POST", {})
        require(
            status == 503 and rejected_appeals.get("error") == "local Worker Fetch transport unavailable",
            f"Workers Fetch accepted invalid submitted appeals ({suffix}): {rejected_appeals}",
        )
    status, invalid_appeal_args = call("/__spike/fetch-client/dashboard_submitted_appeals_snapshot_invalid_args", "POST", {})
    require(
        status == 200 and invalid_appeal_args.get("result") == {"rejected": True},
        f"Workers Fetch accepted invalid submitted appeals arguments: {invalid_appeal_args}",
    )

    status, member_warnings = call("/__spike/fetch-client/api_member_warnings", "POST", {})
    require(
        status == 200 and member_warnings.get("result") == 2,
        f"Signed member warning count failed: {member_warnings}",
    )
    status, invalid_member_warnings = call("/__spike/fetch-client/api_member_warnings_invalid", "POST", {})
    require(
        status == 503 and invalid_member_warnings.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted malformed member warning count: {invalid_member_warnings}",
    )
    status, out_of_range_member_warnings = call("/__spike/fetch-client/api_member_warnings_out_of_range", "POST", {})
    require(
        status == 503 and out_of_range_member_warnings.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted out-of-range member warning count: {out_of_range_member_warnings}",
    )
    status, invalid_member_warning_args = call("/__spike/fetch-client/api_member_warnings_invalid_args", "POST", {})
    require(
        status == 200 and invalid_member_warning_args.get("result") == {"rejected": True},
        f"Workers Fetch accepted invalid member warning count arguments: {invalid_member_warning_args}",
    )

    status, fetch_warnlist = call("/__spike/fetch-client/api_warnlist_snapshot", "POST", {})
    require(
        status == 200 and fetch_warnlist.get("result") == [{"user_id": "123456789012345678", "count": 2}],
        f"Signed warning list snapshot failed: {fetch_warnlist}",
    )
    status, invalid_warnlist = call("/__spike/fetch-client/api_warnlist_snapshot_invalid", "POST", {})
    require(
        status == 503 and invalid_warnlist.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted malformed warning list: {invalid_warnlist}",
    )
    status, duplicate_warnlist = call("/__spike/fetch-client/api_warnlist_snapshot_duplicate", "POST", {})
    require(
        status == 503 and duplicate_warnlist.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted duplicate warning list entries: {duplicate_warnlist}",
    )
    status, invalid_warnlist_args = call("/__spike/fetch-client/api_warnlist_snapshot_invalid_args", "POST", {})
    require(
        status == 200 and invalid_warnlist_args.get("result") == {"rejected": True},
        f"Workers Fetch accepted invalid warning list arguments: {invalid_warnlist_args}",
    )

    status, fetch_sanction_cases = call("/__spike/fetch-client/api_sanction_cases_snapshot", "POST", {})
    require(
        status == 200
        and fetch_sanction_cases.get("result") == [{
            "case_id": 7,
            "type": "ban",
            "timestamp": "Sat, 28 Nov 2026 09:30:00 GMT",
            "user_id": "123456789012345678",
            "reason": "spam",
        }],
        f"Signed sanction cases snapshot failed: {fetch_sanction_cases}",
    )
    status, invalid_sanction_cases = call("/__spike/fetch-client/api_sanction_cases_snapshot_invalid", "POST", {})
    require(
        status == 503 and invalid_sanction_cases.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted malformed sanction cases: {invalid_sanction_cases}",
    )
    status, invalid_sanction_case_args = call("/__spike/fetch-client/api_sanction_cases_snapshot_invalid_args", "POST", {})
    require(
        status == 200 and invalid_sanction_case_args.get("result") == {"rejected": True},
        f"Workers Fetch accepted invalid sanction case arguments: {invalid_sanction_case_args}",
    )

    status, fetch_audit = call("/__spike/fetch-client/api_audit_snapshot", "POST", {})
    require(
        status == 200
        and fetch_audit.get("result") == [{
            "actor_name": "Moderator",
            "action": "Banned member",
            "details": "spam",
            "timestamp": "Sat, 28 Nov 2026 09:30:00 GMT",
        }],
        f"Signed audit snapshot failed: {fetch_audit}",
    )
    status, invalid_audit = call("/__spike/fetch-client/api_audit_snapshot_invalid", "POST", {})
    require(
        status == 503 and invalid_audit.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted malformed audit data: {invalid_audit}",
    )
    status, invalid_audit_args = call("/__spike/fetch-client/api_audit_snapshot_invalid_args", "POST", {})
    require(
        status == 200 and invalid_audit_args.get("result") == {"rejected": True},
        f"Workers Fetch accepted invalid audit arguments: {invalid_audit_args}",
    )

    status, fetch_calendar_duplicate = call("/__spike/fetch-client/dashboard_calendar_has_duplicate_event", "POST", {})
    require(
        status == 200 and fetch_calendar_duplicate.get("result") is False,
        f"Signed dashboard calendar duplicate check failed: {fetch_calendar_duplicate}",
    )
    status, invalid_calendar_duplicate = call("/__spike/fetch-client/dashboard_calendar_has_duplicate_event_invalid", "POST", {})
    require(
        status == 503 and invalid_calendar_duplicate.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted malformed calendar duplicate result: {invalid_calendar_duplicate}",
    )
    status, invalid_calendar_duplicate_args = call("/__spike/fetch-client/dashboard_calendar_has_duplicate_event_invalid_args", "POST", {})
    require(
        status == 200 and invalid_calendar_duplicate_args.get("result") == {"rejected": True},
        f"Workers Fetch accepted invalid calendar duplicate arguments: {invalid_calendar_duplicate_args}",
    )

    status, fetch_calendar_events = call("/__spike/fetch-client/dashboard_calendar_upcoming_events", "POST", {})
    calendar_result = fetch_calendar_events.get("result")
    require(
        status == 200
        and isinstance(calendar_result, list)
        and len(calendar_result) == 1
        and set(calendar_result[0]) == {"_id", "guild_id", "title", "description", "role_id", "starts_at", "timezone", "time_set"}
        and calendar_result[0]["guild_id"] == "123"
        and calendar_result[0]["title"] == "Community event"
        and isinstance(calendar_result[0]["starts_at"], str),
        f"Signed dashboard calendar events snapshot failed: {fetch_calendar_events}",
    )
    status, invalid_calendar_events = call("/__spike/fetch-client/dashboard_calendar_upcoming_events_invalid", "POST", {})
    require(
        status == 503 and invalid_calendar_events.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted malformed dashboard calendar events: {invalid_calendar_events}",
    )
    status, invalid_calendar_args = call("/__spike/fetch-client/dashboard_calendar_upcoming_events_invalid_args", "POST", {})
    require(
        status == 200 and invalid_calendar_args.get("result") == {"rejected": True},
        f"Workers Fetch accepted invalid dashboard calendar arguments: {invalid_calendar_args}",
    )

    status, fetch_dashboard_overview = call("/__spike/fetch-client/dashboard_overview_snapshot", "POST", {})
    expected_dashboard_overview = {
        "id": "123",
        "name": "Spike Test Server",
        "owner_id": "42",
        "text_channels": [{"id": "456", "name": "general"}],
        "roles": [{
            "id": "321",
            "name": "Moderator",
            "permissions": {
                "administrator": False,
                "manage_guild": True,
                "ban_members": False,
                "kick_members": False,
                "manage_roles": False,
            },
        }],
        "role_names": {"321": "Moderator", "654": "Member"},
        "open_tickets": [{"id": "789", "name": "ticket-1"}],
    }
    require(
        status == 200 and fetch_dashboard_overview.get("result") == expected_dashboard_overview,
        f"Signed dashboard overview snapshot failed: {fetch_dashboard_overview}",
    )
    status, invalid_dashboard_overview = call("/__spike/fetch-client/dashboard_overview_snapshot_invalid", "POST", {})
    require(
        status == 503 and invalid_dashboard_overview.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted malformed dashboard overview: {invalid_dashboard_overview}",
    )
    status, invalid_dashboard_overview_args = call("/__spike/fetch-client/dashboard_overview_snapshot_invalid_args", "POST", {})
    require(
        status == 200 and invalid_dashboard_overview_args.get("result") == {"rejected": True},
        f"Workers Fetch accepted invalid dashboard overview arguments: {invalid_dashboard_overview_args}",
    )

    status, fetch_sanction_stats = call("/__spike/fetch-client/dashboard_sanction_stats_snapshot", "POST", {})
    require(
        status == 200 and fetch_sanction_stats.get("result") == [["27/11", 0], ["28/11", 3]],
        f"Signed dashboard sanction stats snapshot failed: {fetch_sanction_stats}",
    )
    status, invalid_sanction_stats = call("/__spike/fetch-client/dashboard_sanction_stats_snapshot_invalid", "POST", {})
    require(
        status == 503 and invalid_sanction_stats.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted malformed dashboard sanction stats: {invalid_sanction_stats}",
    )
    status, invalid_sanction_stats_args = call("/__spike/fetch-client/dashboard_sanction_stats_snapshot_invalid_args", "POST", {})
    require(
        status == 200 and invalid_sanction_stats_args.get("result") == {"rejected": True},
        f"Workers Fetch accepted invalid dashboard sanction stats arguments: {invalid_sanction_stats_args}",
    )

    status, fetch_sanction_types = call("/__spike/fetch-client/dashboard_sanction_types_snapshot", "POST", {})
    require(status == 200 and fetch_sanction_types.get("result") == ["ban", "kick", "mute", "warn"], f"Signed dashboard sanction types snapshot failed: {fetch_sanction_types}")
    status, invalid_sanction_types = call("/__spike/fetch-client/dashboard_sanction_types_snapshot_invalid", "POST", {})
    require(status == 503 and invalid_sanction_types.get("error") == "local Worker Fetch transport unavailable", f"Workers Fetch accepted malformed dashboard sanction types: {invalid_sanction_types}")
    status, duplicate_sanction_types = call("/__spike/fetch-client/dashboard_sanction_types_snapshot_duplicate", "POST", {})
    require(status == 503 and duplicate_sanction_types.get("error") == "local Worker Fetch transport unavailable", f"Workers Fetch accepted duplicate dashboard sanction types: {duplicate_sanction_types}")
    status, invalid_sanction_type_args = call("/__spike/fetch-client/dashboard_sanction_types_snapshot_invalid_args", "POST", {})
    require(status == 200 and invalid_sanction_type_args.get("result") == {"rejected": True}, f"Workers Fetch accepted invalid dashboard sanction types arguments: {invalid_sanction_type_args}")

    status, fetch_presence_viewers = call("/__spike/fetch-client/dashboard_presence_viewers", "POST", {})
    require(
        status == 200 and fetch_presence_viewers.get("result") == ["Loris", "Alex"],
        f"Signed dashboard presence viewer read failed: {fetch_presence_viewers}",
    )
    status, invalid_presence_viewers = call("/__spike/fetch-client/dashboard_presence_viewers_invalid", "POST", {})
    require(
        status == 503 and invalid_presence_viewers.get("error") == "local Worker Fetch transport unavailable",
        "Malformed dashboard presence viewer response was accepted",
    )
    status, invalid_presence_viewer_args = call("/__spike/fetch-client/dashboard_presence_viewers_invalid_args", "POST", {})
    require(
        status == 200 and invalid_presence_viewer_args.get("result") == {"rejected": True},
        "Invalid dashboard presence viewer arguments were accepted",
    )

    status, fetch_dashboard_roles = call("/__spike/fetch-client/dashboard_roles_snapshot", "POST", {})
    expected_dashboard_roles = [{"id": "321", "name": "Moderator"}, {"id": "654", "name": "Member"}]
    require(status == 200 and fetch_dashboard_roles.get("result") == expected_dashboard_roles, f"Signed dashboard roles snapshot failed: {fetch_dashboard_roles}")
    status, invalid_dashboard_roles = call("/__spike/fetch-client/dashboard_roles_snapshot_invalid", "POST", {})
    require(status == 503 and invalid_dashboard_roles.get("error") == "local Worker Fetch transport unavailable", f"Workers Fetch accepted malformed dashboard roles: {invalid_dashboard_roles}")
    status, invalid_dashboard_roles_args = call("/__spike/fetch-client/dashboard_roles_snapshot_invalid_args", "POST", {})
    require(status == 200 and invalid_dashboard_roles_args.get("result") == {"rejected": True}, f"Workers Fetch accepted invalid dashboard roles arguments: {invalid_dashboard_roles_args}")

    status, fetch_public_templates = call("/__spike/fetch-client/dashboard_public_templates_snapshot", "POST", {})
    expected_public_templates = [{
        "code": "AbC_123-x",
        "name": "Welcome Pack",
        "created_guild_name": "Spike Test Server",
        "created_at": "Sat, 28 Nov 2026 09:30:00 GMT",
        "feature_toggle_count": 4,
        "command_role_count": 2,
    }]
    require(
        status == 200 and fetch_public_templates.get("result") == expected_public_templates,
        f"Signed public template snapshot failed: {fetch_public_templates}",
    )
    status, invalid_public_templates = call("/__spike/fetch-client/dashboard_public_templates_snapshot_invalid", "POST", {})
    require(
        status == 503 and invalid_public_templates.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted unsafe public template fields: {invalid_public_templates}",
    )
    status, duplicate_public_templates = call("/__spike/fetch-client/dashboard_public_templates_snapshot_duplicate", "POST", {})
    require(
        status == 503 and duplicate_public_templates.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted duplicate public template codes: {duplicate_public_templates}",
    )
    status, invalid_public_template_args = call("/__spike/fetch-client/dashboard_public_templates_snapshot_invalid_args", "POST", {})
    require(
        status == 200 and invalid_public_template_args.get("result") == {"rejected": True},
        f"Workers Fetch accepted invalid public template arguments: {invalid_public_template_args}",
    )

    status, template_detail = call("/__spike/fetch-client/dashboard_template_snapshot", "POST", {})
    expected_template_detail = {
        "code": "AbC_123-x",
        "name": "Welcome Pack",
        "created_guild_name": "Spike Test Server",
        "created_at": "2026-11-28T09:30:00Z",
        "snapshot": {
            "feature_toggles": {"tickets_enabled": True},
            "automod": {"automod_spam_count": 10, "banned_words": ["spam"]},
            "warn_escalation_enabled": False,
            "command_roles_by_name": {"ban": ["Moderators"]},
        },
    }
    require(
        status == 200 and template_detail.get("result") == expected_template_detail,
        f"Signed public template detail failed: {template_detail}",
    )
    status, invalid_template_detail = call("/__spike/fetch-client/dashboard_template_snapshot_invalid", "POST", {})
    require(
        status == 503 and invalid_template_detail.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch accepted malformed public template detail: {invalid_template_detail}",
    )
    status, invalid_template_detail_args = call("/__spike/fetch-client/dashboard_template_snapshot_invalid_args", "POST", {})
    require(
        status == 200 and invalid_template_detail_args.get("result") == {"rejected": True},
        f"Workers Fetch accepted invalid public template detail arguments: {invalid_template_detail_args}",
    )

    status, fetch_dashboard_settings = call("/__spike/fetch-client/dashboard_settings_snapshot", "POST", {})
    expected_dashboard_settings = {
        "id": "123", "name": "Spike Test Server", "owner_id": "42",
        "text_channels": [{"id": "456", "name": "general"}],
        "voice_channels": [{"id": "789", "name": "Lobby"}],
        "roles": [{"id": "321", "name": "Moderator"}],
    }
    require(status == 200 and fetch_dashboard_settings.get("result") == expected_dashboard_settings, f"Signed dashboard settings snapshot failed: {fetch_dashboard_settings}")
    status, invalid_dashboard_settings = call("/__spike/fetch-client/dashboard_settings_snapshot_invalid", "POST", {})
    require(status == 503 and invalid_dashboard_settings.get("error") == "local Worker Fetch transport unavailable", f"Workers Fetch accepted malformed dashboard settings: {invalid_dashboard_settings}")
    status, invalid_dashboard_settings_args = call("/__spike/fetch-client/dashboard_settings_snapshot_invalid_args", "POST", {})
    require(status == 200 and invalid_dashboard_settings_args.get("result") == {"rejected": True}, f"Workers Fetch accepted invalid dashboard settings arguments: {invalid_dashboard_settings_args}")

    status, fetch_config = call("/__spike/fetch-client/api_rest_config_snapshot", "POST", {})
    expected_config = {
        "guild_id": "123",
        "language": "en",
        "tickets_enabled": True,
        "welcome_banner_enabled": False,
        "command_roles": {"ban": ["321"]},
    }
    require(
        status == 200 and fetch_config.get("result") == expected_config,
        f"Signed REST config snapshot failed: {fetch_config}",
    )
    status, invalid_config = call("/__spike/fetch-client/api_rest_config_snapshot_invalid", "POST", {})
    require(
        status == 503
        and invalid_config.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch client accepted an unreviewed REST config field: {invalid_config}",
    )
    status, invalid_config_args = call("/__spike/fetch-client/api_rest_config_snapshot_invalid_args", "POST", {})
    require(
        status == 200 and invalid_config_args.get("result") == {"rejected": True},
        f"Workers Fetch client accepted invalid REST config arguments: {invalid_config_args}",
    )

    status, fetch_members = call(
        "/__spike/fetch-client/api_members_snapshot", "POST", {}
    )
    expected_members = [
        {
            "id": "123456789012345678",
            "name": "Sample Member",
            "username": "sample",
            "avatar_url": "https://cdn.discordapp.com/avatars/123456789012345678/sample.png",
            "is_muted": False,
        }
    ]
    require(
        status == 200 and fetch_members.get("result") == expected_members,
        f"Signed members snapshot failed: {fetch_members}",
    )
    status, invalid_members = call(
        "/__spike/fetch-client/api_members_snapshot_invalid", "POST", {}
    )
    require(
        status == 503
        and invalid_members.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch client accepted malformed members: {invalid_members}",
    )
    status, invalid_members_args = call(
        "/__spike/fetch-client/api_members_snapshot_invalid_args", "POST", {}
    )
    require(
        status == 200 and invalid_members_args.get("result") == {"rejected": True},
        f"Workers Fetch client accepted invalid members arguments: {invalid_members_args}",
    )
    status, invalid_channels = call(
        "/__spike/fetch-client/api_text_channels_snapshot_invalid", "POST", {}
    )
    require(
        status == 503
        and invalid_channels.get("error") == "local Worker Fetch transport unavailable",
        f"Workers Fetch client accepted malformed text-channel DTOs: {invalid_channels}",
    )

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
