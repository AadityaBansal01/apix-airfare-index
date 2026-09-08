"""Synthetic fare generation for the demo and for engine verification.

WHY THIS EXISTS, AND WHAT IT IS NOT
-----------------------------------
Two honest uses:

1.  **Demo resilience.** A live scrape can fail on presentation day because a
    site is down, the venue network blocks it, or a carrier changes its markup.
    The dashboard must still render.
2.  **Engine verification.** Thirty days of live collection cannot be
    manufactured on demand, but the index engine still has to be exercised over a
    realistic span before anyone trusts it.

It is NOT a substitute for measurement and is never presented as one. Seeded rows
carry `source_code='seed'`, the API echoes a seeded flag in its metadata, and the
dashboard shows a persistent badge. A seeded number must never be readable as an
observation.

THE GENERATING PROCESS
----------------------
Deliberately built from documented aviation-pricing structure rather than noise,
so the analytics built on top have something real to find:

*   **Lead-time curve.** Fares rise as departure nears, steeply inside a week.
    Modelled as a decaying exponential in the advance-purchase window, which is
    the shape the elasticity module is meant to recover.
*   **Day-of-week seasonality.** Friday and Sunday departures price above
    midweek.
*   **Carrier offset.** A persistent level difference between carriers.
*   **A surge event.** A multi-day festival-shaped spike, so the anomaly detector
    has something to detect and the back-test has a real movement to track.
*   **Occasional gaps and sold-out cells**, so the missing-data policy is
    exercised rather than assumed.
"""
from __future__ import annotations

import datetime as dt
import math
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator, Sequence

from apix.sources.base import FareQuote

SEED_SOURCE = "seed"

#: Reference fare level per route at a 45-day advance window, in rupees. Set
#: from published typical trunk-route fares; the index is scale-invariant, so
#: these fix the level of the synthetic data, not the level of the index.
ROUTE_FLOOR = {
    ("BLR", "DEL"): 4200,
    ("BOM", "DEL"): 3900,
    ("CCU", "DEL"): 4400,
    ("BLR", "BOM"): 3300,
    ("BLR", "HYD"): 2600,
    ("DEL", "MAA"): 4800,
    ("DEL", "HYD"): 4300,
    ("BLR", "CCU"): 4600,
}

#: Persistent level differences between carriers.
CARRIER_OFFSET = {"QP": 1.04, "SG": 0.96}

#: Per-route lead-time shape, as (amplitude, decay constant) in
#: P(w) = 1 + a * exp(-w / tau).
#:
#: These differ by route on purpose. A single shared curve would make the
#: last-minute premium identical on every sector, which is both unrealistic and
#: quietly misleading: the lead-time view would show six lines that are the same
#: line, and a reader could reasonably conclude the model had found that result
#: in the data rather than had it assumed.
#:
#: The pattern encoded is the documented one. Business-heavy trunk sectors price
#: late bookings hardest and hold a steeper curve; leisure and short-haul
#: sectors are flatter because the traffic is more price-elastic and books
#: earlier.
ROUTE_LEADTIME = {
    ("BOM", "DEL"): (1.42, 10.0),   # densest business corridor, steepest
    ("BLR", "DEL"): (1.30, 11.0),
    ("DEL", "MAA"): (1.18, 12.0),
    ("CCU", "DEL"): (1.05, 13.5),
    ("BLR", "BOM"): (0.95, 14.0),
    ("DEL", "HYD"): (1.22, 11.5),   # business corridor, close to BLR-DEL
    ("BLR", "CCU"): (1.00, 13.0),   # long, mixed traffic
    ("BLR", "HYD"): (0.72, 16.0),   # short-haul leisure, flattest
}
DEFAULT_LEADTIME = (1.15, 12.0)

#: Flights per carrier per route per day, so a comparison group is large enough
#: for the outlier detector to assess.
FLIGHTS_PER_DAY = 6


@dataclass(frozen=True, slots=True)
class SurgeEvent:
    """A festival- or fuel-shaped price spike."""
    start: dt.date
    days: int
    peak_multiplier: float
    label: str

    def multiplier(self, day: dt.date) -> float:
        offset = (day - self.start).days
        if not (0 <= offset < self.days):
            return 1.0
        # Rises then falls: a raised cosine over the event window.
        phase = offset / max(1, self.days - 1)
        shape = math.sin(math.pi * phase)
        return 1.0 + (self.peak_multiplier - 1.0) * shape


def leadtime_factor(window_days: int, route: tuple[str, str] | None = None) -> float:
    """Fare multiplier as a function of advance-purchase window.

    P(w) = 1 + a * exp(-w / tau)

    Amplitude and decay come from ROUTE_LEADTIME so each sector has its own
    curve; a T+1 fare lands between about 1.7x and 2.3x the far-advance level
    depending on the route, and T+45 is nearly flat everywhere. That is the
    shape airlines describe publicly, and it is what the lead-time view is meant
    to recover.
    """
    if route is None:
        a, tau = DEFAULT_LEADTIME
    else:
        a, tau = ROUTE_LEADTIME.get(
            route, ROUTE_LEADTIME.get((route[1], route[0]), DEFAULT_LEADTIME))
    return 1.0 + a * math.exp(-window_days / tau)


def dow_factor(departure: dt.date) -> float:
    """Friday and Sunday departures price above midweek."""
    return {0: 1.00, 1: 0.97, 2: 0.96, 3: 0.99,
            4: 1.08, 5: 1.02, 6: 1.09}[departure.weekday()]


def generate_day(
    scrape_date: dt.date,
    routes: Sequence[tuple[str, str]],
    windows: Sequence[int],
    carriers: Sequence[str] = ("QP", "SG"),
    *,
    rng: random.Random,
    surge: SurgeEvent | None = None,
    gap_probability: float = 0.02,
    sold_out_probability: float = 0.04,
) -> list[FareQuote]:
    """One day of quotes across the whole route by window matrix."""
    quotes: list[FareQuote] = []
    for origin, destination in routes:
        floor = ROUTE_FLOOR.get((origin, destination))
        if floor is None:
            floor = ROUTE_FLOOR.get((destination, origin), 4000)
        route = (origin, destination)
        for window in windows:
            departure = scrape_date + dt.timedelta(days=window)
            for carrier in carriers:
                # An occasional whole cell simply does not return, which is what
                # exercises the carry-forward policy.
                if rng.random() < gap_probability:
                    continue
                level = (
                    floor
                    * leadtime_factor(window, route)
                    * dow_factor(departure)
                    * CARRIER_OFFSET.get(carrier, 1.0)
                    * (surge.multiplier(scrape_date) if surge else 1.0)
                )
                for i in range(FLIGHTS_PER_DAY):
                    # Within-day dispersion: early and late departures differ.
                    slot = 0.88 + 0.30 * (i / max(1, FLIGHTS_PER_DAY - 1))
                    total = int(level * slot * rng.uniform(0.96, 1.05))
                    base = int(total * 0.82)
                    taxes = int(total * 0.10)
                    udf = int(total * 0.05)
                    quotes.append(FareQuote(
                        source_code=SEED_SOURCE,
                        origin=origin, destination=destination, carrier=carrier,
                        departure_date=departure, scrape_date=scrape_date,
                        total_fare=Decimal(total),
                        flight_number=f"{carrier}{1000 + i}",
                        departure_at=dt.datetime.combine(
                            departure, dt.time(6 + i * 2, 15)),
                        stops=0, fare_class="ECONOMY", fare_brand="SAVER",
                        base_fare=Decimal(base), taxes=Decimal(taxes),
                        udf=Decimal(udf),
                        convenience_fee=Decimal(total - base - taxes - udf),
                        is_sold_out=(rng.random() < sold_out_probability),
                    ))
    return quotes


def generate_range(
    start: dt.date,
    end: dt.date,
    routes: Sequence[tuple[str, str]],
    windows: Sequence[int],
    *,
    seed: int = 20260904,
    surge: SurgeEvent | None = None,
) -> Iterator[tuple[dt.date, list[FareQuote]]]:
    """Deterministic day-by-day generation over an inclusive date range."""
    rng = random.Random(seed)
    day = start
    while day <= end:
        yield day, generate_day(day, routes, windows, rng=rng, surge=surge)
        day += dt.timedelta(days=1)
