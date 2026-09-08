"""Integration tests for the audit writer against real PostgreSQL.

Skipped automatically when no database is reachable, so the suite still runs on a
laptop with nothing started. When it does run, it proves the thing that matters:
the database itself refuses to store a compliant-looking lie.
"""
from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from apix.ethics.audit import AuditRecord, PostgresAuditWriter
from apix.ethics.robots import RobotsSnapshot

pytestmark = pytest.mark.asyncio


def _db_available() -> bool:
    try:
        from apix import db
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM apix.source LIMIT 1")
            return cur.fetchone() is not None
    except Exception:
        return False


pytestmark = [pytest.mark.asyncio,
              pytest.mark.skipif(not _db_available(),
                                 reason="postgres not reachable; run `make db-up schema` "
                                        "and `python scripts/load_reference.py`")]


@pytest.fixture
def writer():
    return PostgresAuditWriter()


@pytest.fixture(autouse=True)
def clean():
    """Truncate in foreign-key order: fare -> raw_quote -> request_audit.

    raw_quote references request_audit and fare references raw_quote, so
    deleting the audit table first fails once any other test has left rows behind.
    """
    from apix import db
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM apix.fare")
        cur.execute("DELETE FROM apix.raw_quote")
        cur.execute("DELETE FROM apix.request_audit")
        cur.execute("DELETE FROM apix.robots_snapshot")
    yield


async def test_record_round_trips(writer):
    from apix import db
    rid = writer.record(AuditRecord(
        source_code="akasa",
        url="https://www.akasaair.com/flight-booking/delhi-to-mumbai",
        user_agent="APIx-Research-Bot/1.0",
        robots_allowed=True,
        delay_applied_s=8.4,
        outcome="ok",
        http_status=200,
        bytes_returned=4321,
        duration_ms=812,
    ))
    assert rid > 0
    row = db.fetch_one(
        "SELECT a.*, s.code FROM apix.request_audit a "
        "JOIN apix.source s USING (source_id) WHERE request_id = %s", (rid,))
    assert row["code"] == "akasa"
    assert row["outcome"] == "ok"
    assert float(row["delay_applied_s"]) == 8.4


async def test_blocked_request_is_storable(writer):
    rid = writer.record(AuditRecord(
        source_code="indigo",
        url="https://www.goindigo.in/booking/search",
        user_agent="APIx-Research-Bot/1.0",
        robots_allowed=False,
        delay_applied_s=0.0,
        outcome="blocked_by_robots",
        robots_rule="robots:disallow:APIx-Research-Bot",
    ))
    assert rid > 0


async def test_database_rejects_a_forged_compliant_fetch():
    """Bypass the Python validator and hit the CHECK constraint directly.

    This is the test that proves compliance is enforced by the schema and not
    merely by the well-behaved writer in front of it.
    """
    from apix import db
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT source_id FROM apix.source WHERE code='indigo'")
            sid = cur.fetchone()["source_id"]
            cur.execute(
                "INSERT INTO apix.request_audit "
                "(source_id, url, user_agent, robots_allowed, delay_applied_s, outcome) "
                "VALUES (%s,'https://www.goindigo.in/booking/search','UA',FALSE,6.0,'ok')",
                (sid,))
    assert "audit_no_disallowed_fetch" in str(exc.value)


async def test_database_rejects_inconsistent_intent_flag():
    from apix import db
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT source_id FROM apix.source WHERE code='spicejet'")
            sid = cur.fetchone()["source_id"]
            cur.execute(
                "INSERT INTO apix.request_audit "
                "(source_id, url, user_agent, robots_allowed, intent_denied, "
                " delay_applied_s, outcome) "
                "VALUES (%s,'https://www.spicejet.com/api/v1','UA',FALSE,TRUE,0.0,"
                "'blocked_by_robots')", (sid,))
    assert "audit_intent_consistent" in str(exc.value)


async def test_unregistered_source_cannot_write(writer):
    with pytest.raises(LookupError):
        writer.record(AuditRecord(
            source_code="some_new_ota",
            url="https://example.com/x",
            user_agent="UA", robots_allowed=True,
            delay_applied_s=6.0, outcome="ok",
        ))


async def test_snapshot_persists_with_hash(writer):
    from apix import db
    body = "User-Agent: *\nSitemap: https://www.akasaair.com/sitemap.xml\n"
    snap = RobotsSnapshot(
        robots_url="https://www.akasaair.com/robots.txt",
        body=body,
        sha256=hashlib.sha256(body.encode()).digest(),
        fetched_at=dt.datetime.now(dt.timezone.utc),
        http_status=200,
        crawl_delay=None,
    )
    sid = writer.save_snapshot("akasa", snap)
    assert sid > 0 and snap.snapshot_id == sid
    row = db.fetch_one(
        "SELECT * FROM apix.robots_snapshot WHERE snapshot_id = %s", (sid,))
    assert row["body"] == body
    assert bytes(row["body_sha256"]) == hashlib.sha256(body.encode()).digest()


async def test_compliance_view_summarises_the_trail(writer):
    from apix import db
    # First request to an origin records 0.0 (no predecessor to be spaced from);
    # the view excludes those zeros from min_delay_s.
    writer.record(AuditRecord("akasa", "https://www.akasaair.com/a", "UA",
                              True, 0.0, "ok", http_status=200))
    writer.record(AuditRecord("akasa", "https://www.akasaair.com/b", "UA",
                              True, 8.4, "ok", http_status=200))
    writer.record(AuditRecord("akasa", "https://www.akasaair.com/c", "UA",
                              True, 9.1, "ok", http_status=200))
    writer.record(AuditRecord("indigo", "https://www.goindigo.in/booking/x", "UA",
                              False, 0.0, "blocked_by_robots",
                              robots_rule="robots:disallow"))

    rows = {r["source"]: r for r in db.fetch_all("SELECT * FROM apix.v_compliance_summary")}
    assert rows["akasa"]["requests"] == 3
    assert rows["akasa"]["requests_sent"] == 3
    # The zero from the first request must not drag the minimum down.
    assert float(rows["akasa"]["min_delay_s"]) == 8.4
    assert float(rows["akasa"]["max_delay_s"]) == 9.1
    assert rows["indigo"]["robots_blocked"] == 1
    assert rows["indigo"]["audit_verdict"] == "do_not_scrape"


async def test_session_writes_a_real_trail(fake_transport, instant_throttler):
    """End to end: session -> PostgresAuditWriter -> queryable compliance view."""
    from apix import db
    from apix.net.identity import Identity, IdentityMode
    from apix.net.session import PolicyEnforcedSession, PolicyRefused, SourcePolicy
    from apix.settings import Settings

    fake_transport.add("https://www.akasaair.com/robots.txt",
                       body=b"User-Agent: *\nSitemap: https://x/s.xml\n")
    fake_transport.add("https://www.akasaair.com/flight-booking/delhi-to-mumbai",
                       body=b"<html>fares</html>")
    fake_transport.add("https://www.goindigo.in/robots.txt",
                       body=b"User-agent: *\nDisallow: /booking/\n")

    s = PolicyEnforcedSession(
        PostgresAuditWriter(),
        identity=Identity(IdentityMode.IDENTIFIED),
        throttler=instant_throttler,
        transport=fake_transport,
        policies={"akasa": SourcePolicy("akasa", 8.0, 0.0)},
        settings=Settings(store_raw_bodies=False, max_retries=0),
    )

    r = await s.get("https://www.akasaair.com/flight-booking/delhi-to-mumbai",
                    source_code="akasa")
    assert r.status == 200

    with pytest.raises(PolicyRefused):
        await s.get("https://www.goindigo.in/booking/search", source_code="indigo")

    summary = {row["source"]: row
               for row in db.fetch_all("SELECT * FROM apix.v_compliance_summary")}
    assert summary["akasa"]["requests"] == 1
    assert summary["indigo"]["robots_blocked"] == 1
    assert summary["__robots__"]["requests"] == 2   # both robots.txt fetches

    snaps = db.fetch_all("SELECT * FROM apix.robots_snapshot ORDER BY snapshot_id")
    assert len(snaps) == 2
