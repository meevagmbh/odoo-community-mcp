from typing import Any

import pytest

from odoo_mcp.keycheck import KeyChecker, Verdict
from odoo_mcp.odoo_client import OdooAuthError

pytestmark = pytest.mark.anyio

KEY = "k" * 40


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class StubOdoo:
    def __init__(self, valid: set[str]) -> None:
        self.valid = valid
        self.calls: list[tuple[str, str, str]] = []

    async def call(self, api_key: str, model: str, method: str, payload: dict[str, Any]) -> Any:
        self.calls.append((api_key, model, method))
        if api_key not in self.valid:
            raise OdooAuthError("rejected", 401)
        return {}


async def test_answer_expires_after_the_cache_period() -> None:
    clock = Clock()
    odoo = StubOdoo({KEY})
    checker = KeyChecker(odoo, cache_seconds=60, attempts_per_minute=10, clock=clock)
    assert await checker.check(KEY) is Verdict.VALID
    odoo.valid.clear()  # key revoked in Odoo
    clock.now += 59
    assert await checker.check(KEY) is Verdict.VALID
    clock.now += 2
    assert await checker.check(KEY) is Verdict.REJECTED
    assert len(odoo.calls) == 2


async def test_cache_is_bounded() -> None:
    odoo = StubOdoo(set())
    checker = KeyChecker(
        odoo, cache_seconds=60, attempts_per_minute=100, max_entries=2, clock=Clock()
    )
    for key in ("a" * 40, "b" * 40, "c" * 40, "a" * 40):
        assert await checker.check(key) is Verdict.REJECTED
    assert len(odoo.calls) == 4


async def test_throttle_reports_retry_after() -> None:
    clock = Clock()
    checker = KeyChecker(StubOdoo(set()), cache_seconds=60, attempts_per_minute=1, clock=clock)
    assert checker.retry_after() == 0
    assert await checker.check("a" * 40) is Verdict.REJECTED
    assert await checker.check("b" * 40) is Verdict.THROTTLED
    clock.now += 30
    assert checker.retry_after() == 30
