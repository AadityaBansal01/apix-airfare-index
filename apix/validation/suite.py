"""The validation suite: what can be checked without an external benchmark.

CONTEXT, STATED PLAINLY
-----------------------
The problem statement asks for back-tested results "validated against publicly
available DGCA monthly average-fare data". That data does not appear to exist as
a published series. DGCA's Tariff Monitoring Unit monitors fares on 78 routes
monthly by checking airline websites, but that is a regulatory compliance
function: what DGCA publishes monthly is traffic, passengers and load factor,
not fares. Fare figures surface in parliamentary answers as occasional point
estimates, not as a series anyone can join against.

So a back-test that reports correlation against DGCA fares would either be built
on numbers nobody can check, or invented. Neither is acceptable for a system
meant for a statistical office.

What this module does instead is the validation a price statistician would
actually demand, none of which needs an external series:

1.  **Identity checks.** Does the index satisfy the properties its own
    construction guarantees? A base period that is not 100, or a monthly figure
    that disagrees with the mean of its dailies, means the engine contradicts
    its own documentation.

2.  **Robustness.** How far does the headline move when the choices we flagged
    as assumptions are varied? The advance-window weights are the largest stated
    assumption in METHODOLOGY.md; this quantifies what they are worth.

3.  **Benchmark comparison.** Does the Jevons/Young construction differ from a
    naive mean of fares? If it does not, the methodology is decoration and
    should be dropped in favour of the simpler thing.

`external.py` scores against a reference series if one is ever loaded. This
module is what runs today.
"""
from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Mapping, Sequence

from apix.index.aggregate import cell_weights, young_index
from apix.index.build import build_elementary, build_index
from apix.index.elementary import jevons_index

D = Decimal


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str
    value: float | None = None
    tolerance: float | None = None

    def __str__(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"  [{mark}] {self.name}: {self.detail}"


@dataclass
class SuiteResult:
    checks: list[Check] = field(default_factory=list)
    robustness: list[dict] = field(default_factory=list)
    benchmark: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def n_failed(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "n_checks": len(self.checks),
            "n_failed": self.n_failed,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail,
                        "value": c.value, "tolerance": c.tolerance}
                       for c in self.checks],
            "robustness": self.robustness,
            "benchmark": self.benchmark,
        }


# ---------------------------------------------------------------------------
# 1. Identity checks
# ---------------------------------------------------------------------------

def check_base_period(
    reference_values: Sequence[Decimal],
    tolerance: float = 2.0,
) -> Check:
    """The index averaged over the price reference period must sit at 100.

    Base prices are the geometric mean of daily prices across that window, so
    this is guaranteed by construction. Drift means base prices and index prices
    were built from different observations, which would bias every subsequent
    value by a constant nobody would notice.
    """
    if not reference_values:
        return Check("base_period_is_100", False,
                     "no index values inside the price reference period")
    mean = float(sum(reference_values) / len(reference_values))
    ok = abs(mean - 100.0) <= tolerance
    return Check(
        "base_period_is_100", ok,
        f"mean over the price reference period is {mean:.4f} "
        f"(tolerance ±{tolerance})",
        value=mean, tolerance=tolerance)


def check_temporal_consistency(
    daily_by_period: Mapping[dt.date, Sequence[Decimal]],
    aggregates: Mapping[dt.date, Decimal],
    tolerance: float = 0.0001,
) -> Check:
    """A monthly value must equal the mean of its daily values.

    With fixed weights this is an algebraic identity, not an approximation. It
    is checked because it is exactly the kind of thing that silently breaks when
    someone later "optimises" the aggregation to average prices instead of
    indices.
    """
    worst = 0.0
    worst_period = None
    for period, dailies in daily_by_period.items():
        if period not in aggregates or not dailies:
            continue
        expected = float(sum(dailies) / len(dailies))
        actual = float(aggregates[period])
        gap = abs(expected - actual)
        if gap > worst:
            worst, worst_period = gap, period
    ok = worst <= tolerance
    return Check(
        "monthly_equals_mean_of_daily", ok,
        f"largest gap {worst:.6f} index points"
        + (f" in {worst_period}" if worst_period else "")
        + f" (tolerance {tolerance})",
        value=worst, tolerance=tolerance)


def check_contributions_reconstruct(
    headline: Decimal,
    contributions: Sequence[Decimal],
    tolerance: float = 0.001,
) -> Check:
    """Route contributions must sum to the headline.

    This is what makes the published breakdown checkable by hand, which is the
    difference between a number a statistical office can defend and one it
    cannot.
    """
    total = float(sum(contributions))
    gap = abs(total - float(headline))
    ok = gap <= tolerance
    return Check(
        "contributions_sum_to_headline", ok,
        f"contributions sum to {total:.4f} against headline "
        f"{float(headline):.4f}, gap {gap:.6f}",
        value=gap, tolerance=tolerance)


def check_coverage_reported(
    rows: Sequence[Mapping],
) -> Check:
    """Every published value must carry its coverage.

    A figure resting on a partial basket that does not say so is a
    misrepresentation, not a rounding issue.
    """
    missing = [r for r in rows
               if r.get("coverage_ratio") is None
               or not (0 < float(r["coverage_ratio"]) <= 1)]
    ok = not missing
    return Check(
        "every_value_reports_coverage", ok,
        f"{len(rows)} published values, {len(missing)} without a valid "
        f"coverage ratio")


# ---------------------------------------------------------------------------
# 2. Robustness
# ---------------------------------------------------------------------------

def robustness_over_window_weights(
    elementary_by_date: Mapping[dt.date, Mapping[tuple[int, int], Decimal]],
    route_weights: Mapping[int, Decimal],
    scenarios: Mapping[str, Mapping[int, Decimal]],
) -> list[dict]:
    """Rebuild the whole series under alternative advance-window weights.

    METHODOLOGY.md section 3.5 states that equal weighting is an assumption, and
    the largest one in the index, because no public booking-lead-time
    distribution for India exists. This is what that assumption is worth: if the
    headline barely moves across plausible alternatives, the assumption is
    cheap; if it swings, the index depends on a number nobody has measured and
    the caveat needs to be louder than a footnote.
    """
    out: list[dict] = []
    baseline: list[float] | None = None

    for name, window_weights in scenarios.items():
        weights = cell_weights(route_weights, window_weights)
        values: list[float] = []
        for _, elementary in sorted(elementary_by_date.items()):
            usable = {c: v for c, v in elementary.items() if c in weights}
            if not usable:
                continue
            values.append(float(young_index(usable, weights).index_value))
        if not values:
            continue
        if baseline is None:
            baseline = values
        gaps = [abs(a - b) for a, b in zip(values, baseline)]
        out.append({
            "scenario": name,
            "n": len(values),
            "first": round(values[0], 4),
            "last": round(values[-1], 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "mean": round(statistics.fmean(values), 4),
            "max_gap_vs_baseline": round(max(gaps), 4) if gaps else 0.0,
            "mean_gap_vs_baseline": round(statistics.fmean(gaps), 4) if gaps else 0.0,
        })
    return out


def robustness_over_price_concept(
    prices_by_date: Mapping[dt.date, Mapping[tuple[int, int], Mapping[str, list[Decimal]]]],
    base: Mapping[tuple[int, int, str], Decimal],
    weights: Mapping[tuple[int, int], Decimal],
    concepts: Mapping[str, Callable[[list[Decimal]], Decimal]],
) -> list[dict]:
    """Rebuild the series under alternative price concepts.

    APIx uses the lowest available fare, which is what a traveller faces and what
    a price collector records. A median or mean over the day's quotes is a
    defensible alternative measuring a slightly different thing. Reporting the
    spread between them tells a reader how much the choice matters.
    """
    out: list[dict] = []
    for name, pick in concepts.items():
        values: list[float] = []
        for _, cells in sorted(prices_by_date.items()):
            elementary: dict[tuple[int, int], Decimal] = {}
            for cell, by_carrier in cells.items():
                current = {c: pick(v) for c, v in by_carrier.items() if v}
                cell_base = {c: b for (r, w, c), b in base.items() if (r, w) == cell}
                if not current or not cell_base:
                    continue
                try:
                    elementary[cell] = jevons_index(current, cell_base).index_value
                except Exception:
                    continue
            usable = {c: v for c, v in elementary.items() if c in weights}
            if usable:
                values.append(float(young_index(usable, weights).index_value))
        if values:
            out.append({
                "concept": name,
                "n": len(values),
                "mean": round(statistics.fmean(values), 4),
                "min": round(min(values), 4),
                "max": round(max(values), 4),
            })
    return out


# ---------------------------------------------------------------------------
# 3. Benchmark
# ---------------------------------------------------------------------------

def benchmark_against_naive(
    index_values: Sequence[float],
    naive_values: Sequence[float],
) -> dict:
    """Compare the weighted Jevons/Young index against a naive mean of fares.

    The naive series is the unweighted mean fare across the basket, rebased.
    If the two are indistinguishable, the CPI-aligned machinery adds nothing and
    the honest response would be to drop it. A visible divergence is what
    justifies the methodology, and its size is the argument for weighting.
    """
    from apix.validation.metrics import changes, pearson, rebase

    n = min(len(index_values), len(naive_values))
    if n < 2:
        return {"n": n, "note": "not enough observations to compare"}

    a, b = list(index_values[:n]), list(naive_values[:n])
    ra, rb = rebase(a), rebase(b)
    gaps = [abs(x - y) for x, y in zip(ra, rb)] if ra and rb else []

    return {
        "n": n,
        "correlation_on_changes": (
            round(pearson(changes(a), changes(b)), 4)
            if pearson(changes(a), changes(b)) is not None else None),
        "max_divergence_points": round(max(gaps), 4) if gaps else None,
        "mean_divergence_points": round(statistics.fmean(gaps), 4) if gaps else None,
        "index_range": [round(min(a), 4), round(max(a), 4)],
        "naive_range": [round(min(b), 4), round(max(b), 4)],
        "note": (
            "The naive series is an unweighted mean of observed fares, rebased. "
            "Divergence is what the weighting and the Jevons aggregation buy; "
            "if it were near zero the methodology would not be earning its "
            "complexity."),
    }
