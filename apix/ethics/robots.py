"""robots.txt compliance, including intent beyond the letter of the standard.

Three things distinguish this from a bare `urllib.robotparser` call.

1.  **Snapshots are persisted.** Every robots.txt we read is stored with its
    SHA-256 and timestamp. Months later we can prove what a site permitted on the
    day we crawled it, even if the site has since changed.

2.  **Intent rules.** Some robots.txt files are malformed in ways that make a
    literal parser more permissive than the operator meant. SpiceJet writes
    `Disallow: https://www.spicejet.com/api/v1` -- an absolute URL where RFC 9309
    requires a path, so a conforming parser ignores the line entirely. We honour
    the evident intent and refuse those paths anyway.

3.  **Fail closed.** If robots.txt cannot be fetched, we do not crawl. The
    common default in scraping libraries is the opposite, and it is wrong for a
    system whose defensibility is the point.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import logging
import urllib.parse

from apix.ethics.matcher import RobotsRules
from typing import Protocol

log = logging.getLogger(__name__)

#: How long a cached robots.txt stays valid before we refetch.
ROBOTS_TTL = dt.timedelta(hours=12)


@dataclasses.dataclass(frozen=True, slots=True)
class RobotsDecision:
    allowed: bool
    rule: str
    crawl_delay: float | None
    snapshot_id: int | None
    intent_denied: bool = False

    @property
    def outcome_if_denied(self) -> str:
        return "blocked_by_intent" if self.intent_denied else "blocked_by_robots"


@dataclasses.dataclass(slots=True)
class RobotsSnapshot:
    robots_url: str
    body: str
    sha256: bytes
    fetched_at: dt.datetime
    http_status: int | None
    crawl_delay: float | None
    snapshot_id: int | None = None


class SnapshotStore(Protocol):
    def save(self, source_code: str, snap: RobotsSnapshot) -> int: ...


class RobotsGate:
    """Decides whether a URL may be requested, and records why.

    Usage is deliberately awkward to bypass: the network session calls
    `check()` and refuses to proceed on a denial. A source adapter never touches
    this class directly.
    """

    def __init__(
        self,
        fetcher,                      # async callable(url) -> (status, text)
        store: SnapshotStore | None = None,
        user_agent: str = "APIx-Research-Bot",
        ttl: dt.timedelta = ROBOTS_TTL,
        clock=dt.datetime.now,
    ) -> None:
        self._fetch = fetcher
        self._store = store
        self._ua = user_agent
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[str, tuple[RobotsSnapshot, RobotsRules]] = {}

    # -- snapshot handling ------------------------------------------------

    async def _snapshot(self, origin: str, source_code: str):
        cached = self._cache.get(origin)
        now = self._clock(dt.timezone.utc)
        if cached and (now - cached[0].fetched_at) < self._ttl:
            return cached

        robots_url = urllib.parse.urljoin(origin, "/robots.txt")
        status, text = await self._fetch(robots_url)

        # apix.ethics.matcher, not urllib.robotparser: the standard library
        # ignores wildcards and returns the first matching rule rather than the
        # most specific one, and both mistakes permit fetches the site forbids.
        # See the module docstring in matcher.py for the case that found this.
        if status == 200:
            parser = RobotsRules.parse(text)
        elif status in (401, 403):
            # RFC 9309: an authorisation failure means the whole site is disallowed.
            parser = RobotsRules([], disallow_all=True)
        elif status is None or status >= 500:
            # Fail closed. A 5xx is not permission.
            parser = RobotsRules([], disallow_all=True)
            log.warning("robots.txt for %s returned %s; failing closed", origin, status)
        else:
            # 404 and other 4xx: RFC 9309 says treat as fully allowed.
            parser = RobotsRules([], allow_all=True)

        delay = parser.crawl_delay(self._ua)

        snap = RobotsSnapshot(
            robots_url=robots_url,
            body=text or "",
            sha256=hashlib.sha256((text or "").encode()).digest(),
            fetched_at=now,
            http_status=status,
            crawl_delay=delay,
        )
        if self._store is not None:
            snap.snapshot_id = self._store.save(source_code, snap)
        self._cache[origin] = (snap, parser)
        return snap, parser

    # -- the decision -----------------------------------------------------

    async def check(
        self,
        url: str,
        *,
        source_code: str,
        intent_denied_paths: tuple[str, ...] = (),
    ) -> RobotsDecision:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        path = parts.path or "/"

        # Intent rules run FIRST and are absolute. A site cannot opt us back in
        # to a path we have decided not to touch.
        for denied in intent_denied_paths:
            if path.startswith(denied):
                log.info("intent-denied %s (matches %r)", url, denied)
                return RobotsDecision(
                    allowed=False,
                    rule=f"intent_denied:{denied}",
                    crawl_delay=None,
                    snapshot_id=None,
                    intent_denied=True,
                )

        snap, parser = await self._snapshot(origin, source_code)
        allowed = parser.can_fetch(self._ua, url)
        # The rule that decided it, not just the verdict: an auditor needs to see
        # which line of which robots.txt permitted or refused each request.
        rule = parser.explain(self._ua, url)
        if not allowed:
            log.info("robots-denied %s for UA %s", url, self._ua)
        return RobotsDecision(
            allowed=allowed,
            rule=rule,
            crawl_delay=snap.crawl_delay,
            snapshot_id=snap.snapshot_id,
        )
