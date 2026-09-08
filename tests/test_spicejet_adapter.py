"""SpiceJet adapter tests.

Mirrors tests/test_akasa_adapter.py, plus the case specific to this source: the
three malformed absolute-URL Disallow lines in its robots.txt, which a
conforming parser ignores and which we obey anyway.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apix.ethics.audit import NullAuditWriter
from apix.net.identity import Identity, IdentityMode
from apix.net.session import PolicyEnforcedSession, PolicyRefused, SourcePolicy
from apix.settings import Settings
from apix.sources.base import NoFlights, ParseError, SearchRequest, SoldOut, Tier
from apix.sources.spicejet import CARRIER, SpiceJetSource

pytestmark = pytest.mark.asyncio

SCRAPE = dt.date(2026, 9, 4)
DEPART = dt.date(2026, 9, 11)
REQ = SearchRequest("DEL", "BOM", DEPART, SCRAPE)

# The real file, from data/raw/robots_snapshot/robots_www.spicejet.com.txt.
SPICEJET_ROBOTS = b"""Disallow:
User-agent: *
Disallow:
Disallow: /cgi-bin/
Disallow: https://www.spicejet.com/api/v1
Disallow: https://www.spicejet.com/public/
Disallow: https://www.spicejet.com/externalBooking
Sitemap: https://www.spicejet.com/sitemap.xml
"""


class Res:
    def __init__(self, json=None, text=""):
        self.json = json
        self.text = text
        self.url = ""
        self.status = 200
        self.body = text.encode()
        self.request_id = 1


def src() -> SpiceJetSource:
    return SpiceJetSource.__new__(SpiceJetSource)


# ==========================================================================
# Identity and policy
# ==========================================================================

async def test_declared_as_tier_1_with_a_deliberate_delay():
    assert SpiceJetSource.tier is Tier.PRIMARY
    assert SpiceJetSource.min_delay_seconds >= 8.0
    assert CARRIER == "SG"


async def test_intent_rules_cover_the_malformed_disallow_lines():
    """RFC 9309 requires a path, so a conforming parser ignores all three of
    SpiceJet's absolute-URL Disallow lines and the site reads as fully open.
    The operator's intent is unmistakable, so we encode it as paths."""
    denied = SpiceJetSource.intent_denied_paths
    for path in ("/api/v1", "/public/", "/externalBooking"):
        assert path in denied, f"{path} must be refused despite being malformed"


async def test_intent_rules_also_cover_payment_and_booking():
    """Ours, not theirs. Fare collection has no business near a payment flow."""
    denied = SpiceJetSource.intent_denied_paths
    assert "/payment" in denied and "/manage-booking" in denied


async def test_the_stdlib_would_permit_what_we_refuse():
    """Documents exactly why the intent rules exist, against the real file."""
    import urllib.robotparser

    p = urllib.robotparser.RobotFileParser()
    p.parse(SPICEJET_ROBOTS.decode().splitlines())
    assert p.can_fetch("APIx-Research-Bot", "https://www.spicejet.com/api/v1/x"), (
        "expected the stdlib to permit this; if it now refuses, the intent "
        "rules are still correct but this test's premise has changed")


# ==========================================================================
# URL construction
# ==========================================================================

async def test_search_url_carries_the_cell():
    u = src().build_search_url(REQ)
    assert "origin=DEL" in u
    assert "destination=BOM" in u
    assert "departureDate=2026-09-11" in u
    assert u.startswith("https://www.spicejet.com/")


# ==========================================================================
# Parsing
# ==========================================================================

async def test_parses_a_full_component_split():
    payload = {"trips": [{"journeys": [{
        "flightNumber": "8721", "departureTime": "2026-09-11T07:40:00",
        "stops": 0, "fareType": "SAVER",
        "baseFare": 4100, "taxAmount": 560, "udf": 200,
        "convenienceFee": 140, "totalAmount": 5000,
    }]}]}
    q = src().parse(REQ, Res(json=payload))[0]
    assert q.carrier == "SG"
    assert q.flight_number == "SG8721"
    assert q.total_fare == Decimal("5000")
    assert q.base_fare == Decimal("4100")
    assert q.components_complete is True
    assert q.window_days == 7
    assert q.source_code == "spicejet"


async def test_taxes_derived_as_residual_when_they_reconcile():
    payload = {"j": [{"flightNumber": "SG101", "stops": 0, "baseFare": 4100,
                      "udf": 200, "convenienceFee": 140, "totalAmount": 5000}]}
    q = src().parse(REQ, Res(json=payload))[0]
    assert q.taxes == Decimal("560")
    assert q.components_complete is True


async def test_impossible_split_is_dropped_not_invented():
    payload = {"j": [{"flightNumber": "SG1", "stops": 0,
                      "baseFare": 9000, "udf": 200, "totalAmount": 5000}]}
    q = src().parse(REQ, Res(json=payload))[0]
    assert q.base_fare is None
    assert q.components_complete is False
    assert "no_component_split" in q.quality_flags
    assert q.total_fare == Decimal("5000"), "the observed total must survive"


async def test_connecting_itineraries_excluded():
    payload = {"j": [
        {"flightNumber": "SG1", "stops": 0, "totalAmount": 5000},
        {"flightNumber": "SG2", "stops": 1, "totalAmount": 3200},
    ]}
    assert [q.flight_number for q in src().parse(REQ, Res(json=payload))] == ["SG1"]


async def test_rupee_and_indian_grouping_are_parsed():
    payload = {"j": [{"flightNumber": "SG1", "stops": 0,
                      "totalAmount": "₹ 1,23,456", "baseFare": "1,00,000",
                      "taxAmount": "23,456"}]}
    q = src().parse(REQ, Res(json=payload))[0]
    assert q.total_fare == Decimal("123456")
    assert q.base_fare == Decimal("100000")


async def test_duplicate_flight_entries_collapse():
    node = {"flightNumber": "SG1", "departureTime": "07:40", "stops": 0,
            "totalAmount": 5000}
    payload = {"a": [node], "b": [dict(node)]}
    assert len(src().parse(REQ, Res(json=payload))) == 1


async def test_sold_out_and_no_flights_are_distinct():
    s = src()
    with pytest.raises(SoldOut):
        s.parse(REQ, Res(text="<div>No seats available on this flight</div>"))
    with pytest.raises(NoFlights):
        s.parse(REQ, Res(text="<div>No flights found for this route</div>"))


async def test_unrecognised_payload_names_what_it_saw():
    with pytest.raises(ParseError, match="Observed keys"):
        src().parse(REQ, Res(json={"weird": {"unexpected": "shape"}}))


async def test_zero_and_negative_totals_are_ignored():
    payload = {"j": [{"flightNumber": "SG1", "stops": 0, "totalAmount": 0},
                     {"flightNumber": "SG2", "stops": 0, "totalAmount": -100},
                     {"flightNumber": "SG3", "stops": 0, "totalAmount": 4900}]}
    quotes = src().parse(REQ, Res(json=payload))
    assert [q.flight_number for q in quotes] == ["SG3"]


# ==========================================================================
# Through the real session
# ==========================================================================

def _session(fake_transport, instant_throttler):
    audit = NullAuditWriter()
    session = PolicyEnforcedSession(
        audit,
        identity=Identity(IdentityMode.IDENTIFIED),
        throttler=instant_throttler,
        transport=fake_transport,
        policies={"spicejet": SourcePolicy(
            "spicejet", 8.0, 0.0, SpiceJetSource.intent_denied_paths)},
        settings=Settings(store_raw_bodies=False, max_retries=0),
    )
    return session, audit


async def test_adapter_can_reach_an_allowed_page(fake_transport, instant_throttler):
    fake_transport.add("https://www.spicejet.com/robots.txt", body=SPICEJET_ROBOTS)
    fake_transport.add("https://www.spicejet.com/about-us", body=b"<html>ok</html>")
    session, audit = _session(fake_transport, instant_throttler)
    SpiceJetSource(session)                       # tier gate must permit it

    r = await session.get("https://www.spicejet.com/about-us",
                          source_code="spicejet")
    assert r.status == 200
    assert any(a.outcome == "ok" and a.source_code == "spicejet"
               for a in audit.records)


@pytest.mark.parametrize("path", ["/api/v1/search", "/public/x", "/externalBooking"])
async def test_malformed_disallow_paths_are_never_sent(
    path, fake_transport, instant_throttler
):
    """The load-bearing test for this source: the stdlib would allow these, and
    they must still never leave the process."""
    fake_transport.add("https://www.spicejet.com/robots.txt", body=SPICEJET_ROBOTS)
    fake_transport.add(f"https://www.spicejet.com{path}", body=b"fares!")
    session, audit = _session(fake_transport, instant_throttler)

    with pytest.raises(PolicyRefused) as exc:
        await session.get(f"https://www.spicejet.com{path}", source_code="spicejet")
    assert exc.value.intent_denied is True
    assert f"https://www.spicejet.com{path}" not in fake_transport.requests
    assert any(a.outcome == "blocked_by_intent" for a in audit.records)


async def test_config_enables_spicejet_at_tier_1():
    """The adapter is written, so the registry must now allow it to run."""
    from apix.net.session import load_policies
    import yaml

    cfg = yaml.safe_load(open("config/sources.yaml"))
    entry = next(s for s in cfg["sources"] if s["code"] == "spicejet")
    assert entry["enabled"] is True
    assert entry["tier"] == 1
    assert entry.get("exclusion_reason") in (None, "")

    policies = load_policies("config/sources.yaml")
    assert "spicejet" in policies
    assert policies["spicejet"].min_delay_seconds >= 8.0
    for p in ("/api/v1", "/public/", "/externalBooking"):
        assert p in policies["spicejet"].intent_denied_paths
