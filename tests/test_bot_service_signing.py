import pytest

from web.bot_service_signing import (
    BotServiceAuthError,
    CLOCK_SKEW_SECONDS,
    HEADER_KEY_ID,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    MAX_BODY_BYTES,
    sign_internal_request,
    verify_internal_request,
)


SECRET = b"current-test-secret-material-32-bytes!"
OLD_SECRET = b"previous-test-secret-material-32bytes"
PATH = "/internal/v1/dispatch"
BODY = b'{"method":"health_snapshot","args":[],"kwargs":{}}'


def _signed(secret=SECRET, key_id="current", timestamp=1, nonce="n" * 24, body=BODY):
    return sign_internal_request(
        secret=secret,
        key_id=key_id,
        method="POST",
        path=PATH,
        body=body,
        timestamp=timestamp,
        nonce=nonce,
    )


def test_valid_signature_returns_claims_for_durable_replay_check():
    headers = _signed()
    claims = verify_internal_request(
        keyring={"current": SECRET},
        method="POST",
        path=PATH,
        body=BODY,
        headers=headers,
        now=1,
    )

    assert claims.key_id == "current"
    assert claims.timestamp == 1
    assert claims.nonce == "n" * 24


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", PATH, BODY),
        ("POST", "/internal/v1/other", BODY),
        ("POST", PATH, BODY + b" "),
    ],
)
def test_signature_binds_method_path_and_exact_body(method, path, body):
    with pytest.raises(BotServiceAuthError):
        verify_internal_request(
            keyring={"current": SECRET},
            method=method,
            path=path,
            body=body,
            headers=_signed(),
            now=1,
        )


@pytest.mark.parametrize("timestamp", [1 - CLOCK_SKEW_SECONDS - 1, 1 + CLOCK_SKEW_SECONDS + 1])
def test_expired_and_future_timestamps_are_rejected(timestamp):
    with pytest.raises(BotServiceAuthError):
        verify_internal_request(
            keyring={"current": SECRET},
            method="POST",
            path=PATH,
            body=BODY,
            headers=_signed(timestamp=timestamp),
            now=1,
        )


def test_previous_key_id_can_be_accepted_during_rotation():
    headers = _signed(secret=OLD_SECRET, key_id="previous")
    claims = verify_internal_request(
        keyring={"current": SECRET, "previous": OLD_SECRET},
        method="POST",
        path=PATH,
        body=BODY,
        headers=headers,
        now=1,
    )
    assert claims.key_id == "previous"


def test_unknown_key_and_altered_signature_are_rejected():
    headers = _signed()
    with pytest.raises(BotServiceAuthError):
        verify_internal_request(
            keyring={"other": SECRET},
            method="POST",
            path=PATH,
            body=BODY,
            headers=headers,
            now=1,
        )

    altered = dict(headers)
    altered[HEADER_SIGNATURE] = "0" * 64
    with pytest.raises(BotServiceAuthError):
        verify_internal_request(
            keyring={"current": SECRET},
            method="POST",
            path=PATH,
            body=BODY,
            headers=altered,
            now=1,
        )


def test_malformed_headers_targets_and_nonce_are_rejected():
    headers = _signed()
    malformed = dict(headers)
    malformed[HEADER_TIMESTAMP] = "01"
    with pytest.raises(BotServiceAuthError):
        verify_internal_request(
            keyring={"current": SECRET},
            method="POST",
            path=PATH,
            body=BODY,
            headers=malformed,
            now=1,
        )

    malformed = dict(headers)
    malformed[HEADER_NONCE] = "short"
    with pytest.raises(BotServiceAuthError):
        verify_internal_request(
            keyring={"current": SECRET},
            method="POST",
            path=PATH,
            body=BODY,
            headers=malformed,
            now=1,
        )

    with pytest.raises(BotServiceAuthError):
        verify_internal_request(
            keyring={"current": SECRET},
            method="POST",
            path=PATH + "?guild=1",
            body=BODY,
            headers=headers,
            now=1,
        )


def test_invalid_secret_and_oversized_body_fail_closed():
    with pytest.raises(ValueError):
        sign_internal_request(
            secret=b"short",
            key_id="current",
            method="POST",
            path=PATH,
            body=BODY,
            timestamp=1,
            nonce="n" * 24,
        )

    with pytest.raises(BotServiceAuthError):
        verify_internal_request(
            keyring={"current": b"short"},
            method="POST",
            path=PATH,
            body=BODY,
            headers=_signed(),
            now=1,
        )

    with pytest.raises(BotServiceAuthError):
        sign_internal_request(
            secret=SECRET,
            key_id="current",
            method="POST",
            path=PATH,
            body=b"x" * (MAX_BODY_BYTES + 1),
            timestamp=1,
            nonce="n" * 24,
        )


def test_signer_generates_nonce_and_verifier_returns_it():
    headers = sign_internal_request(
        secret=SECRET,
        key_id="current",
        method="POST",
        path=PATH,
        body=BODY,
        timestamp=123,
    )
    assert headers[HEADER_KEY_ID] == "current"
    claims = verify_internal_request(
        keyring={"current": SECRET},
        method="POST",
        path=PATH,
        body=BODY,
        headers=headers,
        now=123,
    )
    assert claims.nonce == headers[HEADER_NONCE]
    assert len(claims.nonce) >= 24
