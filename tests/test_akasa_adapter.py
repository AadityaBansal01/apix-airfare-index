"""Akasa adapter tests.

Parsing is pure, so recorded payloads exercise it completely with no network.
The fetch path is exercised through the real PolicyEnforcedSession with a fake
transport, which proves the adapter genuinely cannot reach a URL policy forbids.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from apix.ethics.audit import NullAuditWriter
from apix.net.identity import Identity, IdentityMode
from apix.net.session import PolicyEnforcedSession, PolicyRefused, SourcePolicy
from apix.settings import Settings
from apix.sources.akasa import AkasaAirSource
from apix.sources.base import NoFlights, ParseError, SearchRequest, SoldOut, Tier

pytestmark = pytest.mark.asyncio

SCRAPE = dt.date(2026, 9, 4)
DEPART = dt.date(2026, 9, 11)
REQ = SearchRequest("DEL", "BOM", DEPART, SCRAPE)

AKASA_ROBOTS = b"User-Agent: *\nSitemap: https://www.akasaair.com/sitemap.xml\n"


class Res:
    def __init__(self, json=None, text=""):
        self.json = json
        self.text = text
        self.url = ""
        self.status = 200
        self.body = text.encode()
        self.request_id = 1


# --------------------------------------------------------------------------
# Pure URL construction
# --------------------------------------------------------------------------

async def test_urls():
    s = AkasaAirSource.__new__(AkasaAirSource)
    assert s.landing_url(REQ) == "https://www.akasaair.com/flight-booking/delhi-to-mumbai"
    u = s.build_search_url(REQ)
    assert "origin=DEL" in u and "destination=BOM" in u and "departureDate=2026-09-11" in u


async def test_unknown_city_raises_rather_than_guessing():
    s = AkasaAirSource.__new__(AkasaAirSource)
    with pytest.raises(ParseError, match="no Akasa city slug"):
        s.landing_url(SearchRequest("DEL", "ZZZ", DEPART, SCRAPE))


async def test_tier_gate_blocks_excluded_sources():
    """A tier 3 or 4 source cannot even be constructed."""
    class Blocked(AkasaAirSource):
        code = "indigo"
        tier = Tier.ROBOTS_DENIED
    from apix.sources.base import BlockedByPolicy
    with pytest.raises(BlockedByPolicy, match="tier 4"):
        Blocked(ctx=None)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

async def test_parses_full_component_split():
    payload = {"trips": [{"journeys": [{
        "flightNumber": "1512", "departureTime": "2026-09-11T06:20:00",
        "stops": 0, "fareType": "SAVER",
        "baseFare": 4200, "taxAmount": 520, "udf": 180,
        "convenienceFee": 100, "totalAmount": 5000,
    }]}]}
    s = AkasaAirSource.__new__(AkasaAirSource)
    quotes = s.parse(REQ, Res(json=payload))
    assert len(quotes) == 1
    q = quotes[0]
    assert q.carrier == "QP"
    assert q.total_fare == Decimal("5000")
    assert q.base_fare == Decimal("4200")
    assert q.components_complete is True
    assert q.window_days == 7
    assert q.flight_number == "QP1512"


async def test_taxes_derived_as_residual_only_when_it_reconciles():
    payload = {"j": [{"flightNumber": "QP1101", "stops": 0,
                      "baseFare": 4200, "udf": 180, "convenienceFee": 100,
                      "totalAmount": 5000}]}
    s = AkasaAirSource.__new__(AkasaAirSource)
    q = s.parse(REQ, Res(json=payload))[0]
    assert q.taxes == Decimal("520")     # 5000 - 4200 - 180 - 100
    assert q.components_complete is True


async def test_negative_residual_drops_the_split_instead_of_fabricating():
    """Components exceeding the total means we misread something. Publish no
    split rather than an invented one."""
    payload = {"j": [{"flightNumber": "QP1", "stops": 0,
                      "baseFare": 9000, "udf": 180, "totalAmount": 5000}]}
    s = AkasaAirSource.__new__(AkasaAirSource)
    q = s.parse(REQ, Res(json=payload))[0]
    assert q.base_fare is None
    assert q.components_complete is False
    assert "no_component_split" in q.quality_flags
    assert q.total_fare == Decimal("5000")


async def test_connecting_itineraries_excluded():
    payload = {"j": [
        {"flightNumber": "QP1", "stops": 0, "totalAmount": 5000},
        {"flightNumber": "QP2", "stops": 1, "totalAmount": 3000},
    ]}
    s = AkasaAirSource.__new__(AkasaAirSource)
    quotes = s.parse(REQ, Res(json=payload))
    assert [q.flight_number for q in quotes] == ["QP1"]


async def test_unrecognised_payload_raises_with_observed_keys():
    """The parser must never guess; the error has to name what it saw."""
    s = AkasaAirSource.__new__(AkasaAirSource)
    with pytest.raises(ParseError, match="Observed keys"):
        s.parse(REQ, Res(json={"weird": {"unexpected": "shape"}}))


async def test_sold_out_and_no_flights_are_distinct():
    s = AkasaAirSource.__new__(AkasaAirSource)
    with pytest.raises(SoldOut):
        s.parse(REQ, Res(text="<div>No seats available on this flight</div>"))
    with pytest.raises(NoFlights):
        s.parse(REQ, Res(text="<div>No flights found for this route</div>"))


async def test_money_parsing_handles_rupee_strings():
    payload = {"j": [{"flightNumber": "QP1", "stops": 0,
                      "totalAmount": "₹ 5,000", "baseFare": "4,200",
                      "taxAmount": "800"}]}
    s = AkasaAirSource.__new__(AkasaAirSource)
    q = s.parse(REQ, Res(json=payload))[0]
    assert q.total_fare == Decimal("5000")
    assert q.base_fare == Decimal("4200")


# --------------------------------------------------------------------------
# Adapter through the real session
# --------------------------------------------------------------------------

async def test_adapter_probe_goes_through_policy(fake_transport, instant_throttler):
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    fake_transport.add("https://www.akasaair.com/flight-booking/delhi-to-mumbai",
                       body=b"<html>route page</html>")
    audit = NullAuditWriter()
    session = PolicyEnforcedSession(
        audit, identity=Identity(IdentityMode.IDENTIFIED),
        throttler=instant_throttler, transport=fake_transport,
        policies={"akasa": SourcePolicy("akasa", 8.0, 0.0,
                                        ("/payment", "/checkout"))},
        settings=Settings(store_raw_bodies=False, max_retries=0),
    )
    src = AkasaAirSource(session)
    assert await src.probe_route_served(REQ) is True
    assert any(a.source_code == "akasa" and a.outcome == "ok" for a in audit.records)


async def test_adapter_cannot_reach_an_intent_denied_path(
    fake_transport, instant_throttler
):
    fake_transport.add("https://www.akasaair.com/robots.txt", body=AKASA_ROBOTS)
    audit = NullAuditWriter()
    session = PolicyEnforcedSession(
        audit, identity=Identity(IdentityMode.IDENTIFIED),
        throttler=instant_throttler, transport=fake_transport,
        policies={"akasa": SourcePolicy("akasa", 8.0, 0.0, ("/payment",))},
        settings=Settings(store_raw_bodies=False, max_retries=0),
    )
    with pytest.raises(PolicyRefused):
        await session.get("https://www.akasaair.com/payment/confirm",
                          source_code="akasa")
    assert "https://www.akasaair.com/payment/confirm" not in fake_transport.requests
