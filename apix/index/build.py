"""Index construction: cleaned fares in, published index out.

The pure functions here take plain dictionaries and return plain results, so the
whole methodology is testable without a database. `IndexBuilder` is the thin
shell that loads from and saves to PostgreSQL.

Pipeline for one index date:

    fares -> price per (route, window, carrier)      [done by the cleaner]
          -> elementary index per (route, window)     Jevons, over carriers
          -> headline APIx                            Young, over cells
          -> route breakdown                          contributions

Missing data follows METHODOLOGY.md section 5 exactly, and the policy is
implemented as one function so it cannot drift between callers.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Sequence

from apix.index.aggregate import AggregateResult, NoObservedCells, young_index
from apix.index.elementary import InsufficientData, base_price, jevons_index

log = logging.getLogger(__name__)

ENGINE_VERSION = "index-v1"

#: A cell missing for this many consecutive days or more is dropped from the
#: aggregate rather than carried forward. METHODOLOGY.md section 5.
MAX_CARRY_FORWARD_DAYS = 2

Cell = tuple[int, int]                    # (route_id, window_days)
CarrierPrices = Mapping[str, Decimal]     # carrier -> price


# ---------------------------------------------------------------------------
# Base prices
# ---------------------------------------------------------------------------

def build_base_prices(
    daily_prices: Mapping[tuple[int, int, str], Sequence[Decimal]],
    min_observations: int = 1,
) -> dict[tuple[int, int, str], Decimal]:
    """Geometric mean of daily prices over the price reference period.

    Keyed by (route_id, window_days, carrier). Cells with too few observations
    are omitted rather than estimated from one or two days: a base price is the
    denominator of every future index value for that cell, so a noisy one
    contaminates the entire series rather than a single day.
    """
    out: dict[tuple[int, int, str], Decimal] = {}
    for key, prices in daily_prices.items():
        usable = [p for p in prices if p and p > 0]
        if len(usable) < min_observations:
            log.info("base price skipped for %s: %d observations", key, len(usable))
            continue
        out[key] = base_price(usable)
    return out


# ---------------------------------------------------------------------------
# Elementary indices
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ElementaryOutcome:
    cell: Cell
    index_value: Decimal | None
    carriers_used: tuple[str, ...] = ()
    n_quotes: int = 0
    is_imputed: bool = False
    imputation_note: str | None = None
    dropped: bool = False
    drop_reason: str | None = None


def consecutive_imputed_before(
    history: Mapping[Cell, Mapping[dt.date, bool]],
    cell: Cell,
    index_date: dt.date,
) -> int:
    """How many immediately preceding days for this cell were imputed.

    Walks backwards day by day and stops at the first day that was either
    genuinely observed or absent from history entirely. Used to decide whether
    another carry-forward is still within policy.
    """
    per_cell = history.get(cell)
    if not per_cell:
        return 0
    count = 0
    day = index_date - dt.timedelta(days=1)
    while per_cell.get(day) is True:
        count += 1
        day -= dt.timedelta(days=1)
    return count


def build_elementary(
    index_date: dt.date,
    prices: Mapping[Cell, CarrierPrices],
    base: Mapping[tuple[int, int, str], Decimal],
    basket_cells: Sequence[Cell],
    *,
    last_known: Mapping[Cell, Decimal] | None = None,
    imputed_history: Mapping[Cell, Mapping[dt.date, bool]] | None = None,
    quote_counts: Mapping[Cell, int] | None = None,
    max_carry_forward: int = MAX_CARRY_FORWARD_DAYS,
) -> dict[Cell, ElementaryOutcome]:
    """Jevons index per cell, applying the missing-data policy.

    `basket_cells` is every cell the basket defines, so a cell that produced no
    observations is explicitly accounted for rather than silently absent.
    """
    last_known = last_known or {}
    imputed_history = imputed_history or {}
    quote_counts = quote_counts or {}
    results: dict[Cell, ElementaryOutcome] = {}

    for cell in basket_cells:
        route_id, window = cell
        observed = prices.get(cell) or {}
        cell_base = {
            carrier: b
            for (r, w, carrier), b in base.items()
            if r == route_id and w == window
        }

        if observed and cell_base:
            try:
                res = jevons_index(observed, cell_base)
            except InsufficientData:
                # Carriers seen today have no base price, so no price relative
                # exists. Treated as a missing cell, not as zero change.
                res = None
            if res is not None:
                results[cell] = ElementaryOutcome(
                    cell=cell,
                    index_value=res.index_value,
                    carriers_used=res.carriers_used,
                    n_quotes=quote_counts.get(cell, len(observed)),
                )
                continue

        # Cell produced nothing usable.
        streak = consecutive_imputed_before(imputed_history, cell, index_date)
        prior = last_known.get(cell)
        if prior is not None and streak < max_carry_forward:
            results[cell] = ElementaryOutcome(
                cell=cell,
                index_value=prior,
                is_imputed=True,
                imputation_note=(
                    f"carried forward; consecutive imputed days before this "
                    f"one: {streak}"
                ),
            )
        else:
            results[cell] = ElementaryOutcome(
                cell=cell,
                index_value=None,
                dropped=True,
                drop_reason=(
                    f"no observations and carry-forward limit reached "
                    f"(streak {streak} >= {max_carry_forward})"
                    if prior is not None else "no observations and no prior value"
                ),
            )
    return results


# ---------------------------------------------------------------------------
# Headline
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class IndexBuild:
    index_date: dt.date
    frequency: str
    index_value: Decimal
    n_cells: int
    coverage_ratio: Decimal
    observed_pax_share: Decimal | None
    elementary: dict[Cell, ElementaryOutcome]
    route_values: dict[int, Decimal] = field(default_factory=dict)
    route_contributions: dict[int, Decimal] = field(default_factory=dict)
    route_weights_used: dict[int, Decimal] = field(default_factory=dict)
    imputed_cells: int = 0
    dropped_cells: int = 0

    def summary(self) -> str:
        return (
            f"APIx {self.index_date} {self.frequency}: {self.index_value:.4f} "
            f"({self.n_cells} cells, coverage {float(self.coverage_ratio):.1%}, "
            f"{self.imputed_cells} imputed, {self.dropped_cells} dropped"
            + (f", observing {float(self.observed_pax_share):.1%} of traffic)"
               if self.observed_pax_share is not None else ")")
        )


def observed_passenger_share(
    elementary: Mapping[Cell, ElementaryOutcome],
    route_weights: Mapping[int, Decimal],
    carrier_share: Mapping[str, Decimal],
) -> Decimal | None:
    """Share of passenger traffic flown by carriers we actually observed.

    THE APPROXIMATION, STATED: DGCA publishes carrier traffic nationally and
    city-pair traffic separately, but not carrier-by-city-pair. So a carrier's
    NATIONAL share stands in for its share on each specific route. On trunk
    routes where the excluded carriers are over-represented this flatters the
    number, so treat it as an upper bound on coverage rather than a measurement.

    Returned as a headline field because the alternative is a reader assuming the
    index watches the whole market. It does not.
    """
    if not carrier_share:
        return None
    per_route: dict[int, set[str]] = {}
    for (route_id, _), outcome in elementary.items():
        if outcome.index_value is None or outcome.is_imputed:
            continue
        per_route.setdefault(route_id, set()).update(outcome.carriers_used)
    if not per_route:
        return Decimal("0")

    total_weight = sum(route_weights.get(r, Decimal(0)) for r in per_route)
    if total_weight <= 0:
        return Decimal("0")

    acc = Decimal(0)
    for route_id, carriers in per_route.items():
        w = route_weights.get(route_id, Decimal(0))
        share = sum(carrier_share.get(c, Decimal(0)) for c in carriers)
        acc += w * share
    return acc / total_weight


def build_index(
    index_date: dt.date,
    elementary: Mapping[Cell, ElementaryOutcome],
    cell_weights_map: Mapping[Cell, Decimal],
    route_weights: Mapping[int, Decimal],
    *,
    frequency: str = "daily",
    carrier_share: Mapping[str, Decimal] | None = None,
) -> IndexBuild:
    """Aggregate elementary indices into the headline, with route breakdown."""
    usable = {
        cell: o.index_value
        for cell, o in elementary.items()
        if o.index_value is not None
    }
    if not usable:
        raise NoObservedCells(f"no usable elementary cell on {index_date}")

    agg: AggregateResult = young_index(usable, cell_weights_map)

    # Route-level index: the same Young aggregation restricted to one route,
    # renormalised within it, so route values are directly comparable.
    route_values: dict[int, Decimal] = {}
    route_weight_used: dict[int, Decimal] = {}
    for route_id in {r for r, _ in usable}:
        cells = {c: v for c, v in usable.items() if c[0] == route_id}
        w = {c: cell_weights_map[c] for c in cells if c in cell_weights_map}
        if not w:
            continue
        route_values[route_id] = young_index(cells, w).index_value
        route_weight_used[route_id] = sum(w.values())

    total_used = sum(route_weight_used.values()) or Decimal(1)
    route_contributions = {
        r: route_weight_used[r] * v / total_used
        for r, v in route_values.items()
    }

    return IndexBuild(
        index_date=index_date,
        frequency=frequency,
        index_value=agg.index_value,
        n_cells=agg.n_cells,
        coverage_ratio=agg.coverage_ratio,
        observed_pax_share=observed_passenger_share(
            elementary, route_weights, carrier_share or {}),
        elementary=dict(elementary),
        route_values=route_values,
        route_contributions=route_contributions,
        route_weights_used=route_weight_used,
        imputed_cells=sum(1 for o in elementary.values() if o.is_imputed),
        dropped_cells=sum(1 for o in elementary.values() if o.dropped),
    )


# ---------------------------------------------------------------------------
# Temporal aggregation
# ---------------------------------------------------------------------------

def aggregate_period(
    builds: Sequence[IndexBuild],
    frequency: str,
    period_date: dt.date,
) -> IndexBuild:
    """Weekly or monthly APIx: the arithmetic mean of daily index values.

    With fixed weights this equals aggregating period-mean elementary indices,
    so daily, weekly and monthly figures are mutually consistent by construction.
    METHODOLOGY.md section 3.6; asserted in tests/test_index_math.py.
    """
    if not builds:
        raise ValueError("cannot aggregate an empty period")
    n = Decimal(len(builds))
    value = sum(b.index_value for b in builds) / n
    coverage = sum(b.coverage_ratio for b in builds) / n

    shares = [b.observed_pax_share for b in builds if b.observed_pax_share is not None]
    pax = (sum(shares) / Decimal(len(shares))) if shares else None

    route_ids = {r for b in builds for r in b.route_values}
    route_values = {}
    for r in route_ids:
        vals = [b.route_values[r] for b in builds if r in b.route_values]
        route_values[r] = sum(vals) / Decimal(len(vals))

    return IndexBuild(
        index_date=period_date,
        frequency=frequency,
        index_value=value,
        n_cells=max(b.n_cells for b in builds),
        coverage_ratio=coverage,
        observed_pax_share=pax,
        elementary={},
        route_values=route_values,
        route_contributions={},
        route_weights_used={},
        imputed_cells=sum(b.imputed_cells for b in builds),
        dropped_cells=sum(b.dropped_cells for b in builds),
    )


def week_start(d: dt.date) -> dt.date:
    """Monday of the ISO week containing d."""
    return d - dt.timedelta(days=d.weekday())


def month_start(d: dt.date) -> dt.date:
    return d.replace(day=1)
