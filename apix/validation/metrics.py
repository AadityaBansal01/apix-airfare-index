"""Scoring functions for validating the index against a reference series.

WHY THESE METRICS AND NOT ABSOLUTE ERROR
----------------------------------------
APIx tracks the lowest available fare a traveller is quoted. Any plausible
external benchmark, DGCA's tariff monitoring included, reflects realised revenue
across all booking classes. Those are different quantities and they live at
different levels, so scoring absolute error between them would fail a correct
index for measuring the thing it was built to measure.

What must agree is movement. So the primary scores are:

* **Pearson correlation** on month-over-month changes, not on levels. Two series
  that both trend upward correlate on levels almost regardless of merit; the
  question is whether they move together month to month.
* **Spearman rank correlation**, which survives a non-linear relationship
  between quoted and realised fares.
* **Direction agreement**, the share of months where both series move the same
  way. The plainest statement of "does this track the market", and the one a
  non-specialist can check.

Absolute error is reported only after rebasing both series to a common anchor,
and is labelled as a shape comparison rather than an accuracy claim.

Everything here is pure and takes plain sequences, so the whole scoring layer is
testable without a database.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

#: Below this many paired observations, correlation is not reported at all.
#: Three points can produce r = 0.99 by coincidence, and a back-test that
#: publishes such a number is worse than one that declines to.
MIN_PAIRS_FOR_CORRELATION = 6


class NotEnoughData(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Score:
    name: str
    value: float | None
    n: int
    interpretation: str
    reliable: bool = True

    def __str__(self) -> str:
        v = "n/a" if self.value is None else f"{self.value:.4f}"
        flag = "" if self.reliable else "  [too few observations]"
        return f"{self.name:<26} {v:>9}  (n={self.n}){flag}"


def _clean_pairs(a: Sequence[float], b: Sequence[float]) -> tuple[list[float], list[float]]:
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")
    pairs = [(x, y) for x, y in zip(a, b)
             if x is not None and y is not None
             and not (isinstance(x, float) and math.isnan(x))
             and not (isinstance(y, float) and math.isnan(y))]
    if not pairs:
        return [], []
    xs, ys = zip(*pairs)
    return list(xs), list(ys)


def pearson(a: Sequence[float], b: Sequence[float]) -> float | None:
    xs, ys = _clean_pairs(a, b)
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None          # a constant series has no correlation, not r=0
    return num / (dx * dy)


def _ranks(values: Sequence[float]) -> list[float]:
    """Average ranks, so ties do not distort Spearman."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(a: Sequence[float], b: Sequence[float]) -> float | None:
    xs, ys = _clean_pairs(a, b)
    if len(xs) < 2:
        return None
    return pearson(_ranks(xs), _ranks(ys))


def changes(series: Sequence[float]) -> list[float]:
    """Period-over-period percentage change. One shorter than the input."""
    out = []
    for prev, cur in zip(series, series[1:]):
        out.append(None if not prev else (cur / prev - 1) * 100)
    return out


def direction_agreement(a: Sequence[float], b: Sequence[float],
                        tolerance: float = 0.0) -> tuple[float | None, int]:
    """Share of periods where both series move the same way.

    `tolerance` treats a move smaller than that percentage as flat, so two series
    that are both essentially unchanged are not scored as disagreeing because of
    rounding noise in the fourth decimal.
    """
    ca, cb = changes(a), changes(b)
    xs, ys = _clean_pairs(ca, cb)
    if not xs:
        return None, 0

    def sign(v: float) -> int:
        if v > tolerance:
            return 1
        if v < -tolerance:
            return -1
        return 0

    agree = sum(1 for x, y in zip(xs, ys) if sign(x) == sign(y))
    return agree / len(xs), len(xs)


def rebase(series: Sequence[float], anchor_index: int = 0) -> list[float] | None:
    """Rescale so the anchor observation equals 100."""
    if not series:
        return None
    anchor = series[anchor_index]
    if not anchor:
        return None
    return [v / anchor * 100 for v in series]


def mape(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Mean absolute percentage error. Only meaningful on rebased series."""
    xs, ys = _clean_pairs(a, b)
    pairs = [(x, y) for x, y in zip(xs, ys) if x != 0]
    if not pairs:
        return None
    return sum(abs((x - y) / x) for x, y in pairs) / len(pairs) * 100


def rmse(a: Sequence[float], b: Sequence[float]) -> float | None:
    xs, ys = _clean_pairs(a, b)
    if not xs:
        return None
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(xs, ys)) / len(xs))


@dataclass
class ComparisonResult:
    n_periods: int
    scores: list[Score] = field(default_factory=list)
    reliable: bool = True
    note: str = ""

    def get(self, name: str) -> Score | None:
        return next((s for s in self.scores if s.name == name), None)

    def as_dict(self) -> dict:
        return {
            "n_periods": self.n_periods,
            "reliable": self.reliable,
            "note": self.note,
            "scores": {s.name: {"value": s.value, "n": s.n,
                                "interpretation": s.interpretation,
                                "reliable": s.reliable}
                       for s in self.scores},
        }


def compare(index: Sequence[float], reference: Sequence[float],
            *, label: str = "reference") -> ComparisonResult:
    """Score an index series against a reference series of the same length."""
    if len(index) != len(reference):
        raise ValueError("index and reference must be the same length")

    n = len(index)
    reliable = n >= MIN_PAIRS_FOR_CORRELATION
    scores: list[Score] = []

    ci, cr = changes(index), changes(reference)
    n_changes = len(_clean_pairs(ci, cr)[0])

    r_changes = pearson(ci, cr)
    scores.append(Score(
        "pearson_r_on_changes", r_changes, n_changes,
        "Correlation of month-over-month percentage changes. The primary score: "
        "levels differ by construction, movement is what must agree.",
        reliable=n_changes >= MIN_PAIRS_FOR_CORRELATION))

    rho = spearman(index, reference)
    scores.append(Score(
        "spearman_rho_on_levels", rho, n,
        "Rank correlation of levels. Survives a non-linear relationship between "
        "quoted and realised fares.",
        reliable=reliable))

    agree, n_dir = direction_agreement(index, reference, tolerance=0.05)
    scores.append(Score(
        "direction_agreement", agree, n_dir,
        "Share of periods where both series move the same way. 0.5 is what a "
        "coin would score.",
        reliable=n_dir >= MIN_PAIRS_FOR_CORRELATION))

    ri, rr = rebase(index), rebase(reference)
    if ri and rr:
        scores.append(Score(
            "mape_rebased", mape(ri, rr), n,
            "Mean absolute percentage error after rebasing both to 100 at the "
            "first common period. A shape comparison, NOT an accuracy claim: the "
            "two series measure different quantities and their levels are not "
            "expected to match.",
            reliable=reliable))
        scores.append(Score(
            "rmse_rebased", rmse(ri, rr), n,
            "Root mean squared error on the rebased series, in index points.",
            reliable=reliable))

    note = ""
    if not reliable:
        note = (f"Only {n} paired periods. Below {MIN_PAIRS_FOR_CORRELATION} "
                f"these statistics are not interpretable and are reported for "
                f"completeness only.")

    return ComparisonResult(n_periods=n, scores=scores, reliable=reliable, note=note)
