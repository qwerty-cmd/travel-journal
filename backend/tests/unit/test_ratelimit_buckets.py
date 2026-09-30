"""The token-bucket arithmetic in ``app/core/ratelimit.py``, with an injected clock."""

from __future__ import annotations

import pytest

from app.core.errors import ApiError
from app.core.ratelimit import Bucket, KeyKind, RateLimitRegistry

TEN_PER_100S = Bucket("test-ten", 10, 100.0, KeyKind.IP)
TWO_PER_10S = Bucket("test-two", 2, 10.0, KeyKind.GLOBAL)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def registry(clock: Clock) -> RateLimitRegistry:
    return RateLimitRegistry(clock=clock)


def test_capacity_then_refusal_with_the_wait_for_one_token(registry: RateLimitRegistry) -> None:
    for _ in range(10):
        assert registry.acquire([(TEN_PER_100S, "a")]) is None
    assert registry.acquire([(TEN_PER_100S, "a")]) == pytest.approx(10.0)


def test_refill_is_continuous_and_capped(registry: RateLimitRegistry, clock: Clock) -> None:
    for _ in range(10):
        registry.acquire([(TEN_PER_100S, "a")])
    clock.now += 4.0
    assert registry.acquire([(TEN_PER_100S, "a")]) == pytest.approx(6.0)
    clock.now += 6.0
    assert registry.acquire([(TEN_PER_100S, "a")]) is None
    clock.now += 10_000.0  # far past full: still only 10
    for _ in range(10):
        assert registry.acquire([(TEN_PER_100S, "a")]) is None
    assert registry.acquire([(TEN_PER_100S, "a")]) is not None


def test_keys_and_buckets_are_independent(registry: RateLimitRegistry) -> None:
    for _ in range(10):
        registry.acquire([(TEN_PER_100S, "a")])
    assert registry.acquire([(TEN_PER_100S, "b")]) is None
    assert registry.acquire([(TWO_PER_10S, "a")]) is None


def test_a_refused_multi_bucket_spend_spends_nothing(registry: RateLimitRegistry) -> None:
    registry.acquire([(TWO_PER_10S, "*")])
    registry.acquire([(TWO_PER_10S, "*")])
    for _ in range(3):
        assert registry.acquire([(TEN_PER_100S, "a"), (TWO_PER_10S, "*")]) is not None
    # "a" lost nothing to the refusals: all ten are still there.
    for _ in range(10):
        assert registry.acquire([(TEN_PER_100S, "a")]) is None


def test_the_wait_is_the_longest_of_the_empty_buckets(registry: RateLimitRegistry) -> None:
    for _ in range(10):
        registry.acquire([(TEN_PER_100S, "a")])
    for _ in range(2):
        registry.acquire([(TWO_PER_10S, "*")])
    assert registry.acquire([(TEN_PER_100S, "a"), (TWO_PER_10S, "*")]) == pytest.approx(10.0)


def test_reset_refills_everything(registry: RateLimitRegistry) -> None:
    for _ in range(10):
        registry.acquire([(TEN_PER_100S, "a")])
    registry.reset()
    assert len(registry) == 0
    assert registry.acquire([(TEN_PER_100S, "a")]) is None


def test_eviction_drops_full_buckets_first_then_least_recent(clock: Clock) -> None:
    registry = RateLimitRegistry(clock=clock, max_keys=4, low_water=2)
    for key in "abc":
        registry.acquire([(TEN_PER_100S, key)])
    clock.now += 10.0  # a, b and c have refilled completely
    for _ in range(10):
        registry.acquire([(TEN_PER_100S, "d")])  # d is empty
    registry.acquire([(TEN_PER_100S, "e")])  # at the cap: the full a, b, c go, losslessly
    assert len(registry) == 2
    assert registry.acquire([(TEN_PER_100S, "d")]) is not None, "d's empty bucket was kept"

    for key in "fg":
        for _ in range(10):
            registry.acquire([(TEN_PER_100S, key)])
    # d, e, f, g: at the cap with nothing full, so the least recent go down to 2.
    registry.acquire([(TEN_PER_100S, "h")])
    assert len(registry) == 3


@pytest.mark.parametrize(("wait", "header"), [(0.001, "1"), (1.0, "1"), (1.2, "2"), (59.99, "60")])
def test_rate_limited_rounds_a_float_wait_up(wait: float, header: str) -> None:
    assert ApiError.rate_limited("Wait.", wait).headers == {"Retry-After": header}


@pytest.mark.parametrize("wait", [0.0, -0.5, float("nan"), float("inf")])
def test_rate_limited_refuses_a_float_that_is_not_a_positive_wait(wait: float) -> None:
    with pytest.raises(ValueError):
        ApiError.rate_limited("Wait.", wait)


@pytest.mark.parametrize("wait", [True, False, "5", None])
def test_rate_limited_refuses_bool_and_non_numbers(wait: object) -> None:
    with pytest.raises(TypeError):
        ApiError.rate_limited("Wait.", wait)  # type: ignore[arg-type]
