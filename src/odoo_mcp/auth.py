"""Bearer token extraction and safe fingerprinting.

The server never validates keys itself; Odoo does. This module only makes
sure a syntactically sane token is present and gives a log-safe identifier.
"""

import hashlib
import re

# RFC 6750 b64token, with a length window that covers Odoo's 40-char hex keys.
_BEARER = re.compile(r"^bearer +([A-Za-z0-9\-._~+/]{16,256}=*)$", re.IGNORECASE)


def extract_bearer(header: str | None) -> str | None:
    """Return the token of a well-formed `Authorization: Bearer` header, else None."""
    if not header:
        return None
    match = _BEARER.fullmatch(header)
    return match.group(1) if match else None


def key_digest(key: str) -> str:
    """Full SHA-256 of the key, for in-memory bucketing (never logged)."""
    return hashlib.sha256(key.encode()).hexdigest()


def key_fingerprint(key: str) -> str:
    """Short, log-safe identifier of a key: first 8 hex chars of its SHA-256."""
    return key_digest(key)[:8]
