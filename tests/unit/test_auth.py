import hashlib

import pytest

from odoo_mcp.auth import extract_bearer, key_fingerprint


@pytest.mark.parametrize(
    "header",
    [
        "Bearer 0123456789abcdef0123456789abcdef01234567",
        "bearer 0123456789abcdef0123456789abcdef01234567",
        "BEARER   0123456789abcdef0123456789abcdef01234567",
    ],
)
def test_extract_bearer_accepts_case_insensitive_scheme(header: str) -> None:
    assert extract_bearer(header) == "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "Token abcdef0123456789",
        "Bearer short",
        "Bearer " + "a" * 300,
        "Bearer abc def0123456789abcdef",
        "Bearer abc\r\nX-Injected: 1",
        "Bearer 0123456789abcdef\x00",
    ],
)
def test_extract_bearer_rejects_malformed_header(header: str | None) -> None:
    assert extract_bearer(header) is None


def test_fingerprint_is_short_sha256_prefix_and_never_the_key() -> None:
    key = "0123456789abcdef0123456789abcdef01234567"
    fingerprint = key_fingerprint(key)
    assert fingerprint == hashlib.sha256(key.encode()).hexdigest()[:8]
    assert key not in fingerprint
