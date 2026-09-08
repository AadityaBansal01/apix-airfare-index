"""SpiceJet (SG) fare source.

WHY THIS SOURCE IS PERMITTED
----------------------------
From docs/SOURCE_AUDIT.md, audited 2026-09-04. SpiceJet's robots.txt opens with
a bare `Disallow:` under `User-agent: *`, which under RFC 9309 means allow
everything. No Akamai, Cloudflare, PerimeterX or DataDome cookies were observed
on a homepage fetch. So this is the second of only two sources in the problem
statement that can be collected without evading a control the operator put up.

THE MALFORMED DIRECTIVES, AND WHY WE OBEY THEM ANYWAY
-----------------------------------------------------
The file ends with three lines that are not valid robots.txt:

    Disallow: https://www.spicejet.com/api/v1
    Disallow: https://www.spicejet.com/public/
    Disallow: https://www.spicejet.com/externalBooking

RFC 9309 requires a path, not an absolute URL, so a conforming parser ignores
all three and the site is fully open. The operator's intent is nonetheless
unmistakable. `intent_denied_paths` below refuses those prefixes before
robots.txt is even consulted, so the rule is enforced by code rather than by
good intentions in a comment.

This is the whole posture of the project in one place: where the letter and the
evident intent of a site's policy disagree, we follow the intent.

WHAT IS AND IS NOT VERIFIED
---------------------------
Verified by live fetch: the robots.txt above, the absence of bot-management
cookies, and that the origin serves without challenge. NOT verified: the field
names inside the availability payload. `_FIELD_ALIASES` is written against the
common shapes of this booking-engine family. `parse_availability_json` raises
with the observed key set rather than guessing, so the first live run either
works or names exactly what to add. Stated here rather than discovered later.
"""
from __future__ import annotations

import logging
import urllib.parse
from decimal import Decimal
from typing import Any

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

CARRIER = "SG"

#: Money fields, in the order they are tried. Extend from live payloads as they
#: are observed; never guess at runtime.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "total": ("totalAmount", "totalFare", "total", "grandTotal", "amount",
              "totalPrice"),
    "base": ("baseFare", "base", "baseAmount", "fareAmount", "netFare",
             "adultBaseFare"),
    "taxes": ("taxAmount", "taxes", "totalTax", "tax", "taxAndFees"),
    "udf": ("udf", "UDF", "userDevelopmentFee", "userDevFee",
            "airportDevelopmentFee"),
    "convenience": ("convenienceFee", "convFee", "conv", "serviceFee",
                    "handlingFee"),
    "other": ("otherCharges", "surcharge", "misc", "otherFees", "fuelSurcharge"),
}


class SpiceJetSource(FareSource):
    code = "spicejet"
    display_name = "SpiceJet"
    kind = SourceKind.AIRLINE
    tier = Tier.PRIMARY
    base_url = "https://www.spicejet.com"

    #: Well above anything SpiceJet asks for: its robots.txt sets no
    #: Crawl-delay at all. 8s across the 30-cell matrix is four minutes of
    #: contact per daily cycle.
    min_delay_seconds = 8.0

    #: The three malformed absolute-URL Disallow lines, honoured as paths.
    #: /manage-booking and /payment are ours, not theirs: fare collection has no
    #: business near a booking or a payment flow.
    intent_denied_paths = (
        "/api/v1", "/public/", "/externalBooking",
        "/manage-booking", "/payment", "/checkout", "/my-account",
    )

    parser_version = "spicejet-v1"

    #: Substring matched against request URLs during a render, so a version bump
    #: in the path does not break interception.
    AVAILABILITY_URL_FRAGMENT = "availability"

    def __init__(self, ctx: FetchContext) -> None:
        super().__init__(ctx)

    # -- URL construction (pure) ------------------------------------------

    def build_search_url(self, req: SearchRequest) -> str:
        """SpiceJet's booking entry point.

        Its search state lives in query parameters on the root document, which
        is why there is no separate results path to disallow and why the whole
        flow sits under the permissive `*` group.
        """
        params = {
            "bookingType": "oneway",
            "origin": req.origin,
            "destination": req.destination,
            "departureDate": req.departure_date.isoformat(),
            "adultCount": str(req.adults),
            "childCount": "0",
            "infantCount": "0",
            "currency": "INR",
            "isDomestic": "true",
        }
        return f"{self.base_url}/?{urllib.parse.urlencode(params)}"

    # -- fetch -------------------------------------------------------------

    async def fetch(self, req: SearchRequest) -> FetchResult:
        """Render the search and capture the availability payload.

        All egress goes through `self.ctx`, so robots, intent rules, the rate
        limiter and the audit log apply to the render and to every subresource
        the page requests.
        """
        return await self.ctx.render(
            self.build_search_url(req),
            source_code=self.code,
            wait_for="[data-testid='flight-results'], .flight-results, "
                     ".no-flights, .search-results",
            capture_json=self.AVAILABILITY_URL_FRAGMENT,
        )

    # -- parse (pure) ------------------------------------------------------

    def parse(self, req: SearchRequest, result: FetchResult) -> list[FareQuote]:
        payload = getattr(result, "json", None)
        if payload:
            return self.parse_availability_json(req, payload)

        text = getattr(result, "text", "") or ""
        if P.looks_sold_out(text):
            raise SoldOut(f"SpiceJet {req.origin}-{req.destination} "
                          f"{req.departure_date}")
        if P.looks_no_service(text):
            raise NoFlights(f"SpiceJet does not serve "
                            f"{req.origin}-{req.destination} on "
                            f"{req.departure_date}")
        raise ParseError(
            "no availability JSON captured and the page matched no known state")

    def parse_availability_json(self, req: SearchRequest,
                                payload: Any) -> list[FareQuote]:
        """Extract quotes from the availability payload.

        Finds every node carrying a recognisable total fare, then decomposes it.
        Structure-agnostic because the nesting varies between engine versions
        while the leaf field names are comparatively stable.
        """
        quotes: list[FareQuote] = []
        seen: set[tuple[str, str]] = set()

        for node in P.walk(payload):
            total = P.first_alias(node, _FIELD_ALIASES["total"])
            if total is None or total <= 0:
                continue

            if P.stops(node) != 0:
                continue          # non-stop economy only, per METHODOLOGY.md §7

            flight = P.flight_number(node, CARRIER)
            dep = P.departure_at(node, req.departure_date)
            key = (flight or "", dep.isoformat() if dep else "")
            if key in seen:
                continue

            base = P.first_alias(node, _FIELD_ALIASES["base"])
            taxes = P.first_alias(node, _FIELD_ALIASES["taxes"])
            udf = P.first_alias(node, _FIELD_ALIASES["udf"])
            conv = P.first_alias(node, _FIELD_ALIASES["convenience"])
            other = P.first_alias(node, _FIELD_ALIASES["other"])

            if base is not None and taxes is None:
                base, taxes = P.derive_taxes(total, base, udf, conv, other)

            q = FareQuote(
                source_code=self.code,
                origin=req.origin,
                destination=req.destination,
                carrier=CARRIER,
                departure_date=req.departure_date,
                scrape_date=req.scrape_date,
                total_fare=total,
                flight_number=flight,
                departure_at=dep,
                stops=0,
                fare_class="ECONOMY",
                fare_brand=P.brand(node),
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
            raise ParseError(
                "no fare nodes recognised in the SpiceJet availability payload. "
                f"Observed keys: {P.observed_keys(payload)}. "
                f"Extend _FIELD_ALIASES in apix/sources/spicejet.py.")
        return quotes
