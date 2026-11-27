"""Unit tests for runtime-free OAuth validation helpers."""
import hashlib
import base64

from web.oauth_utils import (
    create_code_challenge,
    is_fresh_state_timestamp,
    is_valid_code_verifier,
    is_valid_oauth_code,
)



def test_code_challenge_is_sha256_base64url_without_padding():
    verifier = "A" * 43
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert create_code_challenge(verifier) == expected



def test_state_timestamp_bounds_and_invalid_types():
    now = 1000.0
    assert is_fresh_state_timestamp(now, now)
    assert is_fresh_state_timestamp(now - 600, now)
    assert is_fresh_state_timestamp(now + 30, now)
    assert not is_fresh_state_timestamp(now - 601, now)
    assert not is_fresh_state_timestamp(now + 31, now)
    assert not is_fresh_state_timestamp(True, now)
    assert not is_fresh_state_timestamp(float("nan"), now)
    assert not is_fresh_state_timestamp("1000", now)



def test_code_verifier_format():
    assert is_valid_code_verifier("A" * 43)
    assert is_valid_code_verifier("a._~-" * 10)
    assert not is_valid_code_verifier("short")
    assert not is_valid_code_verifier("A" * 129)
    assert not is_valid_code_verifier("bad verifier")



def test_authorization_code_rejects_control_characters_and_spaces():
    for value in ("bad code", "bad\ncode", "bad\x7fcode", "", "x" * 2049):
        assert not is_valid_oauth_code(value)
    assert is_valid_oauth_code("authorization-code_123")
