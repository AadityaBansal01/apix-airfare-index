"""Upper-level aggregation: the Young / modified Laspeyres index.

MoSPI uses Young / modified Laspeyres for higher-level aggregation in the CPI 2024
series (FAQ answer 21). The name matters: our weights come from a weight reference
period (DGCA traffic) that differs from the price reference period, which is
exactly what makes the estimator Young rather than Laspeyres.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

Cell = tuple[int, int]   # (route_id, window_days)


class NoObservedCells(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AggregateResult:
    index_value: Decimal
    n_cells: int
    coverage_ratio: Decimal
    contributions: dict[Cell, Decimal]
    formula: str = "young_modified_laspeyres"


def cell_weights(
    route_weights: Mapping[int, Decimal],
    window_weights: Mapping[int, Decimal],
) -> dict[Cell, Decimal]:
    """omega_{r,w} = omega^route_r * omega^win_w, separable by construction.

    Separability is an assumption: it says the mix of advance-purchase windows is
    the same on every route. Stated in METHODOLOGY.md sec.3.5 rather than hidden,
    and replaceable with a full joint weight matrix if data ever supports one.
    """
    return {
        (r, w): rw * ww
        for r, rw in route_weights.items()
        for w, ww in window_weights.items()
    }


def young_index(
    elementary: Mapping[Cell, Decimal],
    weights: Mapping[Cell, Decimal],
) -> AggregateResult:
    """Weighted arithmetic mean of elementary indices over observed cells.

        APIx = sum_{r,w} omega_{r,w} I_{r,w} / sum_{r,w} omega_{r,w}

    The denominator is written out because it is NOT 1 when cells are missing.
    Renormalising over observed weight keeps the index on a comparable scale, and
    `coverage_ratio` reports how much of the basket that actually was, so a reader
    can see when a day's figure rests on a partial basket.
    """
    observed = [c for c in elementary if c in weights]
    if not observed:
        raise NoObservedCells("no elementary cell has a matching weight")

    total_weight = sum(weights[c] for c in observed)
    if total_weight <= 0:
        raise NoObservedCells("observed cells carry zero total weight")

    numerator = sum(weights[c] * elementary[c] for c in observed)
    value = numerator / total_weight

    full_weight = sum(weights.values())
    coverage = (total_weight / full_weight) if full_weight > 0 else Decimal(0)

    contributions = {c: weights[c] * elementary[c] / total_weight for c in observed}
    return AggregateResult(
        index_value=value,
        n_cells=len(observed),
        coverage_ratio=coverage,
        contributions=contributions,
    )


def temporal_aggregate(daily: Sequence[Decimal]) -> Decimal:
    """Weekly / monthly APIx: the arithmetic mean of daily index values.

    With weights fixed within the period this is identically equal to aggregating
    the period-mean elementary indices, so daily, weekly and monthly figures are
    mutually consistent. See METHODOLOGY.md sec.3.6; asserted in
    tests/test_index_math.py::test_temporal_aggregation_is_order_independent.
    """
    if not daily:
        raise ValueError("cannot aggregate an empty period")
    return sum(daily) / Decimal(len(daily))
