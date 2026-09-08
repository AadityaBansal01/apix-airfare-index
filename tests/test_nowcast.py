"""Nowcast tests.

Two things need guarding. The evaluation must never let a model see the value it
is being asked to predict -- a leak turns a forecasting result into a tautology
and is invisible in the output. And the recommendation must be willing to say
"nothing here is worth shipping", because a module that always finds a winner
is not evaluating anything.
"""
from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

import pytest

from apix.analytics.nowcast import (
    MODELS,
    SEASON,
    NowcastReport,
    Score,
    damped_trend,
    drift,
    evaluate,
    naive,
    rolling_origin,
    seasonal_damped_trend,
    seasonal_naive,
    weekly_factors,
)

START = dt.date(2026, 7, 1)


def series(values):
    return [(START + dt.timedelta(days=i), Decimal(str(v)))
            for i, v in enumerate(values)]


def wave(n=90, level=100.0, amp=3.0, slope=0.05, seed=11):
    """A trending series with a real weekly cycle and noise."""
    import random
    rng = random.Random(seed)
    return [level + slope * i + amp * math.sin(2 * math.pi * i / 7)
            + rng.uniform(-0.6, 0.6) for i in range(n)]


# ==========================================================================
# The forecasters
# ==========================================================================

def test_naive_carries_the_last_value_forward():
    assert naive([1.0, 2.0, 5.0], 3) == [5.0, 5.0, 5.0]


def test_seasonal_naive_repeats_the_last_week():
    h = list(range(14))            # 0..13
    assert seasonal_naive([float(x) for x in h], 3) == [7.0, 8.0, 9.0]


def test_seasonal_naive_at_the_season_length_equals_naive():
    """Not a bug: with m = h = 7, y(T+7) = y(T+7-7) = y(T), which is naive.
    Worth pinning, because seeing the two score identically at h=7 otherwise
    looks like a copy-paste error in the evaluation."""
    h = [float(x) for x in range(20)]
    assert seasonal_naive(h, 7)[-1] == naive(h, 7)[-1]


def test_seasonal_naive_falls_back_when_history_is_shorter_than_a_season():
    assert seasonal_naive([1.0, 2.0], 2) == [2.0, 2.0]


def test_drift_extrapolates_the_average_slope():
    assert drift([10.0, 11.0, 12.0], 2) == pytest.approx([13.0, 14.0])


def test_damped_trend_does_not_extrapolate_forever():
    """The damping is the point: an undamped linear trend on a price index
    forecasts it to the moon."""
    rising = [100.0 + 2 * i for i in range(30)]
    far = damped_trend(rising, 60)
    undamped = 100.0 + 2 * (29 + 60)
    assert far[-1] < undamped


def test_a_flat_series_is_forecast_flat_by_every_model():
    flat = [100.0] * 40
    for name, (fn, _) in MODELS.items():
        out = fn(flat, 5)
        assert all(abs(v - 100.0) < 1e-6 for v in out), f"{name} drifted off a flat series"


def test_weekly_factors_are_centred():
    """Factors reshape a week without shifting its level, so they must sum to
    zero. Otherwise deseasonalising would move the series."""
    f = weekly_factors(wave(60))
    assert sum(f) == pytest.approx(0.0, abs=1e-9)
    assert len(f) == SEASON


def test_weekly_factors_find_a_real_weekly_cycle():
    f = weekly_factors(wave(90, amp=5.0, slope=0.0))
    assert max(f) - min(f) > 4.0, f


def test_weekly_factors_are_flat_when_there_is_no_cycle():
    f = weekly_factors([100.0 + 0.1 * i for i in range(60)])
    assert max(abs(x) for x in f) < 0.5


# ==========================================================================
# The evaluation must not leak
# ==========================================================================

def test_rolling_origin_never_shows_a_model_its_target():
    """The failure that would silently invalidate everything else here."""
    seen: list[int] = []

    def spy(history, h):
        seen.append(len(history))
        return [history[-1]] * h

    values = [float(i) for i in range(60)]
    pairs = rolling_origin(values, spy, horizon=7, min_train=21)
    # Origin t sees values[:t] and is scored on values[t+6].
    for i, n in enumerate(seen):
        t = 21 + i
        assert n == t
        assert pairs[i][1] == values[t + 6]
        assert values[t + 6] not in values[:t]


def test_rolling_origin_produces_one_forecast_per_valid_origin():
    values = [float(i) for i in range(50)]
    assert len(rolling_origin(values, naive, 1, 21)) == 50 - 21
    assert len(rolling_origin(values, naive, 7, 21)) == 50 - 21 - 6


def test_a_perfect_forecaster_scores_zero_error():
    values = [float(i) for i in range(60)]
    pairs = rolling_origin(values, lambda h, n: [len(h) + i for i in range(n)], 1, 21)
    assert all(abs(p - a) < 1e-9 for p, a in pairs)


# ==========================================================================
# The recommendation must be willing to say no
# ==========================================================================

def test_a_random_walk_defeats_every_model_and_the_report_says_so():
    """On a random walk nothing can beat `naive`, by construction. A module that
    still found a winner here would be fitting noise."""
    import random
    rng = random.Random(5)
    walk, v = [], 100.0
    for _ in range(90):
        v += rng.gauss(0, 1.5)
        walk.append(v)
    rep = evaluate(series(walk))
    best_model = rep.best_model(1)
    assert best_model is not None
    assert not rep.recommendation.startswith("A nowcast is worth shipping.") or \
        rep.best_baseline(1).mase >= best_model.mase


def test_a_short_series_is_refused_rather_than_scored():
    rep = evaluate(series([100.0 + i for i in range(20)]))
    assert rep.scores == []
    assert "insufficient_history" in rep.warnings
    assert "No model is recommended" in rep.recommendation


def test_a_constant_series_is_refused():
    rep = evaluate(series([100.0] * 90))
    assert "degenerate_series" in rep.warnings
    assert "Nothing to nowcast" in rep.recommendation


def test_the_verdict_compares_against_the_best_baseline_not_against_mase_one():
    """Regression. The verdict column read MASE < 1 as 'beats baseline', which
    labelled a model that lost to `naive` as a winner: the MASE denominator is a
    fixed one-step scale, so at h=1 nearly everything scores under 1."""
    rep = NowcastReport(n_observations=90, season=7, min_train=21, scores=[
        Score("naive", 1, 40, 3.0, 5.0, 0.50, True),
        Score("damped_trend", 1, 40, 3.5, 5.2, 0.58, False),
    ])
    assert rep.verdict_for(rep.scores[1]).startswith("LOSES")
    assert rep.scores[1].beats_the_mase_unit          # < 1, and still a loser


def test_a_model_that_wins_on_mae_but_loses_on_rmse_is_not_recommended():
    """A heavier error tail at turning points is exactly when a nowcast is read.
    Winning on the metric that flatters is not a result."""
    from apix.analytics.nowcast import _recommend

    rep = NowcastReport(n_observations=90, season=7, min_train=21, scores=[
        Score("naive", 1, 45, 3.53, 5.45, 0.506, True),
        Score("seasonal_damped_trend", 1, 45, 3.15, 7.11, 0.451, False),
    ])
    text = _recommend(rep, [1])
    assert "RMSE is worse" in text
    assert text.startswith("NO NOWCAST MODEL IS RECOMMENDED")


def test_every_registered_model_is_scored_at_every_horizon():
    rep = evaluate(series(wave(90)), horizons=(1, 7))
    for h in (1, 7):
        assert {s.model for s in rep.scores if s.horizon == h} == set(MODELS)


def test_baselines_are_declared_in_one_place():
    """So that adding a model cannot quietly change what it is compared to."""
    assert {n for n, (_, b) in MODELS.items() if b} == {
        "naive", "seasonal_naive", "drift"}


# ==========================================================================
# Against the built index
# ==========================================================================

def _db() -> bool:
    try:
        from apix import db
        db.fetch_all("SELECT 1 AS x")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _db(), reason="postgres not reachable; run `make demo`")
def test_the_built_index_produces_a_verdict_either_way():
    from apix import db

    rows = db.fetch_all("SELECT index_date, index_value FROM apix.apix_index "
                        "WHERE frequency='daily' ORDER BY index_date")
    if len(rows) < 40:
        pytest.skip("no built index; run `make demo`")
    rep = evaluate([(r["index_date"], r["index_value"]) for r in rows])
    assert rep.scores
    assert rep.recommendation
    # Whatever the answer, it must be stated as one of the three verdicts.
    assert any(rep.recommendation.startswith(p) for p in (
        "A nowcast is worth shipping.",
        "A nowcast is worth shipping at some horizons only.",
        "NO NOWCAST MODEL IS RECOMMENDED."))
