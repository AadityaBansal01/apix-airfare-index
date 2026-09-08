"""Lead-time elasticity tests.

The load-bearing test is parameter recovery. `apix/seed.py` generates fares from
a known amplitude and decay constant per route, so the fitted model can be
checked against ground truth rather than against its own output. A model that
cannot recover parameters it was handed has no business reporting confidence
intervals on real data.
"""
from __future__ import annotations

import datetime as dt
import math
import random

import pytest

from apix.analytics.elasticity import (
    MIN_DISTINCT_WINDOWS,
    MIN_OBSERVATIONS,
    NotIdentified,
    fit_exponential,
    fit_log_linear,
    fit_route,
)
from apix.seed import DEFAULT_LEADTIME, ROUTE_LEADTIME, leadtime_factor

WINDOWS = [1, 7, 15, 30, 45]


def synth(a: float, tau: float, floor: float = 5000.0, noise: float = 0.0,
          seed: int = 7, per_window: int = 40):
    """Fares from the known generating process, optionally with noise."""
    rng = random.Random(seed)
    w, y = [], []
    for window in WINDOWS:
        for _ in range(per_window):
            level = floor * (1.0 + a * math.exp(-window / tau))
            jitter = rng.uniform(1 - noise, 1 + noise) if noise else 1.0
            w.append(float(window))
            y.append(level * jitter)
    return w, y


# ==========================================================================
# Parameter recovery: the test that justifies the whole module
# ==========================================================================

def test_recovers_parameters_from_clean_data():
    w, y = synth(a=1.42, tau=10.0, floor=4000.0)
    fit = fit_exponential(w, y)
    assert fit.amplitude.value == pytest.approx(1.42, rel=0.02)
    assert fit.decay_days.value == pytest.approx(10.0, rel=0.05)
    assert fit.floor.value == pytest.approx(4000.0, rel=0.02)
    assert fit.r_squared > 0.999


@pytest.mark.parametrize("route,params", sorted(ROUTE_LEADTIME.items()))
def test_recovers_every_route_the_generator_defines(route, params):
    """Every route in apix/seed.py, recovered within tolerance under noise.

    If this fails after someone edits the generator or the fitter, one of the
    two no longer describes the other."""
    a, tau = params
    w, y = synth(a=a, tau=tau, floor=4200.0, noise=0.06, seed=hash(route) % 9973)
    fit = fit_exponential(w, y)
    assert fit.amplitude.value == pytest.approx(a, rel=0.15), route
    assert fit.decay_days.value == pytest.approx(tau, rel=0.25), route


def test_recovery_survives_realistic_noise():
    w, y = synth(a=1.30, tau=11.0, noise=0.12, seed=99)
    fit = fit_exponential(w, y)
    assert fit.amplitude.value == pytest.approx(1.30, rel=0.15)
    assert fit.decay_days.value == pytest.approx(11.0, rel=0.25)


def test_the_true_parameters_lie_inside_the_confidence_intervals():
    """An interval that excludes the truth is worse than no interval."""
    a, tau = 1.18, 12.0
    w, y = synth(a=a, tau=tau, noise=0.08, seed=4242)
    fit = fit_exponential(w, y)
    assert fit.amplitude.ci_low <= a <= fit.amplitude.ci_high
    assert fit.decay_days.ci_low <= tau <= fit.decay_days.ci_high


def test_fit_agrees_with_the_generator_s_own_factor_function():
    """The fitted curve must reproduce apix.seed.leadtime_factor, which is what
    the dashboard's lead-time view is claiming to have recovered."""
    route = ("BOM", "DEL")
    a, tau = ROUTE_LEADTIME[route]
    w, y = synth(a=a, tau=tau, floor=3900.0)
    fit = fit_exponential(w, y)
    for window in WINDOWS:
        assert fit.premium_at(window) == pytest.approx(
            leadtime_factor(window, route), rel=0.03), window


# ==========================================================================
# The curve is a curve, and a single elasticity hides that
# ==========================================================================

def test_steeper_routes_fit_a_larger_amplitude():
    """Business-heavy trunk sectors price late booking harder. The fit must
    preserve the ordering the generator encodes, or the dashboard's per-route
    comparison is meaningless."""
    steep_a, steep_tau = ROUTE_LEADTIME[("BOM", "DEL")]
    flat_a, flat_tau = ROUTE_LEADTIME[("BLR", "HYD")]
    steep = fit_exponential(*synth(a=steep_a, tau=steep_tau, noise=0.05, seed=1))
    flat = fit_exponential(*synth(a=flat_a, tau=flat_tau, noise=0.05, seed=2))
    assert steep.amplitude.value > flat.amplitude.value


def test_log_linear_slope_is_negative_and_reported_as_a_percentage():
    w, y = synth(a=1.30, tau=11.0, noise=0.05)
    fit = fit_log_linear(w, y)
    assert fit.slope.value < 0, "fares fall as the booking horizon lengthens"
    assert fit.percent_per_day == pytest.approx(fit.slope.value * 100)
    assert fit.slope.significant


def test_log_linear_fits_worse_than_the_curve_it_approximates():
    """Documents why both models are reported. The log-linear form averages a
    steep near-term effect with a flat far-term one, so it must fit less well.
    If it ever fits better, the exponential is misspecified and this module's
    primary model is wrong."""
    w, y = synth(a=1.42, tau=10.0)
    assert fit_log_linear(w, y).r_squared < fit_exponential(w, y).r_squared


# ==========================================================================
# Refusing to fit
# ==========================================================================

def test_refuses_too_few_observations():
    with pytest.raises(NotIdentified, match="observations"):
        fit_exponential([1, 7, 15, 30], [9000, 7000, 6000, 5000])


def test_refuses_too_few_distinct_windows():
    """Two points can be joined by infinitely many exponentials."""
    w = [1.0] * 20 + [45.0] * 20
    y = [9000.0] * 20 + [5000.0] * 20
    with pytest.raises(NotIdentified, match="distinct windows"):
        fit_exponential(w, y)


def test_log_linear_refuses_non_positive_fares():
    w, y = synth(a=1.0, tau=10.0)
    y[0] = 0.0
    with pytest.raises(NotIdentified, match="non-positive"):
        fit_log_linear(w, y)


def test_minimums_are_stated_not_magic():
    assert MIN_OBSERVATIONS >= 12
    assert MIN_DISTINCT_WINDOWS >= 4


# ==========================================================================
# fit_route degrades rather than crashing
# ==========================================================================

def test_fit_route_returns_both_models():
    w, y = synth(a=1.05, tau=13.5, noise=0.05)
    r = fit_route("CCU-DEL", w, y)
    assert r.exponential is not None and r.log_linear is not None
    assert r.distinct_windows == len(WINDOWS)
    assert not r.warnings


def test_fit_route_records_a_warning_instead_of_raising():
    r = fit_route("XXX-YYY", [1, 7, 15], [9000, 7000, 6000])
    assert r.exponential is None
    assert r.warnings and "not identified" in r.warnings[0]


def test_flat_route_is_flagged_as_having_no_premium():
    """A route with no last-minute effect must say so rather than report a
    meaningless amplitude."""
    w, y = synth(a=0.0, tau=10.0, noise=0.02, seed=11)
    r = fit_route("FLAT-ONE", w, y)
    assert any("no statistically distinguishable" in x for x in r.warnings)


def test_serialises_with_uncertainty_intact():
    w, y = synth(a=1.30, tau=11.0, noise=0.05)
    d = fit_route("BLR-DEL", w, y).as_dict()
    assert d["exponential"]["amplitude"]["ci_low"] is not None
    assert d["exponential"]["form"].startswith("fare(w)")
    assert "misspecified" in d["log_linear"]["caveat"]
