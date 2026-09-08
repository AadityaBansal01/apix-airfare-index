"""Comparison metrics for validating one price series against another.

THE TRAP THIS MODULE IS BUILT AROUND
------------------------------------
The obvious way to validate a new index against an official one is to correlate
their levels. It is also wrong, and it is wrong in the direction that flatters
the result.

Two price indices both trend upward over time. Correlating their levels measures
mostly that shared trend, so it returns a large coefficient whether or not the
two series have anything to do with each other. A web-scraped airfare index and
a series of monthly rainfall totals would correlate at 0.9 if both happened to
drift up over the sample. Reporting that as validation would be meaningless, and
it is exactly the number a hurried analysis produces.

The correct comparison is between **period-on-period growth rates**, which is
what `align_and_transform` produces by default. Levels are offered too, clearly
labelled with a warning, because a reader will ask for them.

Cavallo and Rigobon's Billion Prices Project work, and the ONS research on
web-scraped price indices, both compare inflation rates rather than index levels
for this reason.

WHAT A SHORT SAMPLE CAN ESTABLISH
---------------------------------
Very little, and this module says so rather than leaving the reader to work it
out. Thirty days against a monthly reference yields one or two overlapping
observations. A correlation on two points is exactly 1 or exactly -1 by
construction and carries no information at all. `MetricResult.interpretation`
states the power limitation alongside every coefficient, and correlations are
refused outright below `MIN_N_FOR_CORRELATION`.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Literal, Sequence

import numpy as np

#: Below this many paired observations a correlation coefficient is not
#: reported. With n=2 the coefficient is +/-1 by construction; with n=3 it is
#: dominated by noise. Refusing to print a number is better than printing one
#: that will be quoted without its caveat.
MIN_N_FOR_CORRELATION = 8

#: Below this, even a direction-agreement statistic is too thin to interpret.
MIN_N_FOR_ANY_METRIC = 3

Transform = Literal["log_diff", "pct_change", "level", "rebased_level"]


@dataclass(slots=True)
class MetricResult:
    name: str
    value: float | None
    n: int
    interpretation: str
    reliable: bool
    detail: str = ""

    def __str__(self) -> str:
        v = "n/a" if self.value is None else f"{self.value:.4f}"
        flag = "" if self.reliable else "  [UNDERPOWERED]"
        return f"{self.name:<28} {v:>10}  (n={self.n}){flag}"


@dataclass(slots=True)
class Comparison:
    """A paired, aligned comparison of two series."""
    dates: list[dt.date]
    a: np.ndarray
    b: np.ndarray
    a_name: str
    b_name: str
    transform: Transform
    metrics: list[MetricResult] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.dates)

    def get(self, name: str) -> MetricResult | None:
        return next((m for m in self.metrics if m.name == name), None)

    def summary(self) -> str:
        lines = [
            f"{self.a_name} vs {self.b_name}",
            f"transform: {self.transform}, n = {self.n}",
            "-" * 58,
        ]
        lines += [str(m) for m in self.metrics]
        if not any(m.reliable for m in self.metrics):
            lines.append("")
            lines.append("NO METRIC HERE IS RELIABLE AT THIS SAMPLE SIZE.")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Alignment and transformation
# ---------------------------------------------------------------------------

def monthly_mean(
    dates: Sequence[dt.date], values: Sequence[float]
) -> dict[dt.date, float]:
    """Aggregate a daily series to monthly means, keyed by first of month.

    Temporal aggregation of the high-frequency series, rather than interpolation
    of the low-frequency one. Interpolating a monthly series to daily invents
    observations that were never made and would inflate the apparent sample size
    from two points to sixty.
    """
    buckets: dict[dt.date, list[float]] = {}
    for d, v in zip(dates, values):
        buckets.setdefault(d.replace(day=1), []).append(float(v))
    return {k: float(np.mean(vs)) for k, vs in sorted(buckets.items())}


def align(
    a: dict[dt.date, float], b: dict[dt.date, float]
) -> tuple[list[dt.date], np.ndarray, np.ndarray]:
    """Inner join on date. Only periods present in both survive."""
    common = sorted(set(a) & set(b))
    return (
        common,
        np.array([a[d] for d in common], dtype=float),
        np.array([b[d] for d in common], dtype=float),
    )


def transform_series(values: np.ndarray, how: Transform) -> np.ndarray:
    """Apply the comparison transformation.

    `log_diff` is the default elsewhere in this module: log differences are
    symmetric in direction, additive across periods, and approximately equal to
    percentage change for small movements, which is what makes them the standard
    choice for comparing inflation rates.
    """
    if how == "level":
        return values
    if how == "rebased_level":
        # Rebase to the first observation so two indices with different base
        # periods start from the same point. Removes the level difference but
        # NOT the shared trend, so this still overstates agreement.
        if values[0] == 0:
            raise ValueError("cannot rebase on a zero first observation")
        return 100.0 * values / values[0]
    if how == "pct_change":
        return np.diff(values) / values[:-1]
    if how == "log_diff":
        if np.any(values <= 0):
            raise ValueError("log differences need strictly positive values")
        return np.diff(np.log(values))
    raise ValueError(f"unknown transform {how!r}")


def align_and_transform(
    a: dict[dt.date, float],
    b: dict[dt.date, float],
    *,
    transform: Transform = "log_diff",
    a_name: str = "APIx",
    b_name: str = "reference",
) -> Comparison:
    dates, av, bv = align(a, b)
    if len(dates) < 2 and transform in ("log_diff", "pct_change"):
        return Comparison(dates, av, bv, a_name, b_name, transform)
    ta, tb = transform_series(av, transform), transform_series(bv, transform)
    # Differencing consumes the first observation.
    out_dates = dates[1:] if transform in ("log_diff", "pct_change") else dates
    return Comparison(out_dates, ta, tb, a_name, b_name, transform)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _underpowered(name: str, n: int, floor: int) -> MetricResult:
    return MetricResult(
        name=name, value=None, n=n, reliable=False,
        interpretation=(
            f"not computed: {n} paired observations is below the {floor} this "
            f"metric needs to mean anything"
        ),
    )


def pearson(c: Comparison) -> MetricResult:
    n = c.n
    if n < MIN_N_FOR_CORRELATION:
        return _underpowered("Pearson r", n, MIN_N_FOR_CORRELATION)
    if np.std(c.a) == 0 or np.std(c.b) == 0:
        return MetricResult("Pearson r", None, n, "one series has no variation",
                            reliable=False)
    r = float(np.corrcoef(c.a, c.b)[0, 1])
    return MetricResult(
        "Pearson r", r, n, reliable=True,
        interpretation=_read_r(r, c.transform),
    )


def spearman(c: Comparison) -> MetricResult:
    """Rank correlation: robust to outliers and to a non-linear relationship."""
    n = c.n
    if n < MIN_N_FOR_CORRELATION:
        return _underpowered("Spearman rho", n, MIN_N_FOR_CORRELATION)
    ra, rb = _rank(c.a), _rank(c.b)
    if np.std(ra) == 0 or np.std(rb) == 0:
        return MetricResult("Spearman rho", None, n, "no rank variation", False)
    rho = float(np.corrcoef(ra, rb)[0, 1])
    return MetricResult(
        "Spearman rho", rho, n, reliable=True,
        interpretation=("rank agreement; less sensitive than Pearson to a single "
                        "large move dominating the result"),
    )


def _rank(x: np.ndarray) -> np.ndarray:
    """Average ranks, so ties do not distort the coefficient."""
    order = np.argsort(x)
    ranks = np.empty(len(x), dtype=float)
    ranks[order] = np.arange(1, len(x) + 1, dtype=float)
    # Average tied ranks.
    for value in np.unique(x):
        mask = x == value
        if mask.sum() > 1:
            ranks[mask] = ranks[mask].mean()
    return ranks


def mae(c: Comparison) -> MetricResult:
    if c.n < MIN_N_FOR_ANY_METRIC:
        return _underpowered("MAE", c.n, MIN_N_FOR_ANY_METRIC)
    v = float(np.mean(np.abs(c.a - c.b)))
    unit = "index points" if c.transform in ("level", "rebased_level") else "log points"
    return MetricResult("MAE", v, c.n, reliable=c.n >= MIN_N_FOR_CORRELATION,
                        interpretation=f"mean absolute difference, in {unit}")


def rmse(c: Comparison) -> MetricResult:
    if c.n < MIN_N_FOR_ANY_METRIC:
        return _underpowered("RMSE", c.n, MIN_N_FOR_ANY_METRIC)
    v = float(np.sqrt(np.mean((c.a - c.b) ** 2)))
    return MetricResult("RMSE", v, c.n, reliable=c.n >= MIN_N_FOR_CORRELATION,
                        interpretation="penalises large divergences more than MAE")


def mape(c: Comparison) -> MetricResult:
    """Only meaningful on levels. On growth rates the denominator crosses zero."""
    if c.transform not in ("level", "rebased_level"):
        return MetricResult(
            "MAPE", None, c.n, reliable=False,
            interpretation=("not applicable to growth rates: the denominator "
                            "passes through zero and the statistic explodes"))
    if c.n < MIN_N_FOR_ANY_METRIC:
        return _underpowered("MAPE", c.n, MIN_N_FOR_ANY_METRIC)
    nz = c.b != 0
    if not nz.any():
        return MetricResult("MAPE", None, c.n, "reference is all zeros", False)
    v = float(np.mean(np.abs((c.a[nz] - c.b[nz]) / c.b[nz])) * 100)
    return MetricResult("MAPE", v, c.n, reliable=c.n >= MIN_N_FOR_CORRELATION,
                        interpretation="mean absolute percentage error")


def direction_agreement(c: Comparison) -> MetricResult:
    """Share of periods where both series moved the same way.

    The most robust statistic available on a short sample. It ignores magnitude
    entirely, which is a weakness in general and a strength here: magnitudes are
    not comparable between a lowest-fare index over two carriers and an
    all-carrier average, but direction is.
    """
    if c.transform in ("level", "rebased_level"):
        a, b = np.diff(c.a), np.diff(c.b)
    else:
        a, b = c.a, c.b
    n = len(a)
    if n < MIN_N_FOR_ANY_METRIC:
        return _underpowered("Direction agreement", n, MIN_N_FOR_ANY_METRIC)
    agree = float(np.mean(np.sign(a) == np.sign(b)))
    return MetricResult(
        "Direction agreement", agree, n, reliable=n >= MIN_N_FOR_CORRELATION,
        interpretation=("share of periods moving the same way; 0.5 is what "
                        "coin-flipping would give"),
    )


def theil_u2(c: Comparison) -> MetricResult:
    """Theil's U2: RMSE relative to a naive no-change forecast.

    Below 1 means the compared series tracks better than assuming no change;
    at or above 1 it does not beat the naive benchmark.
    """
    if c.transform in ("level", "rebased_level"):
        return MetricResult(
            "Theil U2", None, c.n, reliable=False,
            interpretation="computed on growth rates, not levels")
    if c.n < MIN_N_FOR_ANY_METRIC:
        return _underpowered("Theil U2", c.n, MIN_N_FOR_ANY_METRIC)
    denom = float(np.sqrt(np.mean(c.b ** 2)))
    if denom == 0:
        return MetricResult("Theil U2", None, c.n, "reference has no movement", False)
    v = float(np.sqrt(np.mean((c.a - c.b) ** 2)) / denom)
    return MetricResult(
        "Theil U2", v, c.n, reliable=c.n >= MIN_N_FOR_CORRELATION,
        interpretation="<1 tracks better than assuming no change; >=1 does not")


def _read_r(r: float, transform: Transform) -> str:
    if transform in ("level", "rebased_level"):
        return ("CORRELATION ON LEVELS IS NOT EVIDENCE OF AGREEMENT. Two price "
                "indices both trend, so this coefficient mostly measures that "
                "shared trend. Read the growth-rate correlation instead.")
    strength = (
        "strong" if abs(r) >= 0.7 else
        "moderate" if abs(r) >= 0.4 else
        "weak" if abs(r) >= 0.2 else
        "negligible"
    )
    return f"{strength} {'positive' if r >= 0 else 'negative'} co-movement in growth rates"


ALL_METRICS = (pearson, spearman, mae, rmse, mape, direction_agreement, theil_u2)


def compute_all(c: Comparison) -> Comparison:
    c.metrics = [m(c) for m in ALL_METRICS]
    return c


def compare(
    a: dict[dt.date, float],
    b: dict[dt.date, float],
    *,
    transform: Transform = "log_diff",
    a_name: str = "APIx",
    b_name: str = "reference",
) -> Comparison:
    return compute_all(
        align_and_transform(a, b, transform=transform, a_name=a_name, b_name=b_name))
