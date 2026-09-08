"""Elementary (item-level) index: the Jevons formula.

MoSPI uses Jevons at the elementary level in the CPI 2024 series (FAQ answer 20).
We use it identically, over carriers within a (route, advance-window) cell.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

#: Index reference value. A cell equals this in the price reference period.
INDEX_BASE = Decimal("100")


class InsufficientData(Exception):
    """No overlapping carriers between current prices and base prices."""


@dataclass(frozen=True, slots=True)
class ElementaryResult:
    index_value: Decimal
    carriers_used: tuple[str, ...]
    n_carriers: int
    formula: str = "jevons"


def geometric_mean(values: Sequence[Decimal]) -> Decimal:
    """Geometric mean, computed in log space.

    Log space rather than a running product because a month of daily fares
    multiplied together overflows any sane fixed-point range long before the
    root is taken.
    """
    if not values:
        raise InsufficientData("geometric mean of an empty sequence")
    if any(v <= 0 for v in values):
        raise ValueError("geometric mean requires strictly positive values")
    log_sum = sum(math.log(float(v)) for v in values)
    return Decimal(str(math.exp(log_sum / len(values))))


def base_price(daily_prices: Sequence[Decimal]) -> Decimal:
    """Base price for one (route, window, carrier) over the price reference period.

    Geometric, matching the Jevons numerator, so the index is exactly 100 in the
    reference period rather than merely close to it.
    """
    return geometric_mean(daily_prices)


def jevons_index(
    current: Mapping[str, Decimal],
    base: Mapping[str, Decimal],
) -> ElementaryResult:
    """Jevons elementary index over the carriers present in BOTH mappings.

        I = 100 * prod_c (P_c / P0_c) ^ (1/n)

    Carriers observed today but absent from the base period are dropped: a price
    relative needs both endpoints. Dropping them is the standard matched-model
    treatment and is what keeps the index measuring price change rather than
    changes in which carriers we happened to see.
    """
    carriers = tuple(sorted(set(current) & set(base)))
    if not carriers:
        raise InsufficientData(
            f"no carrier overlap: current={sorted(current)} base={sorted(base)}"
        )

    log_sum = 0.0
    for c in carriers:
        p, p0 = current[c], base[c]
        if p <= 0 or p0 <= 0:
            raise ValueError(f"non-positive price for carrier {c}: {p}, {p0}")
        log_sum += math.log(float(p) / float(p0))

    value = INDEX_BASE * Decimal(str(math.exp(log_sum / len(carriers))))
    return ElementaryResult(
        index_value=value,
        carriers_used=carriers,
        n_carriers=len(carriers),
    )
