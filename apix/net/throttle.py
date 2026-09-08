"""Deliberate, per-origin rate limiting.

The politeness policy, in order of precedence, most restrictive wins:

    effective_delay = max(configured_floor, robots_crawl_delay) + jitter

Two properties matter for defensibility.

**We never speed up.** A site's Crawl-delay can only raise our delay, never lower
it. Yatra publishes `Crawl-delay: 5`; our floor is 6, so we use 6.

**Jitter is additive, not centred.** Randomised timing in scraping is usually
sold as evading detection. Ours exists so that a fleet of workers does not
synchronise into bursts, and it only ever *adds* delay -- the mean interval is
strictly above the floor, never at or below it.
"""
from __future__ import annotations

import asyncio
import random
import time
import urllib.parse
from dataclasses import dataclass, field


@dataclass
class ThrottlePolicy:
    min_delay_seconds: float = 6.0
    jitter_seconds: float = 2.0
    max_concurrent_per_origin: int = 1

    def effective_delay(self, crawl_delay: float | None, rng: random.Random) -> float:
        base = self.min_delay_seconds
        if crawl_delay is not None:
            base = max(base, float(crawl_delay))
        return base + rng.uniform(0.0, self.jitter_seconds)


@dataclass
class OriginThrottle:
    """Serialises and spaces requests to one origin."""
    policy: ThrottlePolicy
    _last: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self, crawl_delay: float | None, rng: random.Random,
                      clock=time.monotonic, sleep=asyncio.sleep) -> float:
        """Block until it is polite to send.

        Returns the INTERVAL since the previous request to this origin, not the
        time spent sleeping. The two differ whenever work between requests has
        already consumed part of the delay, and it is the interval that answers
        the question a regulator would actually ask: how far apart were your
        requests? Recording sleep time would understate the gap and make a
        well-behaved crawler look impatient.

        The first request to an origin returns 0.0, having no predecessor to be
        spaced from. `v_compliance_summary` excludes those zeros from its
        minimum so they cannot be mistaken for a zero-delay request.
        """
        async with self._lock:
            delay = self.policy.effective_delay(crawl_delay, rng)
            now = clock()
            if not self._last:
                self._last = now
                return 0.0
            wait = max(0.0, delay - (now - self._last))
            if wait > 0:
                await sleep(wait)
            after = clock()
            interval = after - self._last
            self._last = after
            return interval


class Throttler:
    """One OriginThrottle per origin, created on demand."""

    def __init__(self, policy: ThrottlePolicy | None = None, seed: int | None = None):
        self.policy = policy or ThrottlePolicy()
        self._per_origin: dict[str, OriginThrottle] = {}
        self._rng = random.Random(seed)

    def _origin(self, url: str) -> str:
        p = urllib.parse.urlsplit(url)
        return f"{p.scheme}://{p.netloc}"

    def for_url(self, url: str, policy: ThrottlePolicy | None = None) -> OriginThrottle:
        """Get the throttle for this origin, tightening it if asked.

        An origin's throttle is created on first contact, which is normally the
        robots.txt fetch under default settings. A source-specific policy arrives
        only afterwards, so without this the per-source delay override would be
        silently discarded for every origin. The policy is only ever tightened,
        never relaxed: a later, laxer policy cannot undo a stricter one.
        """
        o = self._origin(url)
        existing = self._per_origin.get(o)
        if existing is None:
            self._per_origin[o] = OriginThrottle(policy or self.policy)
            return self._per_origin[o]
        if policy is not None and (
            policy.min_delay_seconds > existing.policy.min_delay_seconds
        ):
            existing.policy = policy
        return existing

    async def wait(self, url: str, crawl_delay: float | None = None,
                   policy: ThrottlePolicy | None = None) -> float:
        return await self.for_url(url, policy).acquire(crawl_delay, self._rng)
