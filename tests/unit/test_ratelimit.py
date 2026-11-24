from odoo_mcp.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_limit_per_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=3, clock=clock)
    assert [limiter.hit("a") for _ in range(3)] == [0, 0, 0]
    assert limiter.hit("a") > 0


def test_keys_are_independent() -> None:
    limiter = RateLimiter(per_minute=1, clock=FakeClock())
    assert limiter.hit("a") == 0
    assert limiter.hit("b") == 0
    assert limiter.hit("a") > 0


def test_window_slides() -> None:
    clock = FakeClock()
    limiter = RateLimiter(per_minute=1, clock=clock)
    assert limiter.hit("a") == 0
    retry_after = limiter.hit("a")
    assert 0 < retry_after <= 60
    clock.now += 61
    assert limiter.hit("a") == 0


def test_disabled_when_zero() -> None:
    limiter = RateLimiter(per_minute=0, clock=FakeClock())
    assert all(limiter.hit("a") == 0 for _ in range(1000))


def test_memory_is_bounded() -> None:
    limiter = RateLimiter(per_minute=5, clock=FakeClock(), max_keys=10)
    for i in range(100):
        limiter.hit(str(i))
    assert limiter.tracked_keys <= 10
