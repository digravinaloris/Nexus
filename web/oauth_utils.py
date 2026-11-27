"""Pure OAuth PKCE and state-validation helpers shared by web handlers and tests."""
import base64
import hashlib
import math
import re

OAUTH_STATE_MAX_AGE = 600


def create_code_challenge(code_verifier):
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def is_fresh_state_timestamp(created_at, now, max_age=OAUTH_STATE_MAX_AGE):
    return (
        not isinstance(created_at, bool)
        and isinstance(created_at, (int, float))
        and math.isfinite(created_at)
        and -30 <= now - created_at <= max_age
    )


def is_valid_code_verifier(verifier):
    return (
        isinstance(verifier, str)
        and 43 <= len(verifier) <= 128
        and re.fullmatch(r"[A-Za-z0-9._~-]+", verifier) is not None
    )


def is_valid_oauth_code(code):
    return (
        isinstance(code, str)
        and len(code) <= 2048
        and re.search(r"[\x00-\x20\x7f]", code) is None
    )
