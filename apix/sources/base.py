"""The FareSource contract.

Every source — scraped airline, scraped OTA, or licensed GDS API — implements
`FareSource`. The orchestrator knows nothing about any specific site; it walks the
route x window matrix from config and hands each cell to a source.

Two design rules make the contract worth having:

1.  **A source cannot fetch anything itself.** It declares the URL it wants via
    `build_search_url()` and parses bytes it is handed. All network egress goes
    through `FetchContext`, which is where robots compliance, rate limiting and
    audit logging live. A new adapter therefore cannot accidentally bypass the
    ethics layer -- it has no socket to do it with.

2.  **Parsing returns quotes or raises.** It never returns a partially-filled
    quote with guessed components. `components_complete=False` is a first-class,
    honest outcome that the database accepts; a fabricated base/tax split is not.
"""
from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Protocol, Sequence


class SourceKind(str, Enum):
    AIRLINE = "airline"
    OTA = "ota"
    GDS_API = "gds_api"
    OFFICIAL = "official"


class Tier(int, Enum):
    """Compliance tier, set by docs/SOURCE_AUDIT.md.

    Only tier 1 and 2 sources may be enabled. The orchestrator refuses to run a
    source whose tier is 3 or 4 even if config marks it enabled -- a
    belt-and-braces guard against a careless config edit re-enabling a source we
    excluded on ethical grounds.
    """
    PRIMARY = 1          # robots permits, no active bot management
    LICENSED_API = 2     # commercial licence, no scraping
    BOT_MANAGED = 3      # robots would permit, but active bot management -> excluded
    ROBOTS_DENIED = 4    # robots forbids the search path -> excluded


class Outcome(str, Enum):
    OK = "ok"
    BLOCKED_BY_ROBOTS = "blocked_by_robots"
    BLOCKED_BY_INTENT = "blocked_by_intent"
    RATE_LIMITED = "rate_limited"
    HTTP_ERROR = "http_error"
    TIMEOUT = "timeout"
    PARSE_ERROR = "parse_error"
    NO_FLIGHTS = "no_flights"
    CAPTCHA = "captcha"


class SoldOut(Exception):
    """The cell exists but has no purchasable seats.

    Distinct from 'we failed to scrape'. A sold-out cell is real information: it
    is excluded from the index (a sold-out fare is not a price) but recorded as a
    scarcity signal. Conflating it with a scrape failure would bias the index
    downward exactly when the market is tightest.
    """


class NoFlights(Exception):
    """No service operates this route on this date. Not an error, not a price."""


class BlockedByPolicy(Exception):
    """robots.txt, or our own stricter intent rules, forbid this URL."""


class ParseError(Exception):
    """The response did not match any known shape. Never guess -- raise."""


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """One cell of the scraping matrix."""
    origin: str
    destination: str
    departure_date: dt.date
    scrape_date: dt.date
    cabin: str = "ECONOMY"
    adults: int = 1

    @property
    def window_days(self) -> int:
        return (self.departure_date - self.scrape_date).days

    def __post_init__(self) -> None:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.window_days <= 0:
            raise ValueError(
                f"departure {self.departure_date} must be after scrape date "
                f"{self.scrape_date}"
            )


@dataclass(slots=True)
class FareQuote:
    """One observed, purchasable itinerary.

    Money is `Decimal`. Fares reach the index engine and then a statistical
    publication; binary floating point has no place in that chain.
    """
    source_code: str
    origin: str
    destination: str
    carrier: str
    departure_date: dt.date
    scrape_date: dt.date
    total_fare: Decimal

    flight_number: str | None = None
    departure_at: dt.datetime | None = None
    arrival_at: dt.datetime | None = None
    stops: int = 0
    fare_class: str = "ECONOMY"
    fare_brand: str | None = None

    base_fare: Decimal | None = None
    taxes: Decimal | None = None
    udf: Decimal | None = None
    convenience_fee: Decimal | None = None
    other_charges: Decimal | None = None
    currency: str = "INR"

    is_sold_out: bool = False
    quality_flags: list[str] = field(default_factory=list)
    raw_fragment: dict[str, Any] = field(default_factory=dict)

    RECONCILE_TOLERANCE = Decimal("1.00")

    @property
    def window_days(self) -> int:
        return (self.departure_date - self.scrape_date).days

    @property
    def components_complete(self) -> bool:
        """True only when the decomposition genuinely reconciles to the total.

        Mirrors the `fare_components_reconcile` CHECK constraint exactly, so a
        quote that would be rejected by the database is caught in Python first
        with a better error message. The two must stay in sync; the test suite
        asserts that they do.
        """
        if self.base_fare is None or self.taxes is None:
            return False
        parts = sum(
            x for x in (self.base_fare, self.taxes, self.udf,
                        self.convenience_fee, self.other_charges)
            if x is not None
        )
        return abs(parts - self.total_fare) <= self.RECONCILE_TOLERANCE

    def validate(self) -> None:
        if self.total_fare <= 0:
            raise ParseError(f"non-positive total fare {self.total_fare}")
        for name in ("base_fare", "taxes", "udf", "convenience_fee", "other_charges"):
            v = getattr(self, name)
            if v is not None and v < 0:
                raise ParseError(f"negative {name}: {v}")
        if self.window_days <= 0:
            raise ParseError("departure must be after scrape date")
        if self.base_fare is not None and not self.components_complete:
            # Parsed a split that does not add up: keep the total, drop the split,
            # and flag it. Publishing a decomposition we cannot stand behind is
            # worse than publishing none.
            self.quality_flags.append("components_did_not_reconcile")


class FetchResult(Protocol):
    url: str
    status: int
    body: bytes
    text: str
    json: Any
    request_id: int


class FetchContext(Protocol):
    """The only way a source reaches the network.

    Implemented by `apix.net.session.PolicyEnforcedSession`. Every call performs,
    in order: robots check -> intent check -> rate-limit wait -> fetch -> audit
    row. A source can neither skip a step nor reorder them.
    """
    async def get(self, url: str, *, source_code: str, **kw) -> FetchResult: ...
    async def render(self, url: str, *, source_code: str,
                     wait_for: str | None = None, capture_json: str | None = None,
                     **kw) -> FetchResult: ...


class FareSource(abc.ABC):
    """Base class for every fare source."""

    code: str
    display_name: str
    kind: SourceKind
    tier: Tier
    base_url: str
    #: Minimum seconds between requests. Overridden upward, never downward, by a
    #: site's own Crawl-delay.
    min_delay_seconds: float = 6.0
    #: Paths we refuse to touch regardless of what robots.txt literally permits.
    #: See SpiceJet's malformed absolute-URL Disallow lines in the source audit.
    intent_denied_paths: tuple[str, ...] = ()
    parser_version: str = "v1"

    def __init__(self, ctx: FetchContext) -> None:
        if self.tier not in (Tier.PRIMARY, Tier.LICENSED_API):
            raise BlockedByPolicy(
                f"{self.code} is tier {self.tier.value} and must not be run. "
                f"See docs/SOURCE_AUDIT.md."
            )
        self.ctx = ctx

    @abc.abstractmethod
    def build_search_url(self, req: SearchRequest) -> str:
        """The URL for this cell. Pure -- no I/O, so it is trivially testable."""

    @abc.abstractmethod
    async def fetch(self, req: SearchRequest) -> FetchResult:
        """Retrieve the page or API response, via `self.ctx` only."""

    @abc.abstractmethod
    def parse(self, req: SearchRequest, result: FetchResult) -> list[FareQuote]:
        """Turn a response into quotes. Pure, so recorded fixtures fully test it.

        Raises SoldOut, NoFlights or ParseError. Never returns [] to mean
        'something went wrong' -- an empty list means the response genuinely
        contained no itineraries and that fact was recognised.
        """

    async def collect(self, req: SearchRequest) -> list[FareQuote]:
        """fetch + parse + validate. What the orchestrator calls."""
        result = await self.fetch(req)
        quotes = self.parse(req, result)
        for q in quotes:
            q.validate()
        return quotes

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.code} tier={self.tier.value}>"
