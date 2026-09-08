"""Scheduled collection tests.

The four claims worth testing are the four a reviewer would doubt: that one bad
cell does not take the cycle down, that a refusal is never retried, that the rate
limit survives concurrent workers, and that a missed day is admitted rather than
silently reconstructed from today's prices.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from decimal import Decimal

import pytest

from apix.net.session import FetchFailed, PolicyRefused
from apix.net.throttle import ThrottlePolicy, Throttler
from apix.orchestration.matrix import Cell, SourceCost, build_plan
from apix.orchestration.runner import (
    FAILED, NO_FLIGHTS, OK, REFUSED, SKIPPED, SOLD_OUT,
    CycleRunner, classify,
)
from apix.orchestration.schedule import IST, DailySchedule, next_run_at, seconds_until
from apix.sources.base import FareQuote, NoFlights, ParseError, SearchRequest, SoldOut

TODAY = dt.date(2026, 9, 8)

BASKET = {
    "routes": [
        {"code": "BOM-DEL", "origin": "BOM", "destination": "DEL"},
        {"code": "BLR-HYD", "origin": "BLR", "destination": "HYD"},
    ],
    "advance_windows": [{"days": 1}, {"days": 7}, {"days": 30}],
}


class FakePolicy:
    def __init__(self, delay=8.0, jitter=2.0):
        self.min_delay_seconds = delay
        self.jitter_seconds = jitter


def quote(cell: Cell, scrape_date=TODAY) -> FareQuote:
    return FareQuote(
        source_code=cell.source_code, origin=cell.origin,
        destination=cell.destination, carrier="QP",
        departure_date=scrape_date + dt.timedelta(days=cell.window_days),
        scrape_date=scrape_date, total_fare=Decimal("5400.00"))


class FakeResult:
    request_id = 42


class FakeSource:
    """An adapter whose behaviour per call is scripted."""

    def __init__(self, code="akasa", script=None):
        self.code = code
        self.script = list(script or [])
        self.calls: list[SearchRequest] = []

    async def fetch(self, req):
        self.calls.append(req)
        action = self.script.pop(0) if self.script else None
        if isinstance(action, BaseException):
            raise action
        return FakeResult()

    def parse(self, req, result):
        return [FareQuote(
            source_code=self.code, origin=req.origin, destination=req.destination,
            carrier="QP", departure_date=req.departure_date,
            scrape_date=req.scrape_date, total_fare=Decimal("5400.00"))]


def runner_for(sources, **kw):
    kw.setdefault("sleep", lambda _s: asyncio.sleep(0))   # no real backoff in tests
    return CycleRunner(session=None, sources=sources, **kw)


# ==========================================================================
# The plan
# ==========================================================================

def test_plan_is_the_full_matrix():
    plan = build_plan(BASKET, ["akasa", "spicejet"], TODAY)
    assert len(plan) == 2 * 3 * 2          # routes x windows x sources
    assert plan.sources == ("akasa", "spicejet")


def test_plan_interleaves_sources_so_consecutive_cells_differ_in_origin():
    """Grouping by source would serialise the whole cycle behind one throttle."""
    plan = build_plan(BASKET, ["akasa", "spicejet"], TODAY)
    codes = [c.source_code for c in plan.cells]
    assert codes[:4] == ["akasa", "spicejet", "akasa", "spicejet"]


def test_plan_can_be_narrowed_to_a_route_and_window():
    plan = build_plan(BASKET, ["akasa"], TODAY, routes=["BOM-DEL"], windows=[7])
    assert len(plan) == 1
    assert str(plan.cells[0]) == "akasa:BOM-DEL:T+7"


def test_estimated_runtime_is_the_slowest_source_not_the_sum():
    """Different origins genuinely overlap; the same origin genuinely does not."""
    plan = build_plan(BASKET, ["akasa", "spicejet"], TODAY,
                      policies={"akasa": FakePolicy(8, 2), "spicejet": FakePolicy(8, 2)})
    # 6 cells each at a 9s mean interval = 54s per source, run concurrently.
    assert plan.estimated_seconds() == pytest.approx(54.0)


def test_estimate_charges_the_mean_jitter_not_the_best_case():
    cost = SourceCost("x", min_delay_seconds=8.0, jitter_seconds=2.0)
    assert cost.mean_interval == 9.0


def test_a_cell_knows_its_departure_date():
    c = Cell("akasa", "BOM", "DEL", 7)
    assert c.request(TODAY).departure_date == dt.date(2026, 9, 15)
    assert c.request(TODAY).window_days == 7


# ==========================================================================
# Isolation and retries
# ==========================================================================

def test_one_cell_failing_does_not_abort_the_cycle():
    """The property the whole design exists for: a bad night is a short day."""
    plan = build_plan(BASKET, ["akasa"], TODAY)
    src = FakeSource(script=[ParseError("page changed")] + [None] * 10)
    report = asyncio.run(runner_for({"akasa": src}).run(plan))
    assert report.planned == 6
    assert report.ok == 5
    assert report.failed == 1


def test_a_transient_failure_is_retried():
    plan = build_plan(BASKET, ["akasa"], TODAY, routes=["BOM-DEL"], windows=[7])
    src = FakeSource(script=[FetchFailed("timeout", request_id=1, status=None), None])
    report = asyncio.run(runner_for({"akasa": src}, max_attempts=2).run(plan))
    assert report.results[0].outcome == OK
    assert report.results[0].attempts == 2


def test_a_parse_error_is_not_retried():
    """The page shape will not change in thirty seconds; a retry is pure noise."""
    plan = build_plan(BASKET, ["akasa"], TODAY, routes=["BOM-DEL"], windows=[7])
    src = FakeSource(script=[ParseError("x"), ParseError("x")])
    report = asyncio.run(runner_for({"akasa": src}, max_attempts=3).run(plan))
    assert report.results[0].outcome == FAILED
    assert len(src.calls) == 1


def test_a_policy_refusal_is_never_retried():
    """Retrying a robots refusal is exactly what an ethics review looks for."""
    plan = build_plan(BASKET, ["akasa"], TODAY, routes=["BOM-DEL"], windows=[7])
    src = FakeSource(script=[PolicyRefused("no", request_id=1, intent_denied=False)] * 5)
    report = asyncio.run(runner_for({"akasa": src}, max_attempts=5).run(plan))
    assert report.results[0].outcome == REFUSED
    assert len(src.calls) == 1


def test_a_4xx_is_not_retried_but_a_5xx_is():
    assert classify(FetchFailed("", request_id=1, status=404)) == (FAILED, False)
    assert classify(FetchFailed("", request_id=1, status=503)) == (FAILED, True)
    assert classify(FetchFailed("", request_id=1, status=429)) == (FAILED, True)
    assert classify(FetchFailed("", request_id=1, status=None)) == (FAILED, True)


def test_sold_out_and_no_flights_are_outcomes_not_failures():
    """A sold-out cell is information. Counting it as a scrape failure would
    understate coverage exactly when the market is tightest."""
    assert classify(SoldOut()) == (SOLD_OUT, False)
    assert classify(NoFlights()) == (NO_FLIGHTS, False)
    plan = build_plan(BASKET, ["akasa"], TODAY, routes=["BOM-DEL"], windows=[7])
    src = FakeSource(script=[SoldOut()])
    report = asyncio.run(runner_for({"akasa": src}).run(plan))
    assert report.results[0].succeeded


def test_a_source_with_no_adapter_is_skipped_not_crashed():
    plan = build_plan(BASKET, ["nosuchsource"], TODAY, routes=["BOM-DEL"], windows=[7])
    report = asyncio.run(runner_for({}).run(plan))
    assert report.results[0].outcome == SKIPPED


def test_the_report_counts_every_planned_cell():
    plan = build_plan(BASKET, ["akasa", "spicejet"], TODAY)
    sources = {"akasa": FakeSource("akasa"), "spicejet": FakeSource("spicejet")}
    report = asyncio.run(runner_for(sources).run(plan))
    assert len(report.results) == report.planned == 12
    assert sum(report.by_outcome().values()) == 12


# ==========================================================================
# The rate limit under concurrency
# ==========================================================================

def test_the_rate_limit_holds_with_four_workers_on_one_origin():
    """The claim that concurrency cannot outrun the throttle, actually tested.

    Four workers race at one origin. OriginThrottle serialises them behind an
    asyncio.Lock, so the intervals it reports must never fall below the floor.
    Without the lock this test reports intervals near zero.
    """
    clock = {"t": 1000.0}
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)
        clock["t"] += s
        await asyncio.sleep(0)

    throttler = Throttler(ThrottlePolicy(min_delay_seconds=8.0, jitter_seconds=0.0),
                          seed=1)

    async def go():
        url = "https://www.akasaair.com/search"
        # First call to an origin returns 0.0, having no predecessor.
        intervals = await asyncio.gather(*[
            throttler.for_url(url).acquire(None, throttler._rng,
                                           clock=lambda: clock["t"], sleep=fake_sleep)
            for _ in range(5)
        ])
        return intervals

    intervals = asyncio.run(go())
    assert intervals[0] == 0.0
    later = intervals[1:]
    assert later, "expected four spaced requests"
    assert min(later) >= 8.0, f"rate limit violated under concurrency: {later}"


def test_workers_per_source_does_not_change_what_is_collected():
    """More workers may not change the result set, only (potentially) the wall
    clock. If it changed the outcome, the concurrency would be a bug."""
    plan = build_plan(BASKET, ["akasa"], TODAY)
    one = asyncio.run(runner_for({"akasa": FakeSource()}, workers_per_source=1).run(plan))
    four = asyncio.run(runner_for({"akasa": FakeSource()}, workers_per_source=4).run(plan))
    assert [str(r.cell) for r in one.results] == [str(r.cell) for r in four.results]
    assert one.ok == four.ok == 6


def test_every_cell_is_attempted_exactly_once_across_workers():
    """A shared queue, not a shared index. Two workers must not both take cell 3."""
    plan = build_plan(BASKET, ["akasa"], TODAY)
    src = FakeSource()
    asyncio.run(runner_for({"akasa": src}, workers_per_source=4).run(plan))
    seen = [(c.origin, c.destination, c.window_days) for c in src.calls]
    assert len(seen) == len(set(seen)) == 6


# ==========================================================================
# The schedule
# ==========================================================================

def test_next_run_is_the_next_occurrence_of_the_hour():
    now = dt.datetime(2026, 9, 8, 1, 30, tzinfo=IST)
    assert next_run_at(now, 2, 0) == dt.datetime(2026, 9, 8, 2, 0, tzinfo=IST)


def test_next_run_rolls_to_tomorrow_once_the_hour_has_passed():
    now = dt.datetime(2026, 9, 8, 2, 30, tzinfo=IST)
    assert next_run_at(now, 2, 0) == dt.datetime(2026, 9, 9, 2, 0, tzinfo=IST)


def test_the_schedule_targets_a_wall_clock_time_not_an_interval():
    """A 24-hour interval drifts by however long each cycle takes, which moves
    the observation to a different time of day. Fares move intraday, so that
    drift would be a measurement artefact."""
    slow_start = dt.datetime(2026, 9, 8, 2, 40, tzinfo=IST)   # cycle overran
    assert seconds_until(slow_start, 2, 0) == pytest.approx(23 * 3600 + 20 * 60)


def test_a_failing_cycle_does_not_kill_the_schedule():
    """A scheduler that dies on one bad night is worse than none: the failure is
    silent from the next morning onwards."""
    calls = []

    async def cycle(day):
        calls.append(day)
        raise RuntimeError("collection blew up")

    sched = DailySchedule(
        max_cycles=3,
        sleep=lambda _s: asyncio.sleep(0),
        now=lambda: dt.datetime(2026, 9, 8, 1, 0, tzinfo=IST),
    )
    out = asyncio.run(sched.run(cycle))
    assert len(calls) == 3
    assert all(isinstance(o, RuntimeError) for o in out)


# ==========================================================================
# Backfill: the honest part
# ==========================================================================

def test_a_future_date_is_refused():
    from apix.orchestration import backfill as bf

    plan = build_plan(BASKET, ["akasa"], dt.date(2026, 9, 20))
    with pytest.raises(bf.NotBackfillable):
        bf.assess(plan, today=TODAY, conn_factory=_no_db)


def test_today_is_recoverable_and_the_past_is_not():
    """The central fact. A fare quoted three days ago does not exist anywhere
    now; re-running the matrix would collect a shorter-window price and file it
    under the old date, biasing that day upward."""
    from apix.orchestration.backfill import Assessment

    plan = build_plan(BASKET, ["akasa"], TODAY)
    today_case = Assessment(TODAY, TODAY, plan.cells, (), plan.cells)
    past_case = Assessment(TODAY - dt.timedelta(days=3), TODAY, plan.cells, (), plan.cells)
    assert today_case.recoverable
    assert not past_case.recoverable
    assert past_case.age_days == 3
    assert "NOT RECOVERABLE" in past_case.describe()
    assert "RECOVERABLE" in today_case.describe()


def test_a_complete_day_reports_nothing_to_do():
    from apix.orchestration.backfill import Assessment

    plan = build_plan(BASKET, ["akasa"], TODAY)
    a = Assessment(TODAY, TODAY, plan.cells, plan.cells, ())
    assert "nothing to do" in a.describe()


def _no_db(*_a, **_k):
    raise AssertionError("this test must not touch the database")
