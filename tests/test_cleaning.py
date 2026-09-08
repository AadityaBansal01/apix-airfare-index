"""Cleaning pipeline tests.

The central claim under test is that the cleaner removes DATA ERRORS without
removing PRICE SIGNAL. Several tests below exist specifically to fail if someone
later "improves" the detector into something that smooths genuine surges.
"""
from __future__ import annotations

import datetime as dt
import math
import statistics
from decimal import Decimal

import pytest

from apix.pipeline.clean import (
    MAX_PLAUSIBLE_FARE,
    MIN_PLAUSIBLE_FARE,
    REJECT_ABOVE_MAX,
    REJECT_BELOW_MIN,
    REJECT_NONPOSITIVE,
    check_plausible,
    clean_quotes,
    deduplicate,
    select_index_prices,
    verify_decomposition,
)
from apix.pipeline.outliers import (
    MIN_SAMPLE_FOR_DISPERSION,
    Method,
    detect_iqr,
    detect_mad_log,
)
from apix.sources.base import FareQuote

D = Decimal
SCRAPE = dt.date(2026, 9, 4)
DEPART = dt.date(2026, 9, 11)


def q(total, *, carrier="QP", flight="QP1101", base=None, taxes=None, udf=None,
      conv=None, other=None, origin="DEL", destination="BOM",
      scrape=SCRAPE, depart=DEPART, sold_out=False, brand="SAVER") -> FareQuote:
    return FareQuote(
        source_code="akasa", origin=origin, destination=destination,
        carrier=carrier, departure_date=depart, scrape_date=scrape,
        total_fare=D(str(total)), flight_number=flight, fare_brand=brand,
        base_fare=D(str(base)) if base is not None else None,
        taxes=D(str(taxes)) if taxes is not None else None,
        udf=D(str(udf)) if udf is not None else None,
        convenience_fee=D(str(conv)) if conv is not None else None,
        other_charges=D(str(other)) if other is not None else None,
        is_sold_out=sold_out,
    )


# ==========================================================================
# The load-bearing property: price signal survives
# ==========================================================================

def test_a_genuine_market_wide_surge_is_never_flagged():
    """Every fare triples overnight. Not one may be called an outlier.

    This is the failure mode that would quietly destroy the index: a detector
    that smooths exactly the movements APIx exists to measure.
    """
    normal = [q(f, flight=f"QP{i}") for i, f in
              enumerate([4800, 5100, 5400, 5900, 6200, 7100])]
    surged = [q(f * 3, flight=f"QP{i}", scrape=dt.date(2026, 9, 5),
                depart=dt.date(2026, 9, 12))
              for i, f in enumerate([4800, 5100, 5400, 5900, 6200, 7100])]

    r1 = clean_quotes(normal)
    r2 = clean_quotes(surged)
    assert r1.report.outliers == 0
    assert r2.report.outliers == 0, "a market-wide surge was wrongly flagged"
    assert len(r2.clean) == 6


def test_comparison_never_crosses_scrape_dates():
    """A 5x jump between days must survive, because it is a price movement."""
    day1 = [q(5000 + i * 100, flight=f"QP{i}") for i in range(6)]
    day2 = [q(25000 + i * 500, flight=f"QP{i}", scrape=dt.date(2026, 9, 5),
              depart=dt.date(2026, 9, 12)) for i in range(6)]
    result = clean_quotes(day1 + day2)
    assert result.report.outliers == 0
    assert result.report.comparison_groups == 2


def test_wide_but_legitimate_intraday_spread_survives():
    """Morning and evening fares differ a lot. That is real dispersion."""
    fares = [3200, 3900, 4800, 6100, 7900, 9400, 11200]
    result = clean_quotes([q(f, flight=f"QP{i}") for i, f in enumerate(fares)])
    assert result.report.outliers == 0


# ==========================================================================
# Data errors are caught
# ==========================================================================

def test_decimal_shift_parse_error_is_flagged():
    """A 10x decimal shift: 6000 read as 60000.

    Deliberately inside the plausibility bounds, so this test exercises the
    statistical detector rather than the cheap range check. A fare of 520000
    would be rejected earlier as implausible and prove nothing about the detector.
    """
    fares = [4800, 5100, 5400, 5900, 6200, 60000]
    result = clean_quotes([q(f, flight=f"QP{i}") for i, f in enumerate(fares)])
    assert result.report.rejected_count == 0, "must be caught by the detector"
    assert result.report.outliers == 1
    assert result.flagged[0].total_fare == D("60000")
    assert any(f.startswith("outlier:mad_log:") for f in result.flagged[0].quality_flags)


def test_flagged_quote_is_retained_not_deleted():
    fares = [4800, 5100, 5400, 5900, 6200, 60000]
    result = clean_quotes([q(f, flight=f"QP{i}") for i, f in enumerate(fares)])
    assert len(result.persistable) == 6, "flagged rows must still be persisted"
    assert result.clean and result.flagged


def test_mad_resists_masking_where_plain_zscore_fails():
    """The reason the default is MAD and not a standard z-score.

    One extreme contaminant inflates the standard deviation enough to hide
    itself. The median and MAD have a 50% breakdown point and do not.
    """
    fares = [D(x) for x in ["4800", "5100", "5400", "5900", "6200", "900000"]]
    vals = [float(f) for f in fares]
    mean, sd = statistics.fmean(vals), statistics.stdev(vals)
    plain_z = abs((vals[-1] - mean) / sd)
    assert plain_z < 2.5, "contaminant should be masked under a plain z-score"

    verdicts = detect_mad_log(fares)
    assert verdicts[-1].is_outlier
    assert abs(verdicts[-1].score) > 10


# ==========================================================================
# Small samples
# ==========================================================================

def test_small_groups_are_not_assessed():
    """Below the minimum sample, flagging would be numerology."""
    for n in range(1, MIN_SAMPLE_FOR_DISPERSION):
        fares = [q(4000 + i * 3000, flight=f"QP{i}") for i in range(n)]
        result = clean_quotes(fares)
        assert result.report.outliers == 0, f"n={n} should not be assessed"
        assert result.report.groups_too_small_to_assess == (1 if n else 0)


def test_identical_fares_produce_no_outliers():
    result = clean_quotes([q(5000, flight=f"QP{i}") for i in range(8)])
    assert result.report.outliers == 0


# ==========================================================================
# Plausibility bounds
# ==========================================================================

@pytest.mark.parametrize("total,reason", [
    (0, REJECT_NONPOSITIVE),
    (-100, REJECT_NONPOSITIVE),
    (1, REJECT_BELOW_MIN),
    (499, REJECT_BELOW_MIN),
    (200001, REJECT_ABOVE_MAX),
])
def test_implausible_fares_rejected(total, reason):
    assert check_plausible(q(total)) == reason


@pytest.mark.parametrize("total", [500, 5000, 50000, 200000])
def test_plausible_fares_accepted(total):
    assert check_plausible(q(total)) is None


def test_rejected_quotes_are_counted_with_reasons():
    quotes = [q(5000), q(12), q(999999), q(6000)]
    result = clean_quotes(quotes)
    assert result.report.rejected_count == 2
    assert result.report.reasons() == {
        REJECT_BELOW_MIN: 1, REJECT_ABOVE_MAX: 1
    }
    assert len(result.persistable) == 2


def test_rejected_quotes_are_not_persisted():
    """They failed a basic plausibility test, so they are not fares."""
    result = clean_quotes([q(5000), q(3)])
    assert all(f.total_fare == D("5000") for f in result.persistable)


# ==========================================================================
# Deduplication
# ==========================================================================

def test_exact_duplicates_collapse():
    quotes = [q(5000), q(5000), q(5000), q(5200)]
    kept, dropped = deduplicate(quotes)
    assert dropped == 2
    assert len(kept) == 2


def test_different_fare_brands_are_not_duplicates():
    """Two brands on one flight are two observations."""
    quotes = [q(5000, brand="SAVER"), q(6500, brand="FLEXI")]
    kept, dropped = deduplicate(quotes)
    assert dropped == 0
    assert len(kept) == 2


def test_same_flight_from_two_sources_is_not_a_duplicate():
    """Corroboration from a second source must not be silently discarded."""
    a = q(5000)
    b = q(5000)
    b.source_code = "spicejet"
    kept, dropped = deduplicate([a, b])
    assert dropped == 0
    assert len(kept) == 2


# ==========================================================================
# Fare decomposition
# ==========================================================================

def test_reconciling_split_is_kept():
    f = q(5000, base=4200, taxes=520, udf=180, conv=100)
    verify_decomposition(f)
    assert f.components_complete is True
    assert f.base_fare == D("4200")


def test_non_reconciling_split_is_cleared_not_corrected():
    """We never invent a decomposition to make the arithmetic work."""
    f = q(5000, base=4200, taxes=520, udf=180, conv=900)   # sums to 5800
    verify_decomposition(f)
    assert f.components_complete is False
    assert f.base_fare is None and f.taxes is None
    assert f.total_fare == D("5000"), "the observed total must survive"
    assert "components_did_not_reconcile" in f.quality_flags


def test_one_rupee_rounding_tolerance_is_accepted():
    f = q(5000, base=4200, taxes=520, udf=180, conv=99.5)   # 4999.50
    verify_decomposition(f)
    assert f.components_complete is True


def test_missing_split_is_flagged_but_fare_kept():
    f = q(5000)
    verify_decomposition(f)
    assert f.components_complete is False
    assert "no_component_split" in f.quality_flags
    assert f.total_fare == D("5000")


def test_report_tracks_component_completeness():
    quotes = [
        q(5000, base=4200, taxes=520, udf=180, conv=100, flight="QP1"),
        q(5200, base=4400, taxes=520, udf=180, conv=100, flight="QP2"),
        q(5400, flight="QP3"),
    ]
    result = clean_quotes(quotes)
    assert result.report.components_complete == 2
    assert result.report.components_missing == 1
    assert result.report.component_completeness == pytest.approx(2 / 3)


# ==========================================================================
# Sold out
# ==========================================================================

def test_sold_out_excluded_from_index_but_retained():
    quotes = [q(5000, flight="QP1"), q(9999, flight="QP2", sold_out=True)]
    result = clean_quotes(quotes)
    assert result.report.sold_out == 1
    assert len(result.clean) == 1
    assert any(f.is_sold_out for f in result.flagged)
    assert ("DEL", "BOM", 7, "QP") in result.index_prices
    assert result.index_prices[("DEL", "BOM", 7, "QP")] == D("5000")


def test_sold_out_does_not_distort_the_outlier_median():
    """Set aside before assessment, so it cannot shift the group's centre."""
    normal = [q(5000 + i * 100, flight=f"QP{i}") for i in range(6)]
    blocked = [q(60000, flight="QPX", sold_out=True)]
    result = clean_quotes(normal + blocked)
    assert result.report.outliers == 0
    assert result.report.sold_out == 1


# ==========================================================================
# Index price selection
# ==========================================================================

def test_lowest_fare_per_cell_is_selected():
    quotes = [q(6200, flight="QP1"), q(4800, flight="QP2"), q(5500, flight="QP3")]
    assert select_index_prices(quotes)[("DEL", "BOM", 7, "QP")] == D("4800")


def test_cells_split_by_carrier_and_window():
    quotes = [
        q(5000, carrier="QP", flight="QP1"),
        q(4700, carrier="SG", flight="SG1"),
        q(3900, carrier="QP", flight="QP2", depart=dt.date(2026, 10, 4)),
    ]
    prices = select_index_prices(quotes)
    assert prices[("DEL", "BOM", 7, "QP")] == D("5000")
    assert prices[("DEL", "BOM", 7, "SG")] == D("4700")
    assert prices[("DEL", "BOM", 30, "QP")] == D("3900")


# ==========================================================================
# Report
# ==========================================================================

def test_discard_rate_and_summary():
    quotes = (
        [q(5000 + i * 100, flight=f"QP{i}") for i in range(6)]
        + [q(5000, flight="QP0")]        # duplicate
        + [q(12, flight="QPbad")]        # implausible
        + [q(60000, flight="QPerr")]     # outlier: inside bounds, caught by detector
    )
    result = clean_quotes(quotes)
    r = result.report
    assert r.input_quotes == 9
    assert r.exact_duplicates == 1
    assert r.rejected_count == 1
    assert 0 < r.discard_rate < 1
    assert "discarded" in r.summary()


def test_empty_input_is_safe():
    result = clean_quotes([])
    assert result.report.input_quotes == 0
    assert result.report.discard_rate == 0.0
    assert result.index_prices == {}


# ==========================================================================
# IQR alternative
# ==========================================================================

def test_iqr_available_as_alternative():
    fares = [q(f, flight=f"QP{i}") for i, f in
             enumerate([4800, 5100, 5400, 5900, 6200, 60000])]
    result = clean_quotes(fares, method=Method.IQR)
    assert result.report.outliers == 1
    assert result.flagged[0].total_fare == D("60000")


def test_iqr_lower_fence_goes_negative_on_skewed_fares():
    """Documents the concrete defect that rules Tukey fences out as the default.

    On right-skewed fare data the symmetric raw-scale lower fence falls below
    zero, so it can never flag a suspiciously CHEAP quote. A fare of 1 rupee
    would pass. The log-scale method is symmetric in ratio terms instead, which
    is the scale prices actually vary on and the same scale the Jevons index
    uses, so the cleaner and the index agree about what "far apart" means.
    """
    import math
    from apix.pipeline.outliers import _quantile

    fares = [3000.0, 3050.0, 3100.0, 3150.0, 3200.0, 20000.0, 25000.0, 30000.0]
    q1, q3 = _quantile(fares, 0.25), _quantile(fares, 0.75)
    lower_fence = q1 - 1.5 * (q3 - q1)
    assert lower_fence < 0, "expected the raw-scale lower fence to be meaningless"

    # The log-scale detector has a usable lower tail: an absurdly cheap quote
    # among the same fares is flagged.
    with_cheap = [D("1")] + [D(str(int(f))) for f in fares]
    verdicts = detect_mad_log(with_cheap)
    assert verdicts[0].is_outlier
    assert verdicts[0].score < 0


def test_absent_split_clears_every_component():
    """Regression. A quote flagged 'no_component_split' could still carry a UDF
    and a convenience fee, so 'absent' meant 'partially present' and anything
    summing the survivors got a number that was not the fare."""
    f = q(5000, udf=180, conv=100)
    verify_decomposition(f)
    assert f.base_fare is None and f.taxes is None
    assert f.udf is None and f.convenience_fee is None and f.other_charges is None
    assert f.components_complete is False
    assert f.total_fare == D("5000")


def test_non_reconciling_split_clears_every_component_too():
    f = q(5000, base=4200, taxes=520, udf=180, conv=900)
    verify_decomposition(f)
    assert all(v is None for v in
               (f.base_fare, f.taxes, f.udf, f.convenience_fee, f.other_charges))
