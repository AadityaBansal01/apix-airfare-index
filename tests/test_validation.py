"""Validation and back-test tests.

The scoring functions decide whether the index is judged to track the market, so
they get tested against cases with known answers rather than against whatever the
code currently returns.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apix.validation.metrics import (
    MIN_PAIRS_FOR_CORRELATION,
    changes,
    compare,
    direction_agreement,
    mape,
    pearson,
    rebase,
    rmse,
    spearman,
)
from apix.validation.suite import (
    benchmark_against_naive,
    check_base_period,
    check_contributions_reconstruct,
    check_coverage_reported,
    check_temporal_consistency,
    robustness_over_window_weights,
)

D = Decimal


# ==========================================================================
# Correlation
# ==========================================================================

def test_pearson_perfect_positive_and_negative():
    assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert pearson([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)


def test_pearson_is_none_for_a_constant_series():
    """A flat series has no correlation. Reporting r=0 would imply we measured
    independence when we measured nothing."""
    assert pearson([5, 5, 5, 5], [1, 2, 3, 4]) is None


def test_pearson_ignores_paired_nulls():
    assert pearson([1, 2, None, 4], [2, 4, 99, 8]) == pytest.approx(1.0)


def test_pearson_rejects_length_mismatch():
    with pytest.raises(ValueError, match="length mismatch"):
        pearson([1, 2, 3], [1, 2])


def test_spearman_survives_a_monotone_nonlinearity():
    """The reason rank correlation is scored alongside Pearson: quoted and
    realised fares may relate non-linearly and still rank identically."""
    x = [1, 2, 3, 4, 5]
    y = [1, 4, 9, 16, 25]
    assert spearman(x, y) == pytest.approx(1.0)
    assert pearson(x, y) < 1.0


def test_spearman_handles_ties_with_average_ranks():
    assert spearman([1, 2, 2, 3], [1, 2, 2, 3]) == pytest.approx(1.0)


# ==========================================================================
# Changes and direction
# ==========================================================================

def test_changes_are_percentages_and_one_shorter():
    c = changes([100, 110, 99])
    assert len(c) == 2
    assert c[0] == pytest.approx(10.0)
    assert c[1] == pytest.approx(-10.0)


def test_direction_agreement_perfect_and_opposite():
    up = [100, 110, 120, 130]
    assert direction_agreement(up, [50, 55, 60, 65])[0] == pytest.approx(1.0)
    assert direction_agreement(up, [50, 45, 40, 35])[0] == pytest.approx(0.0)


def test_direction_agreement_tolerance_treats_noise_as_flat():
    a = [100, 100.001, 100.002]
    b = [200, 199.999, 199.998]
    # Without tolerance these disagree on every period; with it, both are flat.
    assert direction_agreement(a, b, tolerance=0.0)[0] == pytest.approx(0.0)
    assert direction_agreement(a, b, tolerance=0.05)[0] == pytest.approx(1.0)


def test_direction_agreement_reports_its_own_n():
    _, n = direction_agreement([100, 110, 120], [1, 2, 3])
    assert n == 2


# ==========================================================================
# Rebasing and error
# ==========================================================================

def test_rebase_sets_the_anchor_to_100():
    assert rebase([50, 75, 100])[0] == pytest.approx(100.0)
    assert rebase([50, 75, 100])[2] == pytest.approx(200.0)


def test_rebase_returns_none_on_a_zero_anchor():
    assert rebase([0, 1, 2]) is None


def test_mape_and_rmse_are_zero_for_identical_series():
    s = [100, 105, 110]
    assert mape(s, s) == pytest.approx(0.0)
    assert rmse(s, s) == pytest.approx(0.0)


# ==========================================================================
# compare()
# ==========================================================================

def test_compare_flags_short_series_as_unreliable():
    """Three points can produce r near 1 by coincidence. The result must say so
    rather than publish a flattering number."""
    r = compare([100, 110, 120], [50, 55, 60])
    assert r.reliable is False
    assert "not interpretable" in r.note


def test_compare_is_reliable_once_long_enough():
    n = MIN_PAIRS_FOR_CORRELATION + 2
    idx = [100 + i * 2 for i in range(n)]
    ref = [500 + i * 9 for i in range(n)]
    r = compare(idx, ref)
    assert r.reliable is True
    assert r.get("pearson_r_on_changes") is not None
    assert r.get("direction_agreement").value == pytest.approx(1.0)


def test_compare_scores_movement_not_level():
    """Two series at wildly different levels that move together must score well.
    This is the whole reason absolute error is not the primary metric."""
    idx = [100, 104, 101, 108, 112, 109, 115]
    ref = [8000, 8320, 8080, 8640, 8960, 8720, 9200]   # 80x the level
    r = compare(idx, ref)
    assert r.get("pearson_r_on_changes").value > 0.99
    assert r.get("direction_agreement").value == pytest.approx(1.0)


def test_compare_rejects_length_mismatch():
    with pytest.raises(ValueError):
        compare([1, 2, 3], [1, 2])


def test_compare_serialises_for_the_report():
    d = compare([100, 102, 101, 105, 108, 106, 110],
                [50, 51, 50, 53, 55, 54, 56]).as_dict()
    assert "scores" in d and "pearson_r_on_changes" in d["scores"]
    assert all("interpretation" in s for s in d["scores"].values())


# ==========================================================================
# Identity checks
# ==========================================================================

def test_base_period_check_passes_near_100_and_fails_far_from_it():
    assert check_base_period([D("99.8"), D("100.3"), D("100.1")]).passed
    assert not check_base_period([D("112"), D("113")]).passed


def test_base_period_check_fails_loudly_when_there_is_nothing_to_check():
    """An absent reference-period series must not silently pass. This caught a
    real defect: the CLI was excluding those dates from the build entirely."""
    c = check_base_period([])
    assert not c.passed
    assert "no index values" in c.detail


def test_temporal_consistency_detects_a_broken_aggregate():
    daily = {dt.date(2026, 8, 1): [D("100"), D("110")]}
    assert check_temporal_consistency(daily, {dt.date(2026, 8, 1): D("105")}).passed
    assert not check_temporal_consistency(
        daily, {dt.date(2026, 8, 1): D("108")}).passed


def test_contributions_must_reconstruct_the_headline():
    assert check_contributions_reconstruct(
        D("103.0839"), [D("27.47"), D("20.88"), D("54.7339")]).passed
    assert not check_contributions_reconstruct(
        D("103.0839"), [D("27.47"), D("20.88")]).passed


def test_coverage_check_rejects_missing_or_impossible_ratios():
    assert check_coverage_reported([{"coverage_ratio": 1.0}]).passed
    assert not check_coverage_reported([{"coverage_ratio": None}]).passed
    assert not check_coverage_reported([{"coverage_ratio": 0}]).passed
    assert not check_coverage_reported([{"coverage_ratio": 1.5}]).passed


# ==========================================================================
# Robustness
# ==========================================================================

def test_window_weight_scenarios_move_the_headline():
    """If alternative window weights produced an identical series, the weights
    would be doing nothing and the assumption would not need stating."""
    elementary = {
        dt.date(2026, 8, d): {(1, 7): D("120"), (1, 30): D("100")}
        for d in range(1, 6)
    }
    route_w = {1: D("1.0")}
    scenarios = {
        "equal": {7: D("0.5"), 30: D("0.5")},
        "near_heavy": {7: D("0.9"), 30: D("0.1")},
    }
    out = robustness_over_window_weights(elementary, route_w, scenarios)
    assert len(out) == 2
    equal = next(r for r in out if r["scenario"] == "equal")
    near = next(r for r in out if r["scenario"] == "near_heavy")
    assert equal["mean"] == pytest.approx(110.0)
    assert near["mean"] == pytest.approx(118.0)
    assert near["max_gap_vs_baseline"] == pytest.approx(8.0)


def test_baseline_scenario_has_zero_gap_against_itself():
    elementary = {dt.date(2026, 8, 1): {(1, 7): D("120"), (1, 30): D("100")}}
    out = robustness_over_window_weights(
        elementary, {1: D("1.0")}, {"equal": {7: D("0.5"), 30: D("0.5")}})
    assert out[0]["max_gap_vs_baseline"] == 0.0


# ==========================================================================
# Benchmark
# ==========================================================================

def test_benchmark_reports_divergence_from_a_naive_series():
    idx = [100, 106, 103, 112, 118, 115, 121]
    naive = [5000, 5200, 5100, 5400, 5600, 5500, 5700]
    b = benchmark_against_naive(idx, naive)
    assert b["n"] == 7
    assert b["correlation_on_changes"] is not None
    assert b["max_divergence_points"] >= 0


def test_benchmark_declines_on_too_little_data():
    assert "not enough" in benchmark_against_naive([100], [5000])["note"]


# ==========================================================================
# The DGCA reference must not be fabricated
# ==========================================================================

def _db_available() -> bool:
    try:
        from apix import db
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM apix.dgca_fare_reference LIMIT 1")
            cur.fetchone()
            return True
    except Exception:
        return False


@pytest.mark.skipif(not _db_available(), reason="postgres not reachable")
def test_demo_database_holds_no_invented_reference_fares():
    """The external comparison ships unrun on purpose.

    No public DGCA route-wise monthly average-fare series was found. If this
    fails, someone has put fares in the reference table; check that they came
    from a real, citable source and that fare_basis records what they mean.
    """
    from apix import db
    rows = db.fetch_all(
        "SELECT route_id, ref_month, fare_basis, source_url "
        "FROM apix.dgca_fare_reference")
    assert rows == [], (
        f"unexpected reference fares present ({len(rows)} rows). Every one needs "
        f"a real source_url and a fare_basis saying what 'average fare' means.")


def test_canonical_route_ordering():
    from scripts.load_dgca_fares import canonical_route
    assert canonical_route("BOM-DEL") == ("BOM", "DEL")
    assert canonical_route("DEL-BOM") == ("BOM", "DEL")
    assert canonical_route(" del-bom ") == ("BOM", "DEL")
    for bad in ("DELBOM", "DEL-BOM-BLR", "D-B", ""):
        with pytest.raises(ValueError):
            canonical_route(bad)
