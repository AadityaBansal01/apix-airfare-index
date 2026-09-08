"""Rate-limiting tests, including the two bugs found during the live smoke test.

Both were invisible to the fake-throttler tests and only showed up when a real
run wrote real numbers into the compliance view. They are pinned here.
"""
from __future__ import annotations

import random

import pytest

from apix.net.throttle import OriginThrottle, ThrottlePolicy, Throttler

pytestmark = pytest.mark.asyncio


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


# --------------------------------------------------------------------------
# Policy arithmetic
# --------------------------------------------------------------------------

async def test_crawl_delay_can_only_raise_the_floor():
    pol = ThrottlePolicy(min_delay_seconds=6.0, jitter_seconds=0.0)
    rng = random.Random(0)
    assert pol.effective_delay(None, rng) == 6.0
    assert pol.effective_delay(2.0, rng) == 6.0      # site asks less; we keep ours
    assert pol.effective_delay(30.0, rng) == 30.0    # site asks more; we obey


async def test_jitter_is_additive_only():
    pol = ThrottlePolicy(min_delay_seconds=6.0, jitter_seconds=2.0)
    rng = random.Random(1)
    samples = [pol.effective_delay(None, rng) for _ in range(500)]
    assert min(samples) >= 6.0
    assert max(samples) <= 8.0
    assert sum(samples) / len(samples) > 6.0   # mean strictly above the floor


# --------------------------------------------------------------------------
# Bug 1: per-source policy was dropped after the origin throttle existed
# --------------------------------------------------------------------------

async def test_source_policy_tightens_an_existing_origin_throttle():
    """Regression: the robots.txt fetch creates the origin throttle with default
    settings, so an 8s per-source floor arriving afterwards used to be ignored."""
    t = Throttler(ThrottlePolicy(min_delay_seconds=6.0, jitter_seconds=0.0))
    clock = FakeClock()

    # First contact with no policy, as the robots.txt fetch does.
    await t.for_url("https://x.com/robots.txt").acquire(
        None, random.Random(0), clock, clock.sleep)

    strict = ThrottlePolicy(min_delay_seconds=8.0, jitter_seconds=0.0)
    throttle = t.for_url("https://x.com/page", strict)
    assert throttle.policy.min_delay_seconds == 8.0

    interval = await throttle.acquire(None, random.Random(0), clock, clock.sleep)
    assert interval == pytest.approx(8.0)


async def test_policy_is_never_relaxed():
    t = Throttler(ThrottlePolicy(min_delay_seconds=6.0, jitter_seconds=0.0))
    t.for_url("https://x.com/a", ThrottlePolicy(min_delay_seconds=10.0, jitter_seconds=0.0))
    t.for_url("https://x.com/b", ThrottlePolicy(min_delay_seconds=2.0, jitter_seconds=0.0))
    assert t.for_url("https://x.com/c").policy.min_delay_seconds == 10.0


# --------------------------------------------------------------------------
# Bug 2: the audit recorded sleep time, not the gap between requests
# --------------------------------------------------------------------------

async def test_records_interval_between_requests_not_sleep_time():
    """Regression: with work happening between requests, sleep time understates
    the true gap, so the compliance view reported delays below our own floor."""
    pol = ThrottlePolicy(min_delay_seconds=6.0, jitter_seconds=0.0)
    throttle = OriginThrottle(pol)
    clock = FakeClock()
    rng = random.Random(0)

    first = await throttle.acquire(None, rng, clock, clock.sleep)
    assert first == 0.0                       # nothing to be spaced from

    clock.advance(4.0)                        # 4s of parsing happens
    second = await throttle.acquire(None, rng, clock, clock.sleep)
    # It only slept 2s, but the requests are 6s apart. 6 is the honest number.
    assert second == pytest.approx(6.0)


async def test_interval_never_falls_below_the_floor():
    pol = ThrottlePolicy(min_delay_seconds=6.0, jitter_seconds=0.0)
    throttle = OriginThrottle(pol)
    clock = FakeClock()
    rng = random.Random(0)
    await throttle.acquire(None, rng, clock, clock.sleep)
    for gap in (0.0, 1.0, 3.0, 5.9, 6.0, 20.0):
        clock.advance(gap)
        interval = await throttle.acquire(None, rng, clock, clock.sleep)
        assert interval >= 6.0 - 1e-9, f"gap {gap} produced interval {interval}"


async def test_slow_work_between_requests_needs_no_extra_sleep():
    pol = ThrottlePolicy(min_delay_seconds=6.0, jitter_seconds=0.0)
    throttle = OriginThrottle(pol)
    clock = FakeClock()
    rng = random.Random(0)
    await throttle.acquire(None, rng, clock, clock.sleep)
    clock.advance(30.0)
    before = clock.t
    interval = await throttle.acquire(None, rng, clock, clock.sleep)
    assert clock.t == before          # slept zero
    assert interval == pytest.approx(30.0)


# --------------------------------------------------------------------------
# Isolation between origins
# --------------------------------------------------------------------------

async def test_origins_are_throttled_independently():
    t = Throttler(ThrottlePolicy(min_delay_seconds=6.0, jitter_seconds=0.0))
    a = t.for_url("https://a.com/x")
    b = t.for_url("https://b.com/x")
    assert a is not b
    assert t.for_url("https://a.com/y") is a
