"""Index endpoints: latest value, historical series, route breakdown."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query

from apix.api.deps import build_provenance, parse_date, read_all, read_one
from apix.api.schemas import (
    Frequency,
    IndexPoint,
    IndexResponse,
    RouteBreakdownResponse,
    RoutePoint,
    SeriesResponse,
)

router = APIRouter(prefix="/v1/index", tags=["index"])

#: Hard ceiling on rows per request. A public endpoint must not let one caller
#: ask for an unbounded scan.
MAX_POINTS = 3650


def _point(row: dict) -> IndexPoint:
    return IndexPoint(
        date=row["index_date"],
        frequency=row["frequency"],
        index_value=row["index_value"],
        n_cells=row["n_cells"],
        coverage_ratio=row["coverage_ratio"],
        observed_pax_share=row["observed_pax_share"],
    )


def _pct_change(current: Decimal, previous: Decimal | None) -> Decimal | None:
    if previous is None or previous == 0:
        return None
    return ((current / previous - 1) * 100).quantize(Decimal("0.0001"))


@router.get("/latest", response_model=IndexResponse,
            summary="Latest published index value")
def latest(frequency: Frequency = "daily") -> IndexResponse:
    """Most recent index value, with period-over-period changes.

    `change_30d` compares against 30 periods back at the requested frequency, so
    on the monthly series it is a 30-month change, not a 30-day one.
    """
    rows = read_all(
        "SELECT * FROM apix.apix_index WHERE frequency = %s "
        "ORDER BY index_date DESC LIMIT 31", (frequency,))
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"no {frequency} index has been built yet. "
                   f"Run: make demo")

    current = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    back30 = rows[30] if len(rows) > 30 else None

    return IndexResponse(
        index=_point(current),
        change_1d=_pct_change(current["index_value"],
                              prev["index_value"] if prev else None),
        change_30d=_pct_change(current["index_value"],
                               back30["index_value"] if back30 else None),
        provenance=build_provenance(),
    )


@router.get("/series", response_model=SeriesResponse,
            summary="Historical index series")
def series(
    frequency: Frequency = "daily",
    start: str | None = Query(None, description="ISO date, inclusive"),
    end: str | None = Query(None, description="ISO date, inclusive"),
    limit: int = Query(MAX_POINTS, ge=1, le=MAX_POINTS),
) -> SeriesResponse:
    """The full series, for time-series consumers at the NSO or RBI."""
    try:
        start_date = parse_date(start, "start")
        end_date = parse_date(end, "end")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="start must not be after end")

    sql = "SELECT * FROM apix.apix_index WHERE frequency = %s"
    params: list = [frequency]
    if start_date:
        sql += " AND index_date >= %s"
        params.append(start_date)
    if end_date:
        sql += " AND index_date <= %s"
        params.append(end_date)
    sql += " ORDER BY index_date LIMIT %s"
    params.append(limit)

    rows = read_all(sql, tuple(params))
    points = [_point(r) for r in rows]
    return SeriesResponse(
        frequency=frequency,
        start=points[0].date if points else None,
        end=points[-1].date if points else None,
        count=len(points),
        points=points,
        provenance=build_provenance(),
    )


@router.get("/routes", response_model=RouteBreakdownResponse,
            summary="Route-level breakdown for one period")
def route_breakdown(
    date: str | None = Query(None, description="ISO date; defaults to latest"),
    frequency: Frequency = "daily",
) -> RouteBreakdownResponse:
    """Per-route index values and their contributions to the headline.

    Contributions sum to the headline, so a reader can see exactly which sector
    moved the national figure.
    """
    try:
        want = parse_date(date, "date")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if want is None:
        row = read_one(
            "SELECT max(index_date) AS d FROM apix.apix_index WHERE frequency = %s",
            (frequency,))
        want = row["d"] if row else None
    if want is None:
        raise HTTPException(status_code=404, detail="no index built yet")

    head = read_one(
        "SELECT index_value FROM apix.apix_index "
        "WHERE index_date = %s AND frequency = %s", (want, frequency))
    if head is None:
        raise HTTPException(
            status_code=404,
            detail=f"no {frequency} index for {want}")

    rows = read_all(
        """
        SELECT ri.route_id, r.route_code, r.origin, r.destination,
               ri.index_value, ri.weight, ri.contribution
        FROM apix.apix_route_index ri
        JOIN apix.route r USING (route_id)
        WHERE ri.index_date = %s AND ri.frequency = %s
        ORDER BY ri.contribution DESC
        """,
        (want, frequency))

    return RouteBreakdownResponse(
        date=want,
        frequency=frequency,
        headline=head["index_value"],
        routes=[RoutePoint(**r) for r in rows],
        provenance=build_provenance(),
    )
