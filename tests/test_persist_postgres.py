"""Persistence integration tests against real PostgreSQL.

Proves that cleaned fares land correctly, that re-running a scrape is idempotent,
and that the schema still refuses a fabricated component split even when the
Python layer is bypassed.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apix.pipeline.clean import clean_quotes
from apix.pipeline.persist import Persister, ReferenceMissing
from apix.sources.base import FareQuote

D = Decimal
SCRAPE = dt.date(2026, 9, 4)
DEPART = dt.date(2026, 9, 11)


def _db_available() -> bool:
    try:
        from apix import db
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM apix.route LIMIT 1")
            return cur.fetchone() is not None
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_available(),
    reason="postgres not reachable; run `make reference`")


def q(total, *, carrier="QP", flight="QP1101", base=None, taxes=None,
      udf=None, conv=None, sold_out=False) -> FareQuote:
    return FareQuote(
        source_code="akasa", origin="DEL", destination="BOM", carrier=carrier,
        departure_date=DEPART, scrape_date=SCRAPE, total_fare=D(str(total)),
        flight_number=flight, fare_brand="SAVER",
        base_fare=D(str(base)) if base is not None else None,
        taxes=D(str(taxes)) if taxes is not None else None,
        udf=D(str(udf)) if udf is not None else None,
        convenience_fee=D(str(conv)) if conv is not None else None,
        is_sold_out=sold_out,
    )


@pytest.fixture
def clean_db():
    from apix import db
    with db.connection() as conn, conn.cursor() as cur:
        # Foreign-key order: fare -> raw_quote -> request_audit.
        cur.execute("DELETE FROM apix.fare")
        cur.execute("DELETE FROM apix.raw_quote")
        cur.execute("DELETE FROM apix.request_audit")
        cur.execute("DELETE FROM apix.robots_snapshot")
    yield


@pytest.fixture
def raw_id(clean_db):
    """A raw_quote row for fares to hang off, plus its audit parent."""
    from apix import db
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT source_id FROM apix.source WHERE code='akasa'")
        sid = cur.fetchone()["source_id"]
        cur.execute(
            "INSERT INTO apix.request_audit (source_id, url, user_agent, "
            "robots_allowed, delay_applied_s, outcome) "
            "VALUES (%s,'https://www.akasaair.com/x','UA',TRUE,8.0,'ok') "
            "RETURNING request_id", (sid,))
        rid = cur.fetchone()["request_id"]
    p = Persister()
    return p.save_raw(
        request_id=rid, source_code="akasa", origin="DEL", destination="BOM",
        departure_date=DEPART, scrape_date=SCRAPE,
        payload={"journeys": [{"totalAmount": 5000}]}, parser_version="akasa-v1")


def test_raw_quote_is_stored_and_window_is_derived(raw_id):
    from apix import db
    row = db.fetch_one("SELECT * FROM apix.raw_quote WHERE raw_id = %s", (raw_id,))
    assert row["window_days"] == 7
    assert row["parser_version"] == "akasa-v1"


def test_raw_insert_is_idempotent(raw_id):
    """Re-running a scrape must not duplicate. A series that changes on re-run
    is not reproducible."""
    from apix import db
    p = Persister()
    again = p.save_raw(
        request_id=db.fetch_one("SELECT request_id FROM apix.request_audit LIMIT 1")["request_id"],
        source_code="akasa", origin="DEL", destination="BOM",
        departure_date=DEPART, scrape_date=SCRAPE,
        payload={"journeys": [{"totalAmount": 5000}]}, parser_version="akasa-v1")
    assert again is None
    n = db.fetch_one("SELECT count(*) AS n FROM apix.raw_quote")["n"]
    assert n == 1


def test_cleaned_fares_persist_with_component_split(raw_id):
    from apix import db
    result = clean_quotes([q(5000, base=4200, taxes=520, udf=180, conv=100)])
    written = Persister().save_result(raw_id, result)
    assert written == 1
    row = db.fetch_one("SELECT * FROM apix.fare LIMIT 1")
    assert row["total_fare"] == D("5000.00")
    assert row["base_fare"] == D("4200.00")
    assert row["udf"] == D("180.00")
    assert row["components_complete"] is True
    assert row["route_id"] is not None


def test_unreconciling_split_persists_as_total_only(raw_id):
    from apix import db
    result = clean_quotes([q(5000, base=4200, taxes=520, udf=180, conv=900)])
    Persister().save_result(raw_id, result)
    row = db.fetch_one("SELECT * FROM apix.fare LIMIT 1")
    assert row["total_fare"] == D("5000.00")
    assert row["base_fare"] is None
    assert row["components_complete"] is False
    assert "components_did_not_reconcile" in row["quality_flags"]


def test_flagged_outlier_persists_with_method_and_score(raw_id):
    from apix import db
    quotes = [q(f, flight=f"QP{i}") for i, f in
              enumerate([4800, 5100, 5400, 5900, 6200, 60000])]
    result = clean_quotes(quotes)
    Persister().save_result(raw_id, result)

    flagged = db.fetch_all("SELECT * FROM apix.fare WHERE is_outlier")
    assert len(flagged) == 1
    assert flagged[0]["total_fare"] == D("60000.00")
    assert flagged[0]["outlier_method"] == "mad_log"
    assert abs(float(flagged[0]["outlier_score"])) > 3.5
    # All six rows survive; only the flag differs.
    assert db.fetch_one("SELECT count(*) AS n FROM apix.fare")["n"] == 6


def test_index_cell_view_excludes_flagged_and_sold_out(raw_id):
    from apix import db
    quotes = [q(f, flight=f"QP{i}") for i, f in
              enumerate([4800, 5100, 5400, 5900, 6200, 60000])]
    quotes.append(q(4000, flight="QPX", sold_out=True))
    Persister().save_result(raw_id, clean_quotes(quotes))

    usable = db.fetch_all(
        "SELECT * FROM apix.fare WHERE NOT is_outlier AND NOT is_sold_out")
    assert len(usable) == 5
    assert min(r["total_fare"] for r in usable) == D("4800.00")


def test_unknown_route_raises_a_useful_error(raw_id):
    bad = q(5000)
    bad.origin, bad.destination = "DEL", "GOI"
    with pytest.raises(ReferenceMissing, match="not in the basket"):
        Persister().save_fares(raw_id, [bad])


def test_database_rejects_a_fabricated_component_split(raw_id):
    """Bypass the cleaner and insert a split that does not add up."""
    from apix import db
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT source_id FROM apix.source WHERE code='akasa'")
            sid = cur.fetchone()["source_id"]
            cur.execute("SELECT route_id FROM apix.route WHERE origin='BOM' AND destination='DEL'")
            rid = cur.fetchone()["route_id"]
            cur.execute(
                "INSERT INTO apix.fare (raw_id, source_id, route_id, origin, "
                "destination, carrier, scrape_date, departure_date, window_days, "
                "base_fare, taxes, total_fare, components_complete, cleaner_version) "
                "VALUES (%s,%s,%s,'DEL','BOM','QP',%s,%s,7,1000,200,9999,TRUE,'v1')",
                (raw_id, sid, rid, SCRAPE, DEPART))
    assert "fare_components_reconcile" in str(exc.value)


def test_leadtime_view_aggregates_usable_fares(raw_id):
    from apix import db
    quotes = [q(f, flight=f"QP{i}") for i, f in enumerate([4800, 5200, 6000])]
    Persister().save_result(raw_id, clean_quotes(quotes))
    rows = db.fetch_all("SELECT * FROM apix.v_leadtime_curve")
    assert len(rows) == 1
    assert rows[0]["route_code"] == "BOM-DEL"
    assert rows[0]["n"] == 3
    assert rows[0]["min_fare"] == D("4800.00")
