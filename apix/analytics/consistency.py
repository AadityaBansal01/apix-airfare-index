"""Internal consistency checks on a built index.

WHY THESE MATTER MORE THAN THE EXTERNAL COMPARISON
--------------------------------------------------
The external validation in this project is weak, and honestly so: the reference
series available for Indian domestic airfares are monthly at best, so a 30-day
window yields one or two overlapping observations, and no correlation computed on
that is worth quoting.

These checks are the opposite. Each is a property the index must satisfy by
construction, each is checkable exactly, and each would catch a real class of
bug:

*   **Base period equals 100.** If the price reference period does not index to
    100, the base prices and the index are computed from different data. This is
    the single most likely silent error in an index engine.
*   **Temporal consistency.** Monthly must equal the mean of its daily values.
    If it does not, the aggregation is not doing what the methodology says.
*   **Contributions reconstruct the headline.** The route breakdown must sum to
    the published figure, or the weights and the aggregate disagree.
*   **Coverage arithmetic.** The coverage ratio must equal the observed share of
    basket weight, or a partially observed basket is being reported as complete.
*   **Reproducibility.** A fixed seed must produce byte-identical output. A
    statistical series that changes when you rerun the pipeline is not a
    statistic.

A failure here is a bug in the engine. A weak external correlation might just be
a weak reference series. That asymmetry is why these run first.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Sequence

#: Index values are stored at four decimal places, so agreement is checked to a
#: tolerance just above the storage precision rather than to exact equality.
TOLERANCE = Decimal("0.0002")


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    detail: str
    severity: str = "error"        # error | warning | info
    observed: str | None = None
    expected: str | None = None

    def __str__(self) -> str:
        mark = "PASS" if self.passed else ("WARN" if self.severity == "warning" else "FAIL")
        return f"  [{mark}] {self.name}: {self.detail}"


@dataclass(slots=True)
class ConsistencyReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> None:
        self.checks.append(check)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "error"]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.severity == "warning"]

    @property
    def passed(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        n_pass = sum(1 for c in self.checks if c.passed)
        lines = [str(c) for c in self.checks]
        lines.append("")
        lines.append(
            f"  {n_pass}/{len(self.checks)} checks passed"
            + (f", {len(self.failures)} failed" if self.failures else "")
            + (f", {len(self.warnings)} warnings" if self.warnings else "")
        )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def check_base_period_is_100(
    base_prices: dict,
    index_at_base: Decimal | None,
    tolerance: Decimal = Decimal("0.5"),
) -> Check:
    """The index over the price reference period must equal 100.

    Tolerance is looser than elsewhere because the reference period is a range of
    days whose individual values scatter around 100; what must hold is that the
    period as a whole centres there. A systematic offset means base prices and
    current prices were computed from different data.
    """
    if index_at_base is None:
        return Check(
            "base period equals 100", False,
            "no index value available inside the price reference period",
            severity="warning")
    diff = abs(index_at_base - Decimal("100"))
    ok = diff <= tolerance
    return Check(
        "base period equals 100", ok,
        f"mean index over the reference period is {index_at_base:.4f} "
        f"(deviation {diff:.4f}, tolerance {tolerance})",
        observed=str(index_at_base), expected="100")


def check_temporal_consistency(
    daily: dict[dt.date, Decimal],
    aggregated: dict[dt.date, Decimal],
    period_key: Callable[[dt.date], dt.date],
    label: str,
    tolerance: Decimal = TOLERANCE,
) -> Check:
    """Each aggregate must equal the mean of the daily values in its period."""
    buckets: dict[dt.date, list[Decimal]] = {}
    for d, v in daily.items():
        buckets.setdefault(period_key(d), []).append(v)

    worst = Decimal(0)
    worst_period = None
    compared = 0
    for period, published in aggregated.items():
        members = buckets.get(period)
        if not members:
            continue
        expected = sum(members) / Decimal(len(members))
        diff = abs(published - expected)
        compared += 1
        if diff > worst:
            worst, worst_period = diff, period

    if compared == 0:
        return Check(f"{label} equals the mean of its daily values", False,
                     "no overlapping periods to compare", severity="warning")
    ok = worst <= tolerance
    return Check(
        f"{label} equals the mean of its daily values", ok,
        f"{compared} periods compared, largest deviation {worst:.6f}"
        + (f" at {worst_period}" if worst_period and not ok else ""),
        observed=str(worst), expected=f"<= {tolerance}")


def check_contributions_sum(
    headline: dict[dt.date, Decimal],
    contributions: dict[dt.date, list[Decimal]],
    tolerance: Decimal = Decimal("0.01"),
) -> Check:
    """Route contributions must reconstruct the headline for every date."""
    worst = Decimal(0)
    worst_date = None
    compared = 0
    for day, value in headline.items():
        parts = contributions.get(day)
        if not parts:
            continue
        diff = abs(sum(parts) - value)
        compared += 1
        if diff > worst:
            worst, worst_date = diff, day
    if compared == 0:
        return Check("route contributions sum to the headline", False,
                     "no dates with a route breakdown", severity="warning")
    ok = worst <= tolerance
    return Check(
        "route contributions sum to the headline", ok,
        f"{compared} dates checked, largest deviation {worst:.6f}"
        + (f" at {worst_date}" if worst_date and not ok else ""),
        observed=str(worst), expected=f"<= {tolerance}")


def check_coverage_arithmetic(
    rows: Sequence[dict],
    total_cells: int,
    tolerance: Decimal = Decimal("0.02"),
) -> Check:
    """coverage_ratio must be consistent with the number of cells observed.

    A full basket must report coverage 1. A partial basket must report less, and
    roughly in proportion to the cells it lost. Cells carry unequal weight so the
    relationship is not exact, which is why the tolerance is generous; what this
    catches is a partial basket claiming to be complete.
    """
    bad = []
    for r in rows:
        cov = Decimal(str(r["coverage_ratio"]))
        n = int(r["n_cells"])
        if n >= total_cells and abs(cov - Decimal(1)) > tolerance:
            bad.append(f"{r['index_date']}: all {n} cells but coverage {cov}")
        if n < total_cells and cov >= Decimal(1):
            bad.append(f"{r['index_date']}: only {n}/{total_cells} cells but "
                       f"coverage {cov}")
    ok = not bad
    return Check(
        "coverage ratio matches cells observed", ok,
        f"{len(rows)} dates checked" if ok else "; ".join(bad[:3]),
        observed=str(len(bad)), expected="0 inconsistencies")


def check_no_impossible_values(rows: Sequence[dict]) -> Check:
    """Index values must be positive and finite; coverage must be in (0, 1]."""
    bad = []
    for r in rows:
        v = Decimal(str(r["index_value"]))
        cov = Decimal(str(r["coverage_ratio"]))
        if v <= 0:
            bad.append(f"{r['index_date']}: index {v}")
        if not (Decimal(0) < cov <= Decimal(1)):
            bad.append(f"{r['index_date']}: coverage {cov}")
    return Check(
        "no impossible index or coverage values", not bad,
        f"{len(rows)} rows checked" if not bad else "; ".join(bad[:3]))


def check_series_is_continuous(
    rows: Sequence[dict], expected_step: dt.timedelta = dt.timedelta(days=1)
) -> Check:
    """Gaps in a daily series are worth surfacing rather than silently spanning.

    A warning rather than an error: a genuine collection outage produces a gap,
    and the correct response is to disclose it, not to fail the build.
    """
    dates = sorted(dt.date.fromisoformat(str(r["index_date"])) for r in rows)
    gaps = [
        (a, b) for a, b in zip(dates, dates[1:]) if (b - a) > expected_step
    ]
    return Check(
        "daily series has no gaps", not gaps,
        f"{len(dates)} consecutive days" if not gaps
        else f"{len(gaps)} gap(s), first between {gaps[0][0]} and {gaps[0][1]}",
        severity="warning")


def check_reproducible(first: Sequence[Decimal], second: Sequence[Decimal]) -> Check:
    """Two runs from the same seed must produce identical values.

    A statistical series that changes when the pipeline is rerun cannot be
    published, because a user cannot tell a revision from a bug.
    """
    if len(first) != len(second):
        return Check("rebuild is reproducible", False,
                     f"different lengths: {len(first)} vs {len(second)}")
    diffs = [abs(a - b) for a, b in zip(first, second)]
    worst = max(diffs) if diffs else Decimal(0)
    ok = worst == 0
    return Check(
        "rebuild is reproducible", ok,
        f"{len(first)} values identical across runs" if ok
        else f"largest difference {worst}",
        observed=str(worst), expected="0")


def check_sufficient_history(rows: Sequence[dict], required: int = 30) -> Check:
    """The problem statement asks for at least 30 days of back-tested results."""
    n = len(rows)
    return Check(
        f"at least {required} daily observations", n >= required,
        f"{n} daily index values available",
        observed=str(n), expected=f">= {required}")
