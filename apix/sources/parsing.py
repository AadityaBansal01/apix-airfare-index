"""Parsing helpers shared by every fare-source adapter.

Extracted from the Akasa adapter when the second one was written. The
alternative was copying roughly 150 lines of money parsing, payload walking and
state detection into each new source, which guarantees the copies drift and then
two adapters disagree about what a rupee is.

What lives here is source-agnostic: how to read money, how to find fare nodes in
an arbitrarily nested payload, how to recognise a sold-out page. What stays in
each adapter is source-specific: URL shapes, city slugs, and the field names that
particular booking engine uses.

Everything here is pure, so a recorded payload exercises it completely.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

#: Strips currency marks and separators before parsing. Handles the rupee sign,
#: the Indian digit grouping (1,23,456) and stray whitespace.
_MONEY_STRIP = re.compile(r"[₹Rs\.\s,]", re.IGNORECASE)


def money(value: Any) -> Decimal | None:
    """Parse a money value from a string or number.

    Returns None rather than guessing. A fare we cannot read is a fare we do not
    record; inventing one would be worse than a gap.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):          # bool is an int subclass; reject it
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = _MONEY_STRIP.sub("", value)
        if not cleaned or cleaned in ("-", "+"):
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def first_alias(node: Mapping[str, Any], aliases: Sequence[str]) -> Decimal | None:
    """First alias present in the node that parses as money."""
    for alias in aliases:
        if alias in node:
            v = money(node[alias])
            if v is not None:
                return v
    return None


def walk(node: Any) -> Iterable[dict[str, Any]]:
    """Yield every dict in a nested structure.

    Booking payloads nest journeys inside trips inside dates, and the nesting
    differs between engine versions and even between releases of the same
    engine. Walking the whole structure and recognising leaves by their field
    names is markedly more durable than encoding a path.
    """
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def observed_keys(payload: Any, limit: int = 40) -> list[str]:
    """Every key seen anywhere in a payload, for error messages.

    When a parser fails it must say what it actually saw, so the fix is one
    edit rather than an afternoon of guessing.
    """
    keys = {k for n in walk(payload) if isinstance(n, dict) for k in n}
    return sorted(keys)[:limit]


SOLD_OUT_MARKERS = (
    "sold out", "no seats available", "fully booked", "no fares available",
    "seats are not available", "no seats left",
)

NO_SERVICE_MARKERS = (
    "no flights found", "we don't fly", "we do not operate",
    "no flights available on", "try another date", "no flights on this route",
    "no results found",
)


def looks_sold_out(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in SOLD_OUT_MARKERS)


def looks_no_service(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in NO_SERVICE_MARKERS)


def flight_number(node: Mapping[str, Any], carrier: str,
                  keys: Sequence[str] = ("flightNumber", "flightNo", "number",
                                         "designator", "flightId")) -> str | None:
    """Normalise a flight number to CARRIER + digits.

    Engines report this as a bare number, as a full designator, or as a nested
    object. All three become the same string so deduplication works.
    """
    for k in keys:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            n = v.strip().upper().replace(" ", "")
            return n if n.startswith(carrier) else f"{carrier}{n.lstrip(carrier)}"
        if isinstance(v, (int, float)):
            return f"{carrier}{int(v)}"
        if isinstance(v, Mapping):
            inner = v.get("flightNumber") or v.get("number") or v.get("identifier")
            if inner is not None:
                return f"{carrier}{str(inner).strip().upper().lstrip(carrier)}"
    return None


def departure_at(node: Mapping[str, Any], departure_date: dt.date,
                 keys: Sequence[str] = ("departureTime", "departure", "std",
                                        "departureDateTime", "depTime")
                 ) -> dt.datetime | None:
    """Parse a departure timestamp, accepting ISO or a bare HH:MM."""
    for k in keys:
        v = node.get(k)
        if not isinstance(v, str) or not v.strip():
            continue
        s = v.strip()
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            pass
        if re.fullmatch(r"\d{1,2}:\d{2}", s):
            h, m = map(int, s.split(":"))
            if 0 <= h < 24 and 0 <= m < 60:
                return dt.datetime.combine(departure_date, dt.time(h, m))
    return None


def brand(node: Mapping[str, Any],
          keys: Sequence[str] = ("fareType", "brandName", "productClass",
                                 "fareBrand", "fareFamily")) -> str | None:
    for k in keys:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def stops(node: Mapping[str, Any],
          keys: Sequence[str] = ("stops", "numberOfStops", "stopCount")) -> int:
    """Stop count, defaulting to non-stop when the field is absent or unreadable.

    Defaulting to 0 is safe because the caller filters to non-stop anyway; a
    misread here drops a quote rather than admitting a connecting itinerary.
    """
    for k in keys:
        if k in node:
            try:
                return int(node[k])
            except (TypeError, ValueError):
                continue
    return 0


def derive_taxes(total: Decimal, base: Decimal | None, udf: Decimal | None,
                 convenience: Decimal | None,
                 other: Decimal | None) -> tuple[Decimal | None, Decimal | None]:
    """Derive taxes as the residual, but only when it is non-negative.

    Returns (base, taxes). If the known components already exceed the total we
    have misread something, so the base is cleared too and the caller keeps only
    the observed total. An unverifiable split is worse than none, because a
    consumer would reasonably assume it had been checked.
    """
    if base is None:
        return None, None
    known = base + (udf or Decimal(0)) + (convenience or Decimal(0)) + (other or Decimal(0))
    residual = total - known
    if residual < 0:
        return None, None
    return base, residual
