"""Check a caller's API key with Odoo before any MCP method runs.

The gateway cannot tell a valid Odoo API key from a made-up one by its syntax,
so it asks Odoo once per key and caches the clear answers ("valid", "rejected")
for a short time. That time is also the longest a key revoked in Odoo keeps
working here. Keys that are not in the cache share one global budget of checks
per minute: rotating made-up keys cannot reach Odoo more often than that.
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from enum import Enum
from typing import Any, Protocol

from odoo_mcp.auth import key_digest
from odoo_mcp.odoo_client import OdooAccessError, OdooAuthError, OdooError
from odoo_mcp.ratelimit import RateLimiter

# Cheap, side-effect-free method every Odoo user may call on their own user.
KEY_CHECK_MODEL = "res.users"
KEY_CHECK_METHOD = "context_get"
_ALL_UNVERIFIED = "unverified"


class Verdict(Enum):
    VALID = "valid"
    REJECTED = "rejected"
    THROTTLED = "throttled"
    UNAVAILABLE = "unavailable"


class _Odoo(Protocol):
    async def call(self, api_key: str, model: str, method: str, payload: dict[str, Any]) -> Any: ...


class KeyChecker:
    def __init__(
        self,
        odoo: _Odoo,
        *,
        cache_seconds: float,
        attempts_per_minute: int,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._odoo = odoo
        self._ttl = cache_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._attempts = RateLimiter(attempts_per_minute, clock=clock, max_keys=1)
        self._cache: OrderedDict[str, tuple[float, bool]] = OrderedDict()

    def retry_after(self) -> int:
        """Seconds until a new check is possible; only meaningful after THROTTLED."""
        return self._attempts.retry_after(_ALL_UNVERIFIED)

    async def check(self, key: str) -> Verdict:
        digest = key_digest(key)
        cached = self._cache.get(digest)
        if cached is not None and cached[0] > self._clock():
            return Verdict.VALID if cached[1] else Verdict.REJECTED
        if self._attempts.hit(_ALL_UNVERIFIED):
            return Verdict.THROTTLED
        try:
            await self._odoo.call(key, KEY_CHECK_MODEL, KEY_CHECK_METHOD, {})
            valid = True
        except OdooAuthError:
            valid = False
        except OdooAccessError:
            # Odoo authenticated the key; the user just may not call this method.
            valid = True
        except OdooError:
            # Timeouts and server errors say nothing about the key: do not cache them.
            return Verdict.UNAVAILABLE
        self._remember(digest, valid)
        return Verdict.VALID if valid else Verdict.REJECTED

    def _remember(self, digest: str, valid: bool) -> None:
        self._cache[digest] = (self._clock() + self._ttl, valid)
        self._cache.move_to_end(digest)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
