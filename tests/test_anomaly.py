"""Anomaly detector tests.

Two properties matter most and get the most attention. A sustained surge must be
reported as one episode rather than as one event per day, because inflating an
event count is how a detector starts looking cleverer than it is. And a
hypothesis must never harden into a cause on its way to the dashboard.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apix.analytics.anomaly import (
    DEFAULT_THRESHOLD,
    DEFAULT_WINDOW,
    MIN_HISTORY,
    Anomaly,
    collapse_episodes,
    detect,
    detect_and_label,
    episode_direction,
    episode_peak,
    label_hypotheses,
)

D = Decimal
START = dt.date(2026, 7, 1)


def series(values, start=START):
    return [(start + dt.timedelta(days=i), D(str(v))) for i, v in enumerate(values)]


def flat(n=30, level=100.0, wobble=1.2, seed=17):
    """A calm but realistic series.

    Seeded pseudo-random noise, NOT a perfect alternation. An exactly
    alternating series has a near-zero median absolute deviation, which makes
    every ordinary move score enormously and turns the detector pathological on
    data no real index would produce. Fixtures that are unrealistically clean
    make a detector look broken when it is not; on the real seeded index this
    same detector produces two flags and one episode."""
    import random
    rng = random.Random(seed)
    return [level + rng.uniform(-wobble, wobble) for _ in range(n)]


# ==========================================================================
# Detection
# ==========================================================================

def test_calm_series_produces_no_flags():
    assert detect(series(flat())) == []


def test_a_sharp_spike_is_flagged():
    v = flat(30)
    v[20] = 150.0
    found = detect(series(v))
    assert found
    assert any(a.index_date == START + dt.timedelta(days=20) for a in found)
    assert found[0].direction == "spike"


def test_a_sharp_drop_is_flagged_as_a_drop():
    v = flat(30)
    v[20] = 60.0
    found = detect(series(v))
    assert found and found[0].direction == "drop"
    assert found[0].change_pct < 0


def test_nothing_is_flagged_before_enough_history():
    """A detector that fires on its third observation is measuring its own
    warm-up, not the market."""
    v = flat(40)
    v[3] = 150.0
    found = detect(series(v))
    assert all(a.index_date > START + dt.timedelta(days=MIN_HISTORY) for a in found)


def test_short_series_returns_nothing():
    assert detect(series(flat(5))) == []


def test_zero_dispersion_produces_no_flags():
    """A perfectly constant series has no yardstick, so nothing can be extreme
    relative to it. Returning nothing is correct; dividing by zero is not."""
    assert detect(series([100.0] * 40)) == []


def test_detector_never_looks_ahead():
    """A flag on the last day must be raisable in real time. Truncating the
    series after that day must not change the verdict."""
    v = flat(40)
    v[30] = 150.0
    full = detect(series(v))
    truncated = detect(series(v[:31]))
    target = START + dt.timedelta(days=30)
    assert any(a.index_date == target for a in full)
    assert any(a.index_date == target for a in truncated)


def test_threshold_is_honoured():
    v = flat(40)
    v[25] = 112.0
    assert len(detect(series(v), threshold=2.0)) >= len(
        detect(series(v), threshold=8.0))


def test_higher_threshold_can_silence_a_marginal_move():
    v = flat(40)
    v[25] = 108.0
    assert detect(series(v), threshold=50.0) == []


def test_defaults_match_the_cleaner():
    """The same robust statistic and threshold as apix/pipeline/outliers.py, so
    the two do not disagree about what 'extreme' means."""
    assert DEFAULT_THRESHOLD == 3.5
    assert DEFAULT_WINDOW == 14


# ==========================================================================
# Episodes
# ==========================================================================

def test_a_sustained_surge_does_not_flag_once_per_day():
    """The anti-inflation property, stated as what the detector can deliver.

    Because detection runs on day-over-day CHANGES, a four-day plateau is not
    four anomalies: the days in the middle of it barely move. Only the onset and
    the return are flagged. So the guarantee is not "one episode" — the onset
    and the return are genuinely days apart and proximity grouping cannot join
    them across the plateau — but that a four-day event yields two flags rather
    than four or more."""
    v = flat(40)
    for i in (20, 21, 22, 23):
        v[i] = 150.0 + i
    found = detect(series(v))
    assert len(found) <= 2, [(a.index_date, a.direction) for a in found]
    assert found[0].direction == "spike"
    assert found[-1].direction == "drop"
    assert len(collapse_episodes(found)) <= 2


def test_an_event_does_not_poison_the_baseline_that_follows_it():
    """Regression. Flagged days used to remain in the trailing window, shifting
    the median and MAD so that ordinary days afterwards scored as extreme. A
    four-day surge produced a tail of six false positives running two weeks past
    the event."""
    v = flat(45)
    for i in (20, 21, 22, 23):
        v[i] = 150.0 + i
    found = detect(series(v))
    after = [a for a in found if a.index_date > START + dt.timedelta(days=26)]
    assert after == [], f"false positives after the event: {[a.index_date for a in after]}"


def test_two_separated_events_stay_separate():
    v = flat(60)
    v[20] = 150.0
    v[45] = 150.0
    assert len(collapse_episodes(detect(series(v)))) == 2


def test_a_spike_and_its_return_are_one_episode():
    """Because detection runs on day-over-day changes, an excursion always
    produces a move away from baseline and a move back. Those are one event.
    Counting them separately doubles every spike in the report."""
    a = Anomaly(START, D("140"), 40.0, 9.0, "spike", 14, 3.5)
    b = Anomaly(START + dt.timedelta(days=1), D("95"), -32.0, -9.0, "drop", 14, 3.5)
    episodes = collapse_episodes([a, b])
    assert len(episodes) == 1
    assert episode_direction(episodes[0]) == "spike"   # the larger move
    assert episode_peak(episodes[0]).index_date == START


def test_a_one_day_spike_is_a_single_episode():
    v = flat(40)
    v[25] = 150.0
    episodes = collapse_episodes(detect(series(v)))
    assert len(episodes) == 1, [[a.index_date for a in e] for e in episodes]
    assert episode_direction(episodes[0]) == "spike"


def test_empty_input_collapses_to_nothing():
    assert collapse_episodes([]) == []


# ==========================================================================
# Hypotheses must not become causes
# ==========================================================================

def test_a_label_is_phrased_as_a_candidate():
    a = Anomaly(dt.date(2026, 10, 20), D("140"), 40.0, 9.0, "spike", 14, 3.5)
    labelled = label_hypotheses(a)
    assert labelled.hypotheses
    assert all(h.startswith("candidate:") or "unknown" in h
               for h in labelled.hypotheses)


def test_no_candidate_says_so_rather_than_inventing_one():
    a = Anomaly(dt.date(2026, 8, 12), D("140"), 40.0, 9.0, "spike", 14, 3.5)
    labelled = label_hypotheses(a)
    assert any("unknown" in h for h in labelled.hypotheses)


def test_serialised_flag_carries_the_not_causal_warning():
    a = label_hypotheses(Anomaly(dt.date(2026, 10, 20), D("140"), 40.0,
                                 9.0, "spike", 14, 3.5))
    d = a.as_dict()
    assert "not a causal finding" in d["note"]
    assert isinstance(d["hypotheses"], list)


def test_labelling_does_not_alter_the_statistics():
    a = Anomaly(dt.date(2026, 10, 20), D("140"), 40.0, 9.0, "spike", 14, 3.5)
    b = label_hypotheses(a)
    assert (b.score, b.change_pct, b.index_value) == (a.score, a.change_pct,
                                                      a.index_value)


def test_detect_and_label_matches_detect_then_label():
    v = flat(40)
    v[25] = 150.0
    assert [a.index_date for a in detect_and_label(series(v))] == \
           [a.index_date for a in detect(series(v))]


# ==========================================================================
# Parity with the demo data
# ==========================================================================

def _db() -> bool:
    try:
        from apix import db
        db.fetch_all("SELECT 1 AS x")
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _db(), reason="postgres not reachable; run `make demo`")
def test_finds_the_seeded_august_surge():
    """The generator injects an eight-day surge from 2026-08-20. If the
    detector cannot find a surge that was deliberately put there, it will not
    find a real one."""
    from apix import db

    rows = db.fetch_all(
        "SELECT index_date, index_value FROM apix.apix_index "
        "WHERE frequency='daily' ORDER BY index_date")
    if len(rows) < 30:
        pytest.skip("no built index; run `make demo`")

    found = detect([(r["index_date"], r["index_value"]) for r in rows])
    assert found, "the seeded surge was not detected"
    august = [a for a in found if a.index_date.month == 8]
    assert august, [a.index_date for a in found]
    assert any(a.is_spike for a in august)
    # And it must read as one event, not a week of them.
    assert len(collapse_episodes(august)) <= 2
