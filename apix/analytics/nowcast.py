"""Short-horizon nowcasting of the headline index.

WHAT THIS IS FOR
----------------
A daily index is published with a lag: collection runs at 02:00, cleaning and
aggregation after it, and a user asking "where is the index today" before that
has finished gets yesterday's number. A nowcast fills that gap, and a one-week
horizon answers the adjacent question a ministry actually asks -- is the current
move a blip or the start of something.

THE RULE THIS MODULE EXISTS TO ENFORCE
--------------------------------------
**A forecast that does not beat the naive baseline must be reported as useless
and withdrawn.** Forecasting literature is full of models that look sophisticated
and lose to "tomorrow will be like today"; price series in particular are close
to random walks, and beating a random walk on a 66-observation daily series is
genuinely hard. `evaluate()` therefore returns the baselines and the models on
the same footing and `NowcastReport.recommendation` says, in words, whether
anything earned its place. If nothing did, that is the finding, and this module
prints it rather than shipping a chart of a losing model.

THE BASELINES ARE NOT STRAW MEN
-------------------------------
Three of them, because each is hard to beat for a different reason:

*   `naive` -- tomorrow equals today. The random-walk baseline. If a price series
    is efficient in even a weak sense, nothing beats this at h=1.
*   `seasonal_naive` -- tomorrow equals the same weekday last week. Airfares have
    a real weekly cycle (Tuesday and Wednesday departures price differently from
    Friday and Sunday), so this is the right baseline at h=7.
*   `drift` -- the random walk with the average historical slope. Catches a
    trending series that `naive` lags.

THE MODELS
----------
*   `damped_trend` -- Holt's linear method with a damping parameter, so the trend
    flattens rather than extrapolating forever. Three parameters, grid-fitted on
    one-step squared error. Deliberately small: with 66 observations, anything
    larger fits the noise.
*   `seasonal_damped_trend` -- the same, applied to a series with additive weekly
    factors removed and added back. This is the only model here that can use both
    the trend and the weekly cycle.

EVALUATION IS ROLLING-ORIGIN, NOT A SINGLE SPLIT
-------------------------------------------------
One train/test split on a 66-point series measures luck. Every origin from
`min_train` onward produces a forecast and is scored, so the numbers are means
over dozens of forecasts rather than one. Each origin fits on data strictly
before it: no model ever sees a value it is asked to predict.

SCORED IN MASE
--------------
Mean absolute scaled error, scaled by the in-sample one-step seasonal-naive MAE.
MASE < 1 means "better than the seasonal naive forecast"; > 1 means worse. It is
scale-free, so it is comparable across horizons and across a rebased series,
which raw RMSE is not.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Sequence

#: The weekly cycle. Airfares differ by day of week; a 7-day season is the only
#: seasonality a 66-day series can support.
SEASON = 7

#: Origins before this are refused: a model fitted on ten points and scored on
#: the eleventh is measuring its own initialisation.
MIN_TRAIN = 21

#: Horizons reported. h=1 is the publication-lag nowcast; h=7 is the "is this a
#: blip" question.
HORIZONS = (1, 7)


# ---------------------------------------------------------------------------
# Forecasters. Each takes a history and a horizon, returns h predictions.
# ---------------------------------------------------------------------------

def naive(history: Sequence[float], h: int) -> list[float]:
    """Tomorrow equals today. The random walk."""
    return [history[-1]] * h


def seasonal_naive(history: Sequence[float], h: int, season: int = SEASON) -> list[float]:
    """Tomorrow equals the same weekday last week."""
    if len(history) < season:
        return naive(history, h)
    return [history[-season + (i % season)] for i in range(h)]


def drift(history: Sequence[float], h: int) -> list[float]:
    """Random walk with the average slope over the whole history."""
    if len(history) < 2:
        return naive(history, h)
    slope = (history[-1] - history[0]) / (len(history) - 1)
    return [history[-1] + slope * (i + 1) for i in range(h)]


def _holt_damped(history: Sequence[float], h: int,
                 alpha: float, beta: float, phi: float) -> tuple[list[float], float]:
    """One pass of damped Holt. Returns (forecast, one-step SSE)."""
    if len(history) < 3:
        return naive(history, h), math.inf
    level = history[0]
    trend = history[1] - history[0]
    sse = 0.0
    for y in history[1:]:
        pred = level + phi * trend
        sse += (y - pred) ** 2
        new_level = alpha * y + (1 - alpha) * pred
        trend = beta * (new_level - level) + (1 - beta) * phi * trend
        level = new_level
    out, damp = [], 0.0
    for i in range(1, h + 1):
        damp += phi ** i
        out.append(level + damp * trend)
    return out, sse


#: Coarse grid. Fine enough to find a sensible region, coarse enough that the
#: fit cannot chase noise into a corner of the parameter space.
_GRID_ALPHA = (0.1, 0.2, 0.3, 0.5, 0.7, 0.9)
_GRID_BETA = (0.02, 0.05, 0.1, 0.2, 0.4)
_GRID_PHI = (0.80, 0.90, 0.95, 0.98)


def damped_trend(history: Sequence[float], h: int) -> list[float]:
    """Holt's linear method with damping, parameters grid-fitted on one-step SSE."""
    best, best_sse = None, math.inf
    for a in _GRID_ALPHA:
        for b in _GRID_BETA:
            for p in _GRID_PHI:
                fc, sse = _holt_damped(history, h, a, b, p)
                if sse < best_sse:
                    best, best_sse = fc, sse
    return best if best is not None else naive(history, h)


def weekly_factors(history: Sequence[float], season: int = SEASON) -> list[float]:
    """Additive weekly factors, as mean deviation from a centred moving average.

    Returns `season` values indexed by position modulo the season, centred to
    sum to zero so the factors shift the shape of a week without shifting its
    level.
    """
    n = len(history)
    if n < 2 * season:
        return [0.0] * season
    half = season // 2
    devs: dict[int, list[float]] = {i: [] for i in range(season)}
    for t in range(half, n - half):
        window = history[t - half: t + half + 1]
        devs[t % season].append(history[t] - sum(window) / len(window))
    factors = [statistics.fmean(devs[i]) if devs[i] else 0.0 for i in range(season)]
    mean = statistics.fmean(factors)
    return [f - mean for f in factors]


def seasonal_damped_trend(history: Sequence[float], h: int,
                          season: int = SEASON) -> list[float]:
    """Remove the weekly shape, fit a damped trend, put the shape back."""
    if len(history) < 2 * season:
        return damped_trend(history, h)
    factors = weekly_factors(history, season)
    deseasonalised = [y - factors[i % season] for i, y in enumerate(history)]
    core = damped_trend(deseasonalised, h)
    n = len(history)
    return [v + factors[(n + i) % season] for i, v in enumerate(core)]


#: name -> (callable, is_baseline). Baselines are what a model has to beat; they
#: are listed here rather than hard-coded in the report so adding a model cannot
#: quietly change what it is compared against.
MODELS: dict[str, tuple[Callable[[Sequence[float], int], list[float]], bool]] = {
    "naive": (naive, True),
    "seasonal_naive": (seasonal_naive, True),
    "drift": (drift, True),
    "damped_trend": (damped_trend, False),
    "seasonal_damped_trend": (seasonal_damped_trend, False),
}


# ---------------------------------------------------------------------------
# Rolling-origin evaluation
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Score:
    model: str
    horizon: int
    n_forecasts: int
    mae: float
    rmse: float
    mase: float
    is_baseline: bool

    @property
    def beats_the_mase_unit(self) -> bool:
        """MASE < 1: better than the IN-SAMPLE one-step seasonal-naive error.

        Deliberately not called "beats the baseline". The MASE denominator is a
        fixed one-step scale, so at h=1 almost everything scores under 1 and at
        h=7 almost nothing does, regardless of merit. Whether a model is
        actually worth having is `NowcastReport.verdict_for`, which compares it
        against the best baseline AT THE SAME HORIZON. Conflating the two is how
        a losing model acquires a tick in a results table.
        """
        return self.mase < 1.0

    def as_dict(self) -> dict:
        return {
            "model": self.model, "horizon": self.horizon,
            "n_forecasts": self.n_forecasts,
            "mae": round(self.mae, 4), "rmse": round(self.rmse, 4),
            "mase": round(self.mase, 4), "is_baseline": self.is_baseline,
        }


@dataclass(slots=True)
class NowcastReport:
    n_observations: int
    season: int
    min_train: int
    scores: list[Score] = field(default_factory=list)
    recommendation: str = ""
    warnings: list[str] = field(default_factory=list)

    def best(self, horizon: int) -> Score | None:
        candidates = [s for s in self.scores if s.horizon == horizon]
        return min(candidates, key=lambda s: s.mase) if candidates else None

    def best_model(self, horizon: int) -> Score | None:
        """Best NON-baseline. The thing that has to justify itself."""
        candidates = [s for s in self.scores
                      if s.horizon == horizon and not s.is_baseline]
        return min(candidates, key=lambda s: s.mase) if candidates else None

    def as_dict(self) -> dict:
        return {
            "n_observations": self.n_observations,
            "season": self.season,
            "min_train": self.min_train,
            "scores": [s.as_dict() for s in self.scores],
            "recommendation": self.recommendation,
            "warnings": list(self.warnings),
        }

    def best_baseline(self, horizon: int) -> Score | None:
        candidates = [s for s in self.scores
                      if s.horizon == horizon and s.is_baseline]
        return min(candidates, key=lambda s: s.mase) if candidates else None

    def verdict_for(self, score: Score) -> str:
        """Whether this model earned its place, against the bar it must clear.

        The bar is the BEST baseline at the same horizon, not MASE 1.0. A model
        that loses to `naive` has not earned anything, however good its MASE
        looks next to a fixed scale.
        """
        if score.is_baseline:
            best = self.best_baseline(score.horizon)
            return "baseline (best)" if best and best.model == score.model else "baseline"
        bar = self.best_baseline(score.horizon)
        if bar is None:
            return "no baseline to compare"
        margin = (bar.mase - score.mase) / bar.mase
        if margin > 0.05:
            return f"BEATS {bar.model} by {margin:.0%}"
        if margin > 0:
            return f"ties {bar.model} (+{margin:.0%}, inside noise)"
        return f"LOSES to {bar.model} by {-margin:.0%}"

    def table(self) -> str:
        lines = [f"{'model':<24}{'h':>3}{'n':>5}{'MAE':>9}{'RMSE':>9}{'MASE':>8}"
                 f"  verdict vs best baseline at this horizon"]
        lines.append("-" * 96)
        for s in sorted(self.scores, key=lambda s: (s.horizon, s.mase)):
            lines.append(f"{s.model:<24}{s.horizon:>3}{s.n_forecasts:>5}"
                         f"{s.mae:>9.4f}{s.rmse:>9.4f}{s.mase:>8.3f}"
                         f"  {self.verdict_for(s)}")
        return "\n".join(lines)


def _in_sample_seasonal_mae(values: Sequence[float], season: int) -> float:
    """The MASE denominator: one-step seasonal-naive error on the same data."""
    errs = [abs(values[t] - values[t - season]) for t in range(season, len(values))]
    return statistics.fmean(errs) if errs else 0.0


def rolling_origin(values: Sequence[float], model, horizon: int,
                   min_train: int = MIN_TRAIN) -> list[tuple[float, float]]:
    """(predicted, actual) at `horizon` steps ahead, for every valid origin.

    The model sees `values[:t]` and is asked for `values[t + horizon - 1]`. It
    never sees the target, and it never sees anything after the target either,
    so a forecast made here is one that could have been made on the day.
    """
    out: list[tuple[float, float]] = []
    for t in range(min_train, len(values) - horizon + 1):
        history = values[:t]
        pred = model(history, horizon)
        out.append((pred[horizon - 1], values[t + horizon - 1]))
    return out


def evaluate(series: Sequence[tuple[dt.date, Decimal | float]],
             *, horizons: Sequence[int] = HORIZONS, season: int = SEASON,
             min_train: int = MIN_TRAIN) -> NowcastReport:
    """Score every model at every horizon by rolling origin."""
    values = [float(v) for _, v in series]
    report = NowcastReport(n_observations=len(values), season=season,
                           min_train=min_train)

    if len(values) < min_train + max(horizons) + season:
        report.recommendation = (
            f"Not enough history to evaluate: {len(values)} observations, and a "
            f"credible rolling-origin test at horizon {max(horizons)} needs at "
            f"least {min_train + max(horizons) + season}. No model is recommended, "
            f"because none has been tested.")
        report.warnings.append("insufficient_history")
        return report

    scale = _in_sample_seasonal_mae(values, season)
    if scale <= 0:
        report.recommendation = (
            "The series has no seasonal-naive error to scale against, which means "
            "it is constant. Nothing to nowcast.")
        report.warnings.append("degenerate_series")
        return report

    for h in horizons:
        for name, (fn, is_baseline) in MODELS.items():
            pairs = rolling_origin(values, fn, h, min_train)
            if not pairs:
                continue
            errs = [p - a for p, a in pairs]
            report.scores.append(Score(
                model=name, horizon=h, n_forecasts=len(pairs),
                mae=statistics.fmean(abs(e) for e in errs),
                rmse=math.sqrt(statistics.fmean(e * e for e in errs)),
                mase=statistics.fmean(abs(e) for e in errs) / scale,
                is_baseline=is_baseline,
            ))

    report.recommendation = _recommend(report, horizons)
    return report


def _recommend(report: NowcastReport, horizons: Sequence[int]) -> str:
    """State plainly whether any model earned its place.

    Written to be quotable in a report and to be unflattering when that is the
    truth. A margin under 5% is treated as no improvement: on a few dozen
    forecasts, that is inside the noise, and shipping a model on it would be
    reading a coin flip as a finding.
    """
    verdicts = []
    kept = 0
    for h in horizons:
        model = report.best_model(h)
        best_baseline = report.best_baseline(h)
        if not model or not best_baseline:
            continue
        margin = (best_baseline.mase - model.mase) / best_baseline.mase
        if margin > 0.05:
            kept += 1
            claim = (
                f"At h={h}, {model.model} (MASE {model.mase:.3f}) beats the best "
                f"baseline {best_baseline.model} (MASE {best_baseline.mase:.3f}) "
                f"by {margin:.1%} over {model.n_forecasts} rolling-origin "
                f"forecasts.")
            # MAE and RMSE can disagree, and when they do it is not a detail.
            # A model that wins on mean absolute error while losing on root mean
            # squared error is usually closer but occasionally much further out:
            # a heavier error tail, typically at a turning point, which is
            # exactly where a nowcast is read most closely. Publishing the win
            # without the tail would be selecting the metric that flatters.
            if model.rmse > best_baseline.rmse:
                kept -= 1
                claim += (
                    f" BUT its RMSE is worse ({model.rmse:.2f} against "
                    f"{best_baseline.rmse:.2f}), so it is usually closer and "
                    f"occasionally much further out — a heavier error tail, and "
                    f"the tail falls at turning points, which is when anyone "
                    f"actually reads a nowcast. Not recommended for publication "
                    f"on a {model.n_forecasts}-forecast sample; report the "
                    f"baseline and keep the model under review.")
            else:
                claim += " Worth publishing."
            verdicts.append(claim)
        elif margin > 0:
            verdicts.append(
                f"At h={h}, {model.model} (MASE {model.mase:.3f}) edges the best "
                f"baseline {best_baseline.model} ({best_baseline.mase:.3f}) by "
                f"only {margin:.1%}. On {model.n_forecasts} forecasts that is "
                f"inside the noise and is not a result. Use the baseline.")
        else:
            verdicts.append(
                f"At h={h}, no model beats the baseline: best model "
                f"{model.model} scores MASE {model.mase:.3f} against "
                f"{best_baseline.model} at {best_baseline.mase:.3f}. The extra "
                f"machinery buys nothing. Use {best_baseline.model} and say so.")

    head = ("A nowcast is worth shipping." if kept == len(horizons) else
            "A nowcast is worth shipping at some horizons only." if kept else
            "NO NOWCAST MODEL IS RECOMMENDED. Publish the baseline instead.")
    return head + " " + " ".join(verdicts)
