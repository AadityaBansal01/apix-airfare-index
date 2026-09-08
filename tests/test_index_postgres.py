"""Index engine integration tests against real PostgreSQL.

Exercises the full chain: seeded fares -> cleaning -> base prices -> elementary
indices -> headline -> temporal aggregates, all through the repository.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apix.index.aggregate import cell_weights
from apix.index.build import (
    aggregate_period,
    build_base_prices,
    build_elementary,
    build_index,
    week_start,
)
from apix.index.repository import IndexRepository
from apix.pipeline.clean import clean_quotes
from apix.pipeline.persist import Persister
from apix.seed import generate_range

D = Decimal
PRICE_REF_START = dt.date(2026, 7, 1)
PRICE_REF_END = dt.date(2026, 7, 14)
WEIGHT_REF_START = dt.date(2025, 6, 1)

ROUTES = [("BLR", "DEL"), ("BOM", "DEL")]
WINDOWS = [7, 30]


def _db_available() -> bool:
    try:
        from apix import db
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM apix.route_weight LIMIT 1")
            return cur.fetchone() is not None
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(), reason="postgres not reachable; run `make reference`")


@pytest.fixture
def seeded():
    """A short seeded window, cleaned and persisted through the real pipeline."""
    from apix import db

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM apix.apix_route_index")
        cur.execute("DELETE FROM apix.apix_index")
        cur.execute("DELETE FROM apix.elementary_index")
        cur.execute("DELETE FROM apix.base_price")
        cur.execute("DELETE FROM apix.fare")
        cur.execute("DELETE FROM apix.raw_quote")
        cur.execute("DELETE FROM apix.request_audit")
        cur.execute("SELECT source_id FROM apix.source WHERE code='seed'")
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO apix.source (code, display_name, base_url, kind, tier,"
                " enabled, exclusion_reason, audit_verdict, audited_on) "
                "VALUES ('seed','Seeded','-','official',1,FALSE,'test',"
                "'scrape_ok',%s) RETURNING source_id", (dt.date.today(),))
            row = cur.fetchone()
        sid = row["source_id"]
        cur.execute(
            "INSERT INTO apix.request_audit (source_id, url, user_agent, "
            "robots_allowed, delay_applied_s, outcome) "
            "VALUES (%s,'seed://t','t',TRUE,0.0,'ok') RETURNING request_id", (sid,))
        request_id = cur.fetchone()["request_id"]

    p = Persister()
    end = PRICE_REF_END + dt.timedelta(days=5)
    for day, quotes in generate_range(PRICE_REF_START, end, ROUTES, WINDOWS,
                                      seed=99):
        result = clean_quotes(quotes)
        with p.batch():
            for origin, destination in ROUTES:
                for window in WINDOWS:
                    subset = [q for q in result.persistable
                              if q.origin == origin and q.destination == destination
                              and q.window_days == window]
                    if not subset:
                        continue
                    raw_id = p.save_raw(
                        request_id=request_id, source_code="seed",
                        origin=origin, destination=destination,
                        departure_date=day + dt.timedelta(days=window),
                        scrape_date=day, payload={"n": len(subset)},
                        parser_version="seed-v1")
                    if raw_id:
                        p.save_fares(raw_id, subset)
    return IndexRepository()


def test_repository_reads_prices_grouped_by_cell(seeded):
    prices = seeded.index_prices(PRICE_REF_START)
    assert prices
    for (route_id, window), carriers in prices.items():
        assert window in WINDOWS
        assert set(carriers) <= {"QP", "SG"}
        assert all(v > 0 for v in carriers.values())


def test_base_prices_persist_and_reload(seeded):
    period = seeded.prices_over_period(PRICE_REF_START, PRICE_REF_END)
    base = build_base_prices(period, min_observations=5)
    counts = {k: len(v) for k, v in period.items()}
    n = seeded.save_base_prices(base, counts, PRICE_REF_START, PRICE_REF_END)
    assert n > 0
    reloaded = seeded.base_prices(PRICE_REF_START)
    assert set(reloaded) == set(base)
    for k, v in base.items():
        assert abs(reloaded[k] - v) < D("0.01")


def test_index_is_near_100_within_the_price_reference_period(seeded):
    """The defining property of the base period.

    Base prices are the geometric mean of daily prices across the reference
    window, so the index averaged over that same window must sit at 100. Any
    material drift means base prices and index prices disagree about which
    observations they are built from.
    """
    period = seeded.prices_over_period(PRICE_REF_START, PRICE_REF_END)
    base = build_base_prices(period, min_observations=5)
    seeded.save_base_prices(base, {k: len(v) for k, v in period.items()},
                            PRICE_REF_START, PRICE_REF_END)

    route_w = seeded.route_weights(WEIGHT_REF_START)
    window_w = {w: D("0.5") for w in WINDOWS}
    route_w = {r: w for r, w in route_w.items()
               if any((r, win) in {(k[0], k[1]) for k in base} for win in WINDOWS)}
    total = sum(route_w.values())
    route_w = {r: w / total for r, w in route_w.items()}
    weights = cell_weights(route_w, window_w)

    values = []
    day = PRICE_REF_START
    while day <= PRICE_REF_END:
        elem = build_elementary(day, seeded.index_prices(day), base, list(weights))
        usable = {c: o for c, o in elem.items() if o.index_value is not None}
        if usable:
            values.append(build_index(day, elem, weights, route_w).index_value)
        day += dt.timedelta(days=1)

    assert len(values) >= 10
    mean = sum(values) / Decimal(len(values))
    assert abs(mean - D("100")) < D("2.0"), f"reference-period mean was {mean}"


def test_full_build_persists_all_three_tables(seeded):
    from apix import db

    period = seeded.prices_over_period(PRICE_REF_START, PRICE_REF_END)
    base = build_base_prices(period, min_observations=5)
    seeded.save_base_prices(base, {k: len(v) for k, v in period.items()},
                            PRICE_REF_START, PRICE_REF_END)

    route_w = seeded.route_weights(WEIGHT_REF_START)
    window_w = {w: D("0.5") for w in WINDOWS}
    weights = cell_weights(route_w, window_w)

    day = PRICE_REF_END + dt.timedelta(days=1)
    elem = build_elementary(day, seeded.index_prices(day), base, list(weights))
    build = build_index(day, elem, weights, route_w,
                        carrier_share={"QP": D("0.0526"), "SG": D("0.0303")})
    seeded.save_build(build, WEIGHT_REF_START, PRICE_REF_START)

    head = db.fetch_one(
        "SELECT * FROM apix.apix_index WHERE index_date=%s AND frequency='daily'",
        (day,))
    assert head is not None
    assert head["formula"] == "young_modified_laspeyres"
    assert float(head["observed_pax_share"]) == pytest.approx(0.0829, abs=1e-3)

    elem_rows = db.fetch_all(
        "SELECT * FROM apix.elementary_index WHERE index_date=%s", (day,))
    assert elem_rows
    assert all(r["formula"] == "jevons" for r in elem_rows)
    assert all(r["n_carriers"] >= 1 for r in elem_rows)

    routes = db.fetch_all(
        "SELECT * FROM apix.apix_route_index WHERE index_date=%s", (day,))
    assert len(routes) == len(build.route_values)


def test_rebuilding_the_same_day_is_idempotent(seeded):
    from apix import db

    period = seeded.prices_over_period(PRICE_REF_START, PRICE_REF_END)
    base = build_base_prices(period, min_observations=5)
    seeded.save_base_prices(base, {k: len(v) for k, v in period.items()},
                            PRICE_REF_START, PRICE_REF_END)
    route_w = seeded.route_weights(WEIGHT_REF_START)
    weights = cell_weights(route_w, {w: D("0.5") for w in WINDOWS})

    day = PRICE_REF_END + dt.timedelta(days=1)
    elem = build_elementary(day, seeded.index_prices(day), base, list(weights))
    build = build_index(day, elem, weights, route_w)

    for _ in range(3):
        seeded.save_build(build, WEIGHT_REF_START, PRICE_REF_START)

    n = db.fetch_one("SELECT count(*) AS n FROM apix.apix_index "
                     "WHERE index_date=%s AND frequency='daily'", (day,))["n"]
    assert n == 1


def test_weekly_aggregate_equals_mean_of_dailies(seeded):
    period = seeded.prices_over_period(PRICE_REF_START, PRICE_REF_END)
    base = build_base_prices(period, min_observations=5)
    route_w = seeded.route_weights(WEIGHT_REF_START)
    weights = cell_weights(route_w, {w: D("0.5") for w in WINDOWS})

    builds = []
    for offset in range(1, 6):
        day = PRICE_REF_END + dt.timedelta(days=offset)
        elem = build_elementary(day, seeded.index_prices(day), base, list(weights))
        if any(o.index_value is not None for o in elem.values()):
            builds.append(build_index(day, elem, weights, route_w))

    assert len(builds) >= 3
    weekly = aggregate_period(builds, "weekly", week_start(builds[0].index_date))
    expected = sum(b.index_value for b in builds) / Decimal(len(builds))
    assert abs(weekly.index_value - expected) < D("0.0001")


def test_last_known_and_imputed_history_round_trip(seeded):
    period = seeded.prices_over_period(PRICE_REF_START, PRICE_REF_END)
    base = build_base_prices(period, min_observations=5)
    seeded.save_base_prices(base, {k: len(v) for k, v in period.items()},
                            PRICE_REF_START, PRICE_REF_END)
    route_w = seeded.route_weights(WEIGHT_REF_START)
    weights = cell_weights(route_w, {w: D("0.5") for w in WINDOWS})

    day = PRICE_REF_END + dt.timedelta(days=1)
    elem = build_elementary(day, seeded.index_prices(day), base, list(weights))
    build = build_index(day, elem, weights, route_w)
    seeded.save_build(build, WEIGHT_REF_START, PRICE_REF_START)

    last = seeded.last_known_elementary(day + dt.timedelta(days=1))
    assert last
    hist = seeded.imputed_history(day, day)
    assert hist
    assert all(v is False for per_cell in hist.values() for v in per_cell.values())
