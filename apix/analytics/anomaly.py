"""Surge and anomaly detection on the index series.

Moved here from the dashboard's JavaScript. Detection logic that lives only in
the browser is detection logic the test suite cannot reach, and it would have
had to be reimplemented for the API, giving two copies to drift apart. The
browser now renders flags this module produced.

WHAT IS BEING DETECTED, AND WHAT IS NOT
---------------------------------------
This flags days whose movement is large **relative to the series' own recent
volatility**. That is a statistical statement about the index, not a claim about
the world. A flag says "this day moved unusually for this series"; it does not
say a festival caused it, and this module never asserts that it did.

`label_hypotheses` exists precisely so the distinction survives contact with a
dashboard. It attaches candidate explanations, clearly typed as hypotheses, to a
flag that was raised on purely statistical grounds. A reader sees "flagged, and
Diwali falls in this window" rather than "Diwali caused this".

WHY MEDIAN AND MAD, NOT MEAN AND STANDARD DEVIATION
---------------------------------------------------
Same reasoning as the cleaner in apix/pipeline/outliers.py. A mean and standard
deviation have a breakdown point of zero, so the surge being looked for inflates
the very yardstick meant to detect it and can hide inside its own contribution.
The median and MAD have a 50% breakdown point.

Detection runs on **day-over-day changes**, not on levels. A level-based detector
would flag every day of a sustained plateau; what matters is when the series
moves, and a surge that arrives and persists should be flagged on arrival.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence

#: Trailing days used to estimate normal volatility. Two weeks is long enough to
#: contain a full weekly cycle and short enough to adapt to a regime change.
DEFAULT_WINDOW = 14

#: Modified z-score threshold, as in the cleaner. Iglewicz & Hoaglin's 3.5.
DEFAULT_THRESHOLD = 3.5

#: Below this many trailing observations the volatility estimate is not
#: meaningful and nothing is flagged. A detector that fires on its third ever
#: observation is measuring its own warm-up.
MIN_HISTORY = 10

_MAD_SCALE = 0.6745


@dataclass(frozen=True, slots=True)
class Anomaly:
    index_date: dt.date
    index_value: Decimal
    change_pct: float
    score: float
    direction: str            # "spike" | "drop"
    window: int
    threshold: float
    hypotheses: list[str] = field(default_factory=list)

    @property
    def is_spike(self) -> bool:
        return self.direction == "spike"

    def as_dict(self) -> dict:
        return {
            "index_date": self.index_date.isoformat(),
            "index_value": float(self.index_value),
            "change_pct": round(self.change_pct, 4),
            "score": round(self.score, 4),
            "direction": self.direction,
            "window": self.window,
            "threshold": self.threshold,
            "hypotheses": list(self.hypotheses),
            "note": (
                "Flagged because this day's movement is large relative to the "
                "series' own recent volatility. This is a statistical flag, not "
                "a causal finding."),
        }


def _median(xs: Sequence[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def detect(
    series: Sequence[tuple[dt.date, Decimal]],
    *,
    window: int = DEFAULT_WINDOW,
    threshold: float = DEFAULT_THRESHOLD,
    min_history: int = MIN_HISTORY,
) -> list[Anomaly]:
    """Flag days whose change is extreme against a trailing rolling baseline.

    `series` must be sorted by date. Only history strictly before a day is used
    to judge it, so the detector never sees the future and a flag raised on the
    latest day would have been raised in real time.
    """
    if len(series) < min_history + 1:
        return []

    dates = [d for d, _ in series]
    values = [float(v) for _, v in series]

    changes: list[float | None] = [None]
    for prev, cur in zip(values, values[1:]):
        changes.append((cur - prev) / prev if prev else None)

    out: list[Anomaly] = []
    flagged: set[int] = set()
    for i in range(1, len(series)):
        # Exclude already-flagged days from the baseline.
        #
        # Without this an event poisons the yardstick used to judge the days
        # that follow it: a genuine surge enters the trailing window, shifts the
        # median and MAD, and then ordinary days look extreme against the
        # distorted baseline. On a four-day surge that produced a tail of six
        # false positives stretching two weeks past the event. The baseline
        # should describe normal behaviour, so it is built only from days not
        # already judged abnormal.
        history = [
            changes[j] for j in range(max(1, i - window), i)
            if changes[j] is not None and j not in flagged
        ]
        if len(history) < min_history:
            continue
        med = _median(history)
        mad = _median([abs(c - med) for c in history])
        if mad == 0:
            continue                      # no dispersion: nothing to compare to
        current = changes[i]
        if current is None:
            continue
        score = _MAD_SCALE * (current - med) / mad
        if abs(score) <= threshold:
            continue
        flagged.add(i)
        out.append(Anomaly(
            index_date=dates[i],
            index_value=series[i][1],
            change_pct=current * 100.0,
            score=score,
            direction="spike" if current > med else "drop",
            window=window,
            threshold=threshold,
        ))
    return out


def collapse_episodes(
    anomalies: Sequence[Anomaly],
    max_gap_days: int = 2,
) -> list[list[Anomaly]]:
    """Group nearby flags into episodes.

    A multi-day surge is one event, not four. Reporting each day separately
    inflates the count and makes a single festival look like a pattern.

    GROUPING IGNORES DIRECTION, DELIBERATELY. Because detection runs on
    day-over-day changes, a price excursion produces at least two flags: the
    move away from the baseline and the move back to it. Those are one event.
    An earlier version of this function split on direction and therefore
    reported every one-day spike as two episodes, which is precisely the
    inflation it exists to prevent. Adjacency in time is the right criterion;
    the excursion's direction is that of its largest single move.
    """
    if not anomalies:
        return []
    ordered = sorted(anomalies, key=lambda a: a.index_date)
    episodes: list[list[Anomaly]] = [[ordered[0]]]
    for a in ordered[1:]:
        if (a.index_date - episodes[-1][-1].index_date).days <= max_gap_days:
            episodes[-1].append(a)
        else:
            episodes.append([a])
    return episodes


def episode_direction(episode: Sequence[Anomaly]) -> str:
    """The direction of an episode's largest single move."""
    return max(episode, key=lambda a: abs(a.score)).direction


def episode_peak(episode: Sequence[Anomaly]) -> Anomaly:
    """The most extreme day in an episode, which is what a reader wants named."""
    return max(episode, key=lambda a: abs(a.score))


# ---------------------------------------------------------------------------
# Hypotheses, never conclusions
# ---------------------------------------------------------------------------

#: Recurring demand peaks in the Indian domestic calendar. Approximate windows,
#: because most of these are lunar and move year to year. Deliberately coarse:
#: this exists to prompt a human to check, not to attribute.
SEASONAL_WINDOWS: tuple[tuple[int, int, int, str], ...] = (
    (10, 15, 11, "Diwali travel period (date varies by year; verify)"),
    (12, 20, 1, "Christmas and New Year peak"),
    (3, 1, 3, "financial year end, business travel"),
    (5, 1, 6, "summer school holidays"),
)


def label_hypotheses(anomaly: Anomaly,
                     extra: Iterable[str] = ()) -> Anomaly:
    """Attach candidate explanations to a flag raised on statistical grounds.

    Returns a new Anomaly. Every label is phrased as something to check, not as
    a cause, because this module has no evidence about causation and a dashboard
    that says "Diwali" next to a spike will be read as an explanation.
    """
    hypotheses: list[str] = []
    m, d = anomaly.index_date.month, anomaly.index_date.day
    for start_m, start_d, end_m, label in SEASONAL_WINDOWS:
        if start_m <= end_m:
            inside = (start_m, start_d) <= (m, d) and m <= end_m
        else:                              # window wraps the year end
            inside = (m, d) >= (start_m, start_d) or m <= end_m
        if inside:
            hypotheses.append(f"candidate: {label}")
    hypotheses.extend(extra)
    if not hypotheses:
        hypotheses.append("no candidate identified; cause unknown")

    return Anomaly(
        index_date=anomaly.index_date,
        index_value=anomaly.index_value,
        change_pct=anomaly.change_pct,
        score=anomaly.score,
        direction=anomaly.direction,
        window=anomaly.window,
        threshold=anomaly.threshold,
        hypotheses=hypotheses,
    )


def detect_and_label(
    series: Sequence[tuple[dt.date, Decimal]],
    **kwargs,
) -> list[Anomaly]:
    return [label_hypotheses(a) for a in detect(series, **kwargs)]
