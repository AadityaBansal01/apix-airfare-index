"""Outlier detection for airfare quotes.

THE TRAP THIS MODULE AVOIDS
---------------------------
The obvious reading of "detect outliers in fare data" is to find unusually high
fares and drop them. For this project that would destroy the measurement.

APIx exists because airfares swing 200-400% within a booking window. Those swings
are the signal. A detector tuned to flag statistically extreme fares would remove
exactly the observations the index is built to capture, and it would do so hardest
during festival peaks and fuel-price shocks, which is when a statistical office
most needs the number. The index would look reassuringly smooth and be wrong.

So the rule here is narrow and deliberate:

    We detect DATA ERRORS. We do not detect EXPENSIVE FLIGHTS.

Three consequences follow.

**No detection across time.** A fare five times yesterday's is a price movement,
not an error. Every comparison group in this module is confined to a single
scrape date, so a genuine surge can never be flagged.

**Thresholds are deliberately loose.** We are hunting parse failures that
misplace a decimal or pick up a package price, which show up as order-of-magnitude
deviations. A quote merely at the expensive end of a day's spread is kept.

**Nothing is deleted.** A flagged row is written to the database with its flag,
its method and its score, and excluded from the index. The exclusion is auditable
and reversible; if the threshold turns out to be wrong, the data is still there.

WHY MODIFIED Z-SCORE ON LOG FARES, NOT IQR OR PLAIN Z-SCORE
-----------------------------------------------------------
*   **Plain z-score** uses the mean and standard deviation, both of which have a
    breakdown point of zero: a single 100x parse error drags the mean toward
    itself and inflates the standard deviation, so the error masks its own
    detection. On a sample of twenty quotes one bad value can hide completely.

*   **IQR / Tukey fences** are robust, but they are symmetric on the raw scale
    while fare distributions are strongly right-skewed. The upper fence sits far
    too close to legitimate high fares and the lower fence is often below zero.
    Offered here as an alternative for comparison, not as the default.

*   **Modified z-score (median and MAD) on log fares** is the default. The
    median and MAD have a 50% breakdown point, so contamination cannot hide
    itself. The log transform turns the multiplicative structure of fares into an
    additive one, which is the scale price relatives actually live on and the
    same scale the Jevons index works in. Consistency between the cleaner and the
    index formula is not an accident.

The 0.6745 constant makes the modified z-score comparable to a standard z-score
under normality; the 3.5 threshold is the Iglewicz and Hoaglin recommendation.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Sequence

#: Scaling constant: 0.6745 is the 0.75 quantile of the standard normal, which
#: makes MAD a consistent estimator of sigma for normally distributed data.
_MAD_SCALE = 0.6745

#: Iglewicz & Hoaglin (1993) recommend 3.5 for the modified z-score. On the log
#: scale used here, that corresponds to a large multiplicative deviation, which
#: is what a parse error looks like and an expensive flight does not.
DEFAULT_MAD_THRESHOLD = 3.5

#: Tukey's outer fence. 1.5 is the usual "suspected outlier" multiplier; we use
#: 3.0 because at 1.5 the fence sits inside the legitimate spread of fares.
DEFAULT_IQR_K = 3.0

#: Below this many quotes, robust dispersion cannot be estimated and NOTHING is
#: flagged. Flagging on n=3 would be numerology, and on a thin route it would
#: systematically discard the cheapest or dearest carrier.
MIN_SAMPLE_FOR_DISPERSION = 5


class Method(str, Enum):
    MAD_LOG = "mad_log"
    IQR = "iqr"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Verdict:
    is_outlier: bool
    method: str
    score: float
    reason: str


def _clean(v: Verdict | None = None) -> Verdict:
    return Verdict(False, Method.NONE.value, 0.0, "")


def modified_zscores(values: Sequence[float]) -> list[float] | None:
    """Modified z-scores on the given values. None when dispersion is degenerate.

    Returns None rather than zeros when MAD is 0 (every value identical, or more
    than half identical), because "no dispersion" and "no outliers" are different
    statements and the caller should not conflate them.
    """
    n = len(values)
    if n < MIN_SAMPLE_FOR_DISPERSION:
        return None
    med = statistics.median(values)
    deviations = [abs(v - med) for v in values]
    mad = statistics.median(deviations)
    if mad == 0:
        return None
    return [_MAD_SCALE * (v - med) / mad for v in values]


def detect_mad_log(
    fares: Sequence[Decimal],
    threshold: float = DEFAULT_MAD_THRESHOLD,
) -> list[Verdict]:
    """Default detector: modified z-score on natural-log fares."""
    if len(fares) < MIN_SAMPLE_FOR_DISPERSION:
        return [
            Verdict(False, Method.MAD_LOG.value, 0.0,
                    f"sample of {len(fares)} below minimum "
                    f"{MIN_SAMPLE_FOR_DISPERSION}; not assessed")
            for _ in fares
        ]
    logs = [math.log(float(f)) for f in fares]
    scores = modified_zscores(logs)
    if scores is None:
        return [
            Verdict(False, Method.MAD_LOG.value, 0.0,
                    "zero median absolute deviation; not assessed")
            for _ in fares
        ]
    out = []
    for f, s in zip(fares, scores):
        flagged = abs(s) > threshold
        out.append(Verdict(
            is_outlier=flagged,
            method=Method.MAD_LOG.value,
            score=round(s, 4),
            reason=(f"modified z-score {s:.2f} exceeds {threshold} on log fares"
                    if flagged else ""),
        ))
    return out


def detect_iqr(
    fares: Sequence[Decimal],
    k: float = DEFAULT_IQR_K,
) -> list[Verdict]:
    """Tukey fences on the raw scale. Provided for comparison, not the default."""
    n = len(fares)
    if n < MIN_SAMPLE_FOR_DISPERSION:
        return [Verdict(False, Method.IQR.value, 0.0,
                        f"sample of {n} below minimum; not assessed") for _ in fares]
    vals = sorted(float(f) for f in fares)
    q1, q3 = _quantile(vals, 0.25), _quantile(vals, 0.75)
    iqr = q3 - q1
    if iqr == 0:
        return [Verdict(False, Method.IQR.value, 0.0,
                        "zero interquartile range; not assessed") for _ in fares]
    lo, hi = q1 - k * iqr, q3 + k * iqr
    out = []
    for f in fares:
        v = float(f)
        flagged = v < lo or v > hi
        # Score expressed in IQRs beyond the nearer fence, so it is comparable
        # in spirit to the modified z-score.
        score = (lo - v) / iqr if v < lo else ((v - hi) / iqr if v > hi else 0.0)
        out.append(Verdict(
            is_outlier=flagged,
            method=Method.IQR.value,
            score=round(score, 4),
            reason=(f"outside Tukey fences [{lo:.0f}, {hi:.0f}] with k={k}"
                    if flagged else ""),
        ))
    return out


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Linear interpolation between order statistics (numpy's default method)."""
    if not sorted_vals:
        raise ValueError("empty sequence")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_vals[int(pos)]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def detect(
    fares: Sequence[Decimal],
    method: Method | str = Method.MAD_LOG,
    **kwargs,
) -> list[Verdict]:
    m = Method(method)
    if m is Method.MAD_LOG:
        return detect_mad_log(fares, **kwargs)
    if m is Method.IQR:
        return detect_iqr(fares, **kwargs)
    return [_clean() for _ in fares]
