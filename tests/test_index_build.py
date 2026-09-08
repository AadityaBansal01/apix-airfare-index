"""Index engine tests: base prices, missing-data policy, and aggregation.

The missing-data policy is the part a price statistician will interrogate first,
because carrying a stale value forward too long turns a gap into a fabricated
observation. Every branch of METHODOLOGY.md section 5 is pinned here.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apix.index.aggregate import NoObservedCells, cell_weights
from apix.index.build import (
    MAX_CARRY_FORWARD_DAYS,
    aggregate_period,
    build_base_prices,
    build_elementary,
    build_index,
    consecutive_imputed_before,
    month_start,
    observed_passenger_share,
    week_start,
)

D = Decimal
DAY = dt.date(2026, 9, 4)

BLR_DEL, BOM_DEL = 1, 2
CELLS = [(BLR_DEL, 7), (BLR_DEL, 30), (BOM_DEL, 7), (BOM_DEL, 30)]
ROUTE_W = {BLR_DEL: D("0.565330"), BOM_DEL: D("0.434670")}
WINDOW_W = {7: D("0.5"), 30: D("0.5")}
WEIGHTS = cell_weights(ROUTE_W, WINDOW_W)

# Base prices matching the worked example in METHODOLOGY.md section 6.
BASE = {
    (BLR_DEL, 7, "QP"): D("5200"), (BLR_DEL, 7, "SG"): D("4900"),
    (BLR_DEL, 30, "QP"): D("4100"), (BLR_DEL, 30, "SG"): D("3800"),
    (BOM_DEL, 7, "QP"): D("4800"), (BOM_DEL, 7, "SG"): D("4500"),
    (BOM_DEL, 30, "QP"): D("3700"), (BOM_DEL, 30, "SG"): D("3500"),
}
DAY1_PRICES = {
    (BLR_DEL, 7): {"QP": D("5980"), "SG": D("5390")},
    (BLR_DEL, 30): {"QP": D("4305"), "SG": D("3914")},
    (BOM_DEL, 7): {"QP": D("5760"), "SG": D("5040")},
    (BOM_DEL, 30): {"QP": D("3811"), "SG": D("3605")},
}


def approx(a, b, tol="0.0001"):
    return abs(Decimal(str(a)) - Decimal(str(b))) < Decimal(tol)


# ==========================================================================
# Base prices
# ==========================================================================

def test_base_price_is_geometric_mean():
    base = build_base_prices({(1, 7, "QP"): [D("100"), D("400")]})
    assert approx(base[(1, 7, "QP")], "200")


def test_base_price_skips_cells_with_too_few_observations():
    """A base price is the denominator of every future value for that cell, so a
    noisy one contaminates the whole series rather than one day."""
    base = build_base_prices(
        {(1, 7, "QP"): [D("5000")], (1, 7, "SG"): [D("4000")] * 20},
        min_observations=10)
    assert (1, 7, "QP") not in base
    assert (1, 7, "SG") in base


def test_base_price_ignores_non_positive_values():
    base = build_base_prices({(1, 7, "QP"): [D("100"), D("0"), D("400")]})
    assert approx(base[(1, 7, "QP")], "200")


def test_index_is_100_when_current_equals_base():
    """The defining property of the whole construction."""
    prices = {
        (r, w): {c: BASE[(r, w, c)] for c in ("QP", "SG")}
        for (r, w) in CELLS
    }
    elem = build_elementary(DAY, prices, BASE, CELLS)
    for o in elem.values():
        assert approx(o.index_value, "100")
    build = build_index(DAY, elem, WEIGHTS, ROUTE_W)
    assert approx(build.index_value, "100")


# ==========================================================================
# The worked example, driven through the real engine
# ==========================================================================

def test_worked_example_reproduces_through_the_engine():
    elem = build_elementary(DAY, DAY1_PRICES, BASE, CELLS)
    assert approx(elem[(BLR_DEL, 7)].index_value, "112.4722")
    assert approx(elem[(BOM_DEL, 7)].index_value, "115.9310")

    build = build_index(DAY, elem, WEIGHTS, ROUTE_W)
    assert approx(build.index_value, "108.7691")
    assert build.n_cells == 4
    assert approx(build.coverage_ratio, "1.0")
    assert build.imputed_cells == 0
    assert build.dropped_cells == 0


def test_route_breakdown_is_internally_consistent():
    elem = build_elementary(DAY, DAY1_PRICES, BASE, CELLS)
    build = build_index(DAY, elem, WEIGHTS, ROUTE_W)
    assert set(build.route_values) == {BLR_DEL, BOM_DEL}
    # Contributions reconstruct the headline.
    assert approx(sum(build.route_contributions.values()), build.index_value)


# ==========================================================================
# Missing data: carrier level
# ==========================================================================

def test_missing_carrier_uses_the_others_without_imputation():
    """Jevons is self-renormalising over observed carriers, so a missing carrier
    needs no imputation at all."""
    prices = dict(DAY1_PRICES)
    prices[(BLR_DEL, 7)] = {"QP": D("5980")}       # SG absent
    elem = build_elementary(DAY, prices, BASE, CELLS)
    o = elem[(BLR_DEL, 7)]
    assert o.carriers_used == ("QP",)
    assert o.is_imputed is False
    assert approx(o.index_value, "115.0")           # 5980/5200 = 1.15


def test_carrier_with_no_base_price_is_ignored():
    prices = dict(DAY1_PRICES)
    prices[(BLR_DEL, 7)] = {"QP": D("5980"), "6E": D("9999")}
    elem = build_elementary(DAY, prices, BASE, CELLS)
    assert elem[(BLR_DEL, 7)].carriers_used == ("QP",)


def test_cell_whose_carriers_all_lack_base_prices_is_treated_as_missing():
    prices = dict(DAY1_PRICES)
    prices[(BLR_DEL, 7)] = {"6E": D("9999")}
    elem = build_elementary(DAY, prices, BASE, CELLS,
                            last_known={(BLR_DEL, 7): D("110")})
    assert elem[(BLR_DEL, 7)].is_imputed is True


# ==========================================================================
# Missing data: cell level, the carry-forward policy
# ==========================================================================

def test_first_missing_day_carries_forward():
    prices = {c: v for c, v in DAY1_PRICES.items() if c != (BLR_DEL, 7)}
    elem = build_elementary(DAY, prices, BASE, CELLS,
                            last_known={(BLR_DEL, 7): D("112.4722")})
    o = elem[(BLR_DEL, 7)]
    assert o.is_imputed is True
    assert approx(o.index_value, "112.4722")
    assert "carried forward" in o.imputation_note


def test_third_consecutive_missing_day_is_dropped():
    """Two carry-forwards are policy; a third would be inventing data."""
    history = {(BLR_DEL, 7): {
        DAY - dt.timedelta(days=1): True,
        DAY - dt.timedelta(days=2): True,
    }}
    prices = {c: v for c, v in DAY1_PRICES.items() if c != (BLR_DEL, 7)}
    elem = build_elementary(DAY, prices, BASE, CELLS,
                            last_known={(BLR_DEL, 7): D("112.4722")},
                            imputed_history=history)
    o = elem[(BLR_DEL, 7)]
    assert o.dropped is True
    assert o.index_value is None
    assert "carry-forward limit" in o.drop_reason


def test_second_consecutive_missing_day_still_carries_forward():
    history = {(BLR_DEL, 7): {DAY - dt.timedelta(days=1): True}}
    prices = {c: v for c, v in DAY1_PRICES.items() if c != (BLR_DEL, 7)}
    elem = build_elementary(DAY, prices, BASE, CELLS,
                            last_known={(BLR_DEL, 7): D("112.4722")},
                            imputed_history=history)
    assert elem[(BLR_DEL, 7)].is_imputed is True


def test_streak_resets_after_a_genuine_observation():
    """A real observation between gaps restores the full carry-forward budget."""
    history = {(BLR_DEL, 7): {
        DAY - dt.timedelta(days=1): False,   # observed
        DAY - dt.timedelta(days=2): True,
        DAY - dt.timedelta(days=3): True,
    }}
    assert consecutive_imputed_before(history, (BLR_DEL, 7), DAY) == 0
    prices = {c: v for c, v in DAY1_PRICES.items() if c != (BLR_DEL, 7)}
    elem = build_elementary(DAY, prices, BASE, CELLS,
                            last_known={(BLR_DEL, 7): D("112.4722")},
                            imputed_history=history)
    assert elem[(BLR_DEL, 7)].is_imputed is True


def test_missing_cell_with_no_prior_value_is_dropped():
    prices = {c: v for c, v in DAY1_PRICES.items() if c != (BLR_DEL, 7)}
    elem = build_elementary(DAY, prices, BASE, CELLS)
    o = elem[(BLR_DEL, 7)]
    assert o.dropped is True
    assert "no prior value" in o.drop_reason


def test_dropped_cell_lowers_coverage_and_renormalises():
    prices = {c: v for c, v in DAY1_PRICES.items() if c != (BOM_DEL, 30)}
    elem = build_elementary(DAY, prices, BASE, CELLS)
    build = build_index(DAY, elem, WEIGHTS, ROUTE_W)
    assert build.n_cells == 3
    assert build.coverage_ratio < 1
    assert approx(build.coverage_ratio,
                  str(1 - float(WEIGHTS[(BOM_DEL, 30)])), "0.0001")
    # Dropping the cheapest cell must raise the headline, not leave it unchanged.
    full = build_index(DAY, build_elementary(DAY, DAY1_PRICES, BASE, CELLS),
                       WEIGHTS, ROUTE_W)
    assert build.index_value > full.index_value


def test_everything_missing_raises_rather_than_publishing_a_number():
    elem = build_elementary(DAY, {}, BASE, CELLS)
    with pytest.raises(NoObservedCells):
        build_index(DAY, elem, WEIGHTS, ROUTE_W)


def test_carry_forward_limit_constant_matches_methodology():
    """METHODOLOGY.md section 5 says fewer than three consecutive days."""
    assert MAX_CARRY_FORWARD_DAYS == 2


# ==========================================================================
# Observed passenger share
# ==========================================================================

CARRIER_SHARE = {
    "6E": D("0.6389"), "AI": D("0.1516"), "IX": D("0.1150"),
    "QP": D("0.0526"), "SG": D("0.0303"),
}


def test_observed_share_reflects_only_carriers_actually_seen():
    elem = build_elementary(DAY, DAY1_PRICES, BASE, CELLS)
    share = observed_passenger_share(elem, ROUTE_W, CARRIER_SHARE)
    assert approx(share, "0.0829", "0.0001")


def test_observed_share_falls_when_a_carrier_disappears():
    prices = {c: {"QP": v["QP"]} for c, v in DAY1_PRICES.items()}
    elem = build_elementary(DAY, prices, BASE, CELLS)
    share = observed_passenger_share(elem, ROUTE_W, CARRIER_SHARE)
    assert approx(share, "0.0526", "0.0001")


def test_imputed_cells_do_not_count_as_observed_traffic():
    """A carried-forward value is not an observation of the market."""
    prices = {c: v for c, v in DAY1_PRICES.items() if c[0] != BOM_DEL}
    elem = build_elementary(DAY, prices, BASE, CELLS,
                            last_known={c: D("100") for c in CELLS})
    share = observed_passenger_share(elem, ROUTE_W, CARRIER_SHARE)
    # Only BLR-DEL genuinely observed, so the share is computed over that route.
    assert approx(share, "0.0829", "0.0001")
    assert any(o.is_imputed for o in elem.values())


def test_observed_share_is_none_without_carrier_data():
    elem = build_elementary(DAY, DAY1_PRICES, BASE, CELLS)
    assert observed_passenger_share(elem, ROUTE_W, {}) is None


def test_headline_carries_the_observed_share():
    elem = build_elementary(DAY, DAY1_PRICES, BASE, CELLS)
    build = build_index(DAY, elem, WEIGHTS, ROUTE_W, carrier_share=CARRIER_SHARE)
    assert approx(build.observed_pax_share, "0.0829", "0.0001")
    assert "8.3%" in build.summary()


# ==========================================================================
# Temporal aggregation
# ==========================================================================

def test_period_mean_matches_the_methodology_identity():
    day2_prices = {
        (BLR_DEL, 7): {"QP": D("6240"), "SG": D("5880")},
        (BLR_DEL, 30): {"QP": D("4182"), "SG": D("3990")},
        (BOM_DEL, 7): {"QP": D("6000"), "SG": D("5310")},
        (BOM_DEL, 30): {"QP": D("3774"), "SG": D("3640")},
    }
    b1 = build_index(DAY, build_elementary(DAY, DAY1_PRICES, BASE, CELLS),
                     WEIGHTS, ROUTE_W)
    d2 = DAY + dt.timedelta(days=1)
    b2 = build_index(d2, build_elementary(d2, day2_prices, BASE, CELLS),
                     WEIGHTS, ROUTE_W)
    assert approx(b1.index_value, "108.7691")
    assert approx(b2.index_value, "111.9522")

    weekly = aggregate_period([b1, b2], "weekly", week_start(DAY))
    assert approx(weekly.index_value, "110.3607")
    assert weekly.frequency == "weekly"


def test_period_aggregation_averages_route_values():
    b1 = build_index(DAY, build_elementary(DAY, DAY1_PRICES, BASE, CELLS),
                     WEIGHTS, ROUTE_W)
    monthly = aggregate_period([b1, b1], "monthly", month_start(DAY))
    for r, v in b1.route_values.items():
        assert approx(monthly.route_values[r], v)


def test_empty_period_raises():
    with pytest.raises(ValueError):
        aggregate_period([], "weekly", DAY)


def test_period_boundaries():
    assert week_start(dt.date(2026, 9, 4)) == dt.date(2026, 8, 31)   # Friday -> Monday
    assert week_start(dt.date(2026, 8, 31)) == dt.date(2026, 8, 31)
    assert month_start(dt.date(2026, 9, 4)) == dt.date(2026, 9, 1)
