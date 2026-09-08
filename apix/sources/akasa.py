"""Akasa Air (QP) — the reference FareSource adapter.

WHY THIS SOURCE FIRST
---------------------
From docs/SOURCE_AUDIT.md, audited 2026-09-04. Akasa's robots.txt is, in full:

    User-Agent: *
    Sitemap: https://www.akasaair.com/sitemap.xml
    Sitemap: https://www.akasaair.com/book-flight-tickets/sitemap_index.xml

There is no Disallow directive. Nothing on the site is off limits to any crawler.
A homepage fetch returned `server: Webserver` with no CDN bot-management layer and
none of the Akamai Bot Manager cookies (`ak_bmsc`, `bm_s`, `bm_so`) that IndiGo,
Air India, MakeMyTrip and Yatra all set on first contact.

So Akasa is the one source in the problem statement where we can collect fares at
a deliberate, low rate without evading any control the operator has put up. That
is the entire reason it is adapter number one, ahead of larger carriers. IndiGo
would give better coverage and is explicitly disallowed:
`Disallow: /booking/*`.

EXTRACTION STRATEGY
-------------------
Two paths, tried in order, both robots-permitted:

  1.  **Route landing page** (`/flight-booking/<origin>-to-<destination>`).
      A server-rendered SEO page carrying an indicative lowest fare. One cheap
      GET, no booking-engine interaction. Used to detect whether the carrier
      serves the route at all before spending a browser render on it.

  2.  **Booking search** via Playwright, intercepting the availability JSON the
      SPA fetches rather than scraping the DOM. Reading the response the page
      already requested is more robust than CSS selectors and costs the origin
      exactly the same -- we issue no request the browser would not have issued.

Path 2 is what produces index-grade quotes, because only the availability payload
carries the base/tax/UDF decomposition the index needs.

A NOTE ON WHAT IS VERIFIED
--------------------------
The robots.txt, the absent bot-management layer, and the `/flight-booking/...`
URL shape are confirmed by live fetches recorded in `data/raw/robots_snapshot/`.
The **field names** inside the availability JSON (`_JSON_FIELD_ALIASES` below) are
written defensively against the common shapes of this booking-engine family and
are NOT yet confirmed against a live availability response -- doing so means
issuing real fare searches, which belongs in a rate-limited scheduled run, not in
adapter authoring. `parse_availability_json` therefore raises `ParseError` with
the observed key set rather than guessing, and the first live run will either
confirm the aliases or produce one precise error naming exactly what to add.
That is the honest state of this file and it is written down rather than implied.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import urllib.parse
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from apix.sources import parsing as P
from apix.sources.base import (
    FareQuote,
    FareSource,
    FetchContext,
    FetchResult,
    NoFlights,
    ParseError,
    SearchRequest,
    SoldOut,
    SourceKind,
    Tier,
)

log = logging.getLogger(__name__)

CARRIER = "QP"

#: IATA -> the slug Akasa uses in its route landing-page URLs.
_CITY_SLUG = {
    "DEL": "delhi", "BOM": "mumbai", "BLR": "bengaluru", "CCU": "kolkata",
    "HYD": "hyderabad", "MAA": "chennai", "AMD": "ahmedabad", "PNQ": "pune",
    "GOI": "goa", "COK": "kochi", "LKO": "lucknow", "IXC": "chandigarh",
    "PAT": "patna", "BBI": "bhubaneshwar", "GAU": "guwahati", "VNS": "varanasi",
    "SXR": "srinagar", "IXB": "bagdogra", "GOX": "goa", "JAI": "jaipur",
}

#: Tolerated spellings for each money field across booking-engine versions.
#: Extended from live payloads as they are observed, never guessed at runtime.
_JSON_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "total":       ("totalAmount", "total", "grandTotal", "totalFare", "amount"),
    "base":        ("baseFare", "base", "baseAmount", "fareAmount", "netFare"),
    "taxes":       ("taxAmount", "taxes", "totalTax", "tax"),
    "udf":         ("udf", "userDevelopmentFee", "UDF", "userDevFee"),
    "convenience": ("convenienceFee", "conv", "convFee", "serviceFee"),
    "other":       ("otherCharges", "surcharge", "misc", "otherFees"),
}

#: Akasa-specific: the indicative fare on an SEO route landing page.
_LANDING_FARE_RE = re.compile(
    r"(?:starting|lowest|from)[^₹]{0,40}₹\s?([\d,]{3,9})", re.I
)

# Money parsing, payload walking and state detection now live in
# apix/sources/parsing.py, shared with every other adapter. They were extracted
# when the SpiceJet adapter was written: two private copies of "what is a rupee"
# would have drifted, and then two adapters would disagree about a fare.


def _first_alias(node: dict[str, Any], field: str) -> Decimal | None:
    """Thin wrapper binding this adapter's alias table to the shared lookup."""
    return P.first_alias(node, _JSON_FIELD_ALIASES[field])


class AkasaAirSource(FareSource):
    code = "akasa"
    display_name = "Akasa Air"
    kind = SourceKind.AIRLINE
    tier = Tier.PRIMARY
    base_url = "https://www.akasaair.com"

    #: Well above anything Akasa asks for -- their robots.txt sets no Crawl-delay
    #: at all. 8s x 6 routes x 5 windows is 4 minutes of contact per daily cycle.
    min_delay_seconds = 8.0

    #: Nothing is disallowed, but we still never touch account or payment paths.
    #: Fare collection has no business near either.
    intent_denied_paths = ("/manage-booking", "/payment", "/checkout", "/my-account")

    parser_version = "akasa-v1"

    #: The availability endpoint the booking SPA calls. Matched as a substring
    #: against request URLs during a render, so a version bump in the path does
    #: not break interception.
    AVAILABILITY_URL_FRAGMENT = "availability"

    def __init__(self, ctx: FetchContext) -> None:
        super().__init__(ctx)

    # -- URL construction (pure) ------------------------------------------

    def landing_url(self, req: SearchRequest) -> str:
        o = _CITY_SLUG.get(req.origin)
        d = _CITY_SLUG.get(req.destination)
        if not o or not d:
            raise ParseError(
                f"no Akasa city slug for {req.origin}->{req.destination}; "
                f"add it to _CITY_SLUG"
            )
        return f"{self.base_url}/flight-booking/{o}-to-{d}"

    def build_search_url(self, req: SearchRequest) -> str:
        params = {
            "origin": req.origin,
            "destination": req.destination,
            "departureDate": req.departure_date.isoformat(),
            "adult": str(req.adults),
            "child": "0",
            "infant": "0",
            "currency": "INR",
            "tripType": "O",
        }
        return f"{self.base_url}/book-flight-tickets?{urllib.parse.urlencode(params)}"

    # -- fetch -------------------------------------------------------------

    async def fetch(self, req: SearchRequest) -> FetchResult:
        """Render the booking search and capture the availability JSON.

        All egress goes through `self.ctx`, so robots, throttle and audit apply
        to both the render and every subresource the page requests.
        """
        return await self.ctx.render(
            self.build_search_url(req),
            source_code=self.code,
            wait_for="[data-testid='flight-results'], .flight-results, .no-flights",
            capture_json=self.AVAILABILITY_URL_FRAGMENT,
        )

    async def probe_route_served(self, req: SearchRequest) -> bool:
        """Cheap check that Akasa serves this city pair before a full render."""
        try:
            res = await self.ctx.get(self.landing_url(req), source_code=self.code)
        except Exception as exc:
            log.info("akasa landing probe failed for %s-%s: %s",
                     req.origin, req.destination, exc)
            return True   # inconclusive -> let the real search decide
        return res.status == 200

    # -- parse (pure) ------------------------------------------------------

    def parse(self, req: SearchRequest, result: FetchResult) -> list[FareQuote]:
        payload = getattr(result, "json", None)
        if payload:
            return self.parse_availability_json(req, payload)
        text = getattr(result, "text", "") or ""
        if self._looks_sold_out(text):
            raise SoldOut(f"Akasa {req.origin}-{req.destination} {req.departure_date}")
        if self._looks_no_service(text):
            raise NoFlights(f"Akasa does not serve {req.origin}-{req.destination}")
        raise ParseError(
            "no availability JSON captured and page text matched no known state"
        )

    @staticmethod
    def _looks_sold_out(text: str) -> bool:
        t = text.lower()
        return any(s in t for s in (
            "sold out", "no seats available", "fully booked",
            "no fares available", "seats are not available",
        ))

    @staticmethod
    def _looks_no_service(text: str) -> bool:
        t = text.lower()
        return any(s in t for s in (
            "no flights found", "we don't fly", "we do not operate",
            "no flights available on", "try another date",
        ))

    def parse_availability_json(
        self, req: SearchRequest, payload: Any
    ) -> list[FareQuote]:
        """Extract quotes from the availability payload.

        Strategy: find every dict that carries a recognisable total fare *and*
        something flight-shaped, then decompose it. Structure-agnostic, because
        the nesting varies between engine versions while the leaf field names are
        stable.
        """
        quotes: list[FareQuote] = []
        seen: set[tuple[str, str]] = set()

        for node in P.walk(payload):
            total = _first_alias(node, "total")
            if total is None or total <= 0:
                continue

            flight_no = self._flight_number(node)
            dep_at = self._departure_at(node, req)
            key = (flight_no or "", dep_at.isoformat() if dep_at else "")
            if key in seen:
                continue

            stops = node.get("stops", node.get("numberOfStops", 0))
            try:
                stops = int(stops)
            except (TypeError, ValueError):
                stops = 0
            if stops != 0:
                continue  # non-stop economy only, per METHODOLOGY.md sec.7

            base = _first_alias(node, "base")
            taxes = _first_alias(node, "taxes")
            udf = _first_alias(node, "udf")
            conv = _first_alias(node, "convenience")
            other = _first_alias(node, "other")

            # If base is present but taxes are not, derive taxes as the residual
            # ONLY when every other component is known. Otherwise leave the split
            # absent -- an unverifiable decomposition is worse than none.
            if base is not None and taxes is None:
                known = base + (udf or 0) + (conv or 0) + (other or 0)
                residual = total - known
                if residual >= 0:
                    taxes = residual
                else:
                    base = None

            q = FareQuote(
                source_code=self.code,
                origin=req.origin,
                destination=req.destination,
                carrier=CARRIER,
                departure_date=req.departure_date,
                scrape_date=req.scrape_date,
                total_fare=total,
                flight_number=flight_no,
                departure_at=dep_at,
                stops=0,
                fare_class="ECONOMY",
                fare_brand=self._brand(node),
                base_fare=base,
                taxes=taxes,
                udf=udf,
                convenience_fee=conv,
                other_charges=other,
                raw_fragment={k: v for k, v in node.items()
                              if not isinstance(v, (dict, list))},
            )
            if base is None:
                q.quality_flags.append("no_component_split")
            quotes.append(q)
            seen.add(key)

        if not quotes:
            keys = P.observed_keys(payload)
            raise ParseError(
                "no fare nodes recognised in Akasa availability payload. "
                f"Observed keys: {keys}. Extend _JSON_FIELD_ALIASES."
            )
        return quotes

    # -- field extraction helpers -----------------------------------------

    @staticmethod
    def _flight_number(node: dict[str, Any]) -> str | None:
        for k in ("flightNumber", "flightNo", "number", "designator"):
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                n = v.strip().upper()
                return n if n.startswith(CARRIER) else f"{CARRIER}{n.lstrip(CARRIER)}"
            if isinstance(v, dict):
                inner = v.get("flightNumber") or v.get("number")
                if inner:
                    return f"{CARRIER}{str(inner).lstrip(CARRIER)}"
        return None

    @staticmethod
    def _departure_at(node: dict[str, Any], req: SearchRequest) -> dt.datetime | None:
        for k in ("departureTime", "departure", "std", "departureDateTime"):
            v = node.get(k)
            if isinstance(v, str):
                try:
                    return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
                except ValueError:
                    if re.fullmatch(r"\d{2}:\d{2}", v):
                        h, m = map(int, v.split(":"))
                        return dt.datetime.combine(
                            req.departure_date, dt.time(h, m)
                        )
        return None

    @staticmethod
    def _brand(node: dict[str, Any]) -> str | None:
        for k in ("fareType", "brandName", "productClass", "fareBrand"):
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    def parse_landing_fare(self, req: SearchRequest, html: str) -> Decimal | None:
        """Indicative lowest fare from the SEO landing page.

        Never enters the index -- it has no carrier/flight/date attribution and no
        component split. Used only for coverage checks and monitoring.
        """
        m = _LANDING_FARE_RE.search(html)
        return P.money(m.group(1)) if m else None
