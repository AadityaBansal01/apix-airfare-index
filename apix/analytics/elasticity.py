"""Lead-time elasticity: how much more a traveller pays for booking later.

This is the analytical artifact the problem statement asks for, as distinct from
the index itself. It answers something a monthly CPI observation structurally
cannot: a monthly collection samples one booking horizon, so it cannot see the
shape of the curve at all, let alone that the shape differs by route.

TWO MODELS, AND WHY BOTH
------------------------
An earlier specification for this module said "fit log(fare) ~ a + b*window and
recover the generator's amplitude and decay constant". Those two requirements are
incompatible, and the discrepancy is worth stating rather than quietly resolving.

Fares fall towards an asymptote as the booking horizon lengthens: there is a
floor price, and the last-minute premium decays towards it. That is

    fare(w) = L * (1 + a * exp(-w / tau))

which is not log-linear in w. A log-linear fit cannot recover `a` and `tau`
because they are not parameters of a log-linear model. So:

1.  **Exponential decay (primary).** Fitted by nonlinear least squares. Recovers
    the floor `L`, the last-minute amplitude `a` and the decay constant `tau`
    directly, each with a standard error and a 95% interval. This is the model
    that matches both the documented shape of airline pricing and the process
    the synthetic generator uses, so the tests can check it against known truth.

2.  **Log-linear semi-elasticity (secondary).** OLS of log(fare) on the window.
    Its slope is the average percentage change in fare per additional day of
    lead time, which is one interpretable number a non-specialist can carry
    around. It is a deliberately misspecified summary of a curved relationship,
    and is reported as such, never as the model.

Reporting only the second would flatter the analysis: a single elasticity implies
a constant percentage effect, when the whole point is that the effect is
concentrated in the last fortnight.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Sequence

log = logging.getLogger(__name__)

#: Below this many observations a three-parameter curve is not identified.
MIN_OBSERVATIONS = 12

#: Below this many distinct booking windows the curve's shape is unconstrained:
#: two points can be joined by infinitely many exponentials.
MIN_DISTINCT_WINDOWS = 4

Z95 = 1.959963984540054


class NotIdentified(Exception):
    """Too few observations, or too few distinct windows, to fit."""


@dataclass(frozen=True, slots=True)
class Estimate:
    """A parameter with its uncertainty. Never a bare point estimate."""
    value: float
    std_error: float | None
    ci_low: float | None
    ci_high: float | None

    @property
    def significant(self) -> bool:
        """Does the 95% interval exclude zero?"""
        if self.ci_low is None or self.ci_high is None:
            return False
        return (self.ci_low > 0) or (self.ci_high < 0)

    def as_dict(self) -> dict:
        return {"value": self.value, "std_error": self.std_error,
                "ci_low": self.ci_low, "ci_high": self.ci_high,
                "significant": self.significant}


@dataclass(slots=True)
class ExponentialFit:
    """fare(w) = L * (1 + a * exp(-w / tau))"""
    floor: Estimate           # L, the far-advance price level
    amplitude: Estimate       # a, the last-minute premium as a multiple of L
    decay_days: Estimate      # tau, days for the premium to fall by 1/e
    r_squared: float
    rmse: float
    n: int
    converged: bool
    note: str = ""

    @property
    def premium_at(self):
        """Fitted multiplier over the floor at a given booking window."""
        def f(window_days: float) -> float:
            return 1.0 + self.amplitude.value * math.exp(
                -window_days / self.decay_days.value)
        return f

    def as_dict(self) -> dict:
        return {
            "model": "exponential_decay",
            "form": "fare(w) = L * (1 + a * exp(-w / tau))",
            "floor": self.floor.as_dict(),
            "amplitude": self.amplitude.as_dict(),
            "decay_days": self.decay_days.as_dict(),
            "r_squared": self.r_squared,
            "rmse": self.rmse,
            "n": self.n,
            "converged": self.converged,
            "note": self.note,
        }


@dataclass(slots=True)
class LogLinearFit:
    """log(fare) = c + b * w. `b` is the fractional change per day of lead time."""
    intercept: Estimate
    slope: Estimate
    r_squared: float
    n: int

    @property
    def percent_per_day(self) -> float:
        """The slope as a percentage, which is how it should be quoted."""
        return self.slope.value * 100.0

    def as_dict(self) -> dict:
        return {
            "model": "log_linear",
            "form": "log(fare) = c + b * w",
            "intercept": self.intercept.as_dict(),
            "slope": self.slope.as_dict(),
            "percent_per_day": self.percent_per_day,
            "r_squared": self.r_squared,
            "n": self.n,
            "caveat": (
                "A deliberately misspecified summary. The true relationship is "
                "curved, so this averages a steep near-term effect together with "
                "a nearly flat far-term one. Use it as a single headline number, "
                "not as the model."),
        }


@dataclass(slots=True)
class RouteElasticity:
    route_code: str
    exponential: ExponentialFit | None
    log_linear: LogLinearFit | None
    n: int
    distinct_windows: int
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "route_code": self.route_code,
            "n_observations": self.n,
            "distinct_windows": self.distinct_windows,
            "exponential": self.exponential.as_dict() if self.exponential else None,
            "log_linear": self.log_linear.as_dict() if self.log_linear else None,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------

def _model(w, floor, amplitude, tau):
    import numpy as np
    return floor * (1.0 + amplitude * np.exp(-w / tau))


def fit_exponential(
    windows: Sequence[float],
    fares: Sequence[float],
) -> ExponentialFit:
    """Nonlinear least squares on the decay form.

    Starting values are derived from the data rather than hard-coded: the floor
    from the longest-window fares, the amplitude from the ratio of the shortest
    to the longest, and tau from the middle of the plausible range. A fit that
    depends on a lucky starting guess is not a fit.
    """
    import numpy as np
    from scipy.optimize import curve_fit

    w = np.asarray(windows, dtype=float)
    y = np.asarray(fares, dtype=float)
    if len(w) < MIN_OBSERVATIONS:
        raise NotIdentified(f"{len(w)} observations, need {MIN_OBSERVATIONS}")
    if len(set(windows)) < MIN_DISTINCT_WINDOWS:
        raise NotIdentified(
            f"{len(set(windows))} distinct windows, need {MIN_DISTINCT_WINDOWS}")

    w_max, w_min = w.max(), w.min()
    floor0 = float(np.median(y[w >= np.percentile(w, 75)])) or float(np.median(y))
    near = float(np.median(y[w <= np.percentile(w, 25)]))
    amp0 = max(0.05, (near / floor0) - 1.0) if floor0 else 1.0
    tau0 = max(2.0, (w_max - w_min) / 3.0)

    converged, note = True, ""
    try:
        popt, pcov = curve_fit(
            _model, w, y,
            p0=[floor0, amp0, tau0],
            bounds=([1.0, 0.0, 0.5], [np.inf, 20.0, 400.0]),
            maxfev=20000,
        )
    except Exception as exc:  # noqa: BLE001 - a failed fit is a result, not a crash
        raise NotIdentified(f"curve fitting did not converge: {exc}") from exc

    perr = np.sqrt(np.diag(pcov))
    if not np.all(np.isfinite(perr)):
        converged = False
        note = ("the covariance matrix is not finite, so the standard errors "
                "are unavailable; treat the point estimates as indicative only")
        perr = np.full(3, float("nan"))

    resid = y - _model(w, *popt)
    ss_res = float(np.sum(resid ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    rmse = float(np.sqrt(ss_res / len(y)))

    def est(i: int) -> Estimate:
        v, se = float(popt[i]), float(perr[i])
        if not math.isfinite(se):
            return Estimate(v, None, None, None)
        return Estimate(v, se, v - Z95 * se, v + Z95 * se)

    return ExponentialFit(
        floor=est(0), amplitude=est(1), decay_days=est(2),
        r_squared=r2, rmse=rmse, n=len(y), converged=converged, note=note)


def fit_log_linear(
    windows: Sequence[float],
    fares: Sequence[float],
) -> LogLinearFit:
    """OLS of log(fare) on the booking window."""
    import numpy as np
    import statsmodels.api as sm

    w = np.asarray(windows, dtype=float)
    y = np.asarray(fares, dtype=float)
    if len(w) < MIN_OBSERVATIONS:
        raise NotIdentified(f"{len(w)} observations, need {MIN_OBSERVATIONS}")
    if np.any(y <= 0):
        raise NotIdentified("non-positive fares cannot be logged")

    X = sm.add_constant(w)
    res = sm.OLS(np.log(y), X).fit()
    ci = res.conf_int(alpha=0.05)

    return LogLinearFit(
        intercept=Estimate(float(res.params[0]), float(res.bse[0]),
                           float(ci[0][0]), float(ci[0][1])),
        slope=Estimate(float(res.params[1]), float(res.bse[1]),
                       float(ci[1][0]), float(ci[1][1])),
        r_squared=float(res.rsquared),
        n=int(res.nobs),
    )


def fit_route(route_code: str, windows: Sequence[float],
              fares: Sequence[float]) -> RouteElasticity:
    """Fit both models for one route, degrading gracefully."""
    warnings: list[str] = []
    exponential = log_linear = None

    try:
        exponential = fit_exponential(windows, fares)
        if exponential.amplitude.ci_low is not None and exponential.amplitude.ci_low <= 0:
            warnings.append(
                "the amplitude's confidence interval includes zero: this route "
                "shows no statistically distinguishable last-minute premium")
    except NotIdentified as exc:
        warnings.append(f"exponential fit not identified: {exc}")

    try:
        log_linear = fit_log_linear(windows, fares)
    except NotIdentified as exc:
        warnings.append(f"log-linear fit not identified: {exc}")

    return RouteElasticity(
        route_code=route_code,
        exponential=exponential,
        log_linear=log_linear,
        n=len(fares),
        distinct_windows=len(set(windows)),
        warnings=warnings,
    )
