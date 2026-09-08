"""Analytical endpoints: the lead-time elasticity curve.

This is the genuine analytical artifact the problem statement asks for, as
distinct from the index itself. It answers a question a monthly CPI observation
cannot: how much more does a traveller pay for booking later, and does that
premium differ by sector?
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query

from apix.api.deps import parse_date, read_all, read_one
from apix.api.schemas import (
    AnomalyResponse,
    ElasticityEstimate,
    LeadTimeFit,
    LeadTimePoint,
    LeadTimeResponse,
    NowcastResponse,
)

router = APIRouter(prefix="/v1", tags=["analytics"])


@router.get("/leadtime", response_model=LeadTimeResponse,
            summary="Fare against advance-purchase window for one route")
def leadtime(
    route: str = Query(..., description="Route code such as BOM-DEL"),
    start: str | None = Query(None, description="ISO date, inclusive"),
    end: str | None = Query(None, description="ISO date, inclusive"),
) -> LeadTimeResponse:
    """Mean, median and minimum fare at each advance-purchase window.

    `ratio_to_longest_window` expresses each window's mean fare relative to the
    longest window in the basket, which is the lead-time premium in a form a
    non-specialist can read directly: 1.8 means booking at that window costs
    roughly eighty percent more than booking furthest ahead.

    Aggregated over the requested date range, so a single surge does not
    dominate the curve.
    """
    try:
        start_date = parse_date(start, "start")
        end_date = parse_date(end, "end")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    code = route.strip().upper()
    row = read_one("SELECT route_id FROM apix.route WHERE route_code = %s", (code,))
    if row is None:
        available = [
            r["route_code"]
            for r in read_all("SELECT route_code FROM apix.route ORDER BY route_code")
        ]
        raise HTTPException(
            status_code=404,
            detail=f"unknown route {code}. Routes are stored canonically with "
                   f"origin before destination alphabetically. Available: "
                   f"{', '.join(available) or 'none'}")
    route_id = row["route_id"]

    sql = """
        SELECT window_days,
               count(*)                    AS n_observations,
               round(avg(total_fare), 2)   AS mean_fare,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY total_fare) AS median_fare,
               min(total_fare)             AS min_fare
        FROM apix.fare
        WHERE route_id = %s AND NOT is_outlier AND NOT is_sold_out
    """
    params: list = [route_id]
    if start_date:
        sql += " AND scrape_date >= %s"
        params.append(start_date)
    if end_date:
        sql += " AND scrape_date <= %s"
        params.append(end_date)
    sql += " GROUP BY window_days ORDER BY window_days"

    rows = read_all(sql, tuple(params))
    if not rows:
        raise HTTPException(
            status_code=404, detail=f"no fare observations for {code}")

    longest = rows[-1]["mean_fare"]
    points = [
        LeadTimePoint(
            window_days=r["window_days"],
            n_observations=r["n_observations"],
            mean_fare=r["mean_fare"],
            median_fare=Decimal(str(r["median_fare"])).quantize(Decimal("0.01")),
            min_fare=r["min_fare"],
            ratio_to_longest_window=(
                (r["mean_fare"] / longest).quantize(Decimal("0.0001"))
                if longest else None),
        )
        for r in rows
    ]

    return LeadTimeResponse(
        route_code=code,
        start=start_date,
        end=end_date,
        points=points,
        fit=_fit_for(route_id, start_date, end_date),
        note=(
            "Mean fares across all observed carriers and departures in the "
            "range. Sold-out and outlier-flagged quotes are excluded. The "
            "`ratio_to_longest_window` column is descriptive; `fit` carries the "
            "estimated model with its uncertainty."),
    )


def _fit_for(route_id: int, start_date, end_date) -> LeadTimeFit | None:
    """Fit the lead-time curve on individual fares, not on the window means.

    Fitting the five means would discard the within-window dispersion that the
    standard errors are supposed to reflect, and would leave a three-parameter
    curve with two residual degrees of freedom. The individual observations are
    the honest input.
    """
    from apix.analytics.elasticity import fit_route

    sql = """
        SELECT window_days, total_fare FROM apix.fare
        WHERE route_id = %s AND NOT is_outlier AND NOT is_sold_out
    """
    params: list = [route_id]
    if start_date:
        sql += " AND scrape_date >= %s"
        params.append(start_date)
    if end_date:
        sql += " AND scrape_date <= %s"
        params.append(end_date)

    rows = read_all(sql, tuple(params))
    if not rows:
        return None

    windows = [float(r["window_days"]) for r in rows]
    fares = [float(r["total_fare"]) for r in rows]
    result = fit_route("", windows, fares)
    if result.exponential is None:
        return None

    e = result.exponential
    as_est = lambda x: ElasticityEstimate(**x.as_dict())
    return LeadTimeFit(
        floor=as_est(e.floor),
        amplitude=as_est(e.amplitude),
        decay_days=as_est(e.decay_days),
        r_squared=e.r_squared,
        rmse=e.rmse,
        n=e.n,
        converged=e.converged,
        percent_per_day=(result.log_linear.percent_per_day
                         if result.log_linear else None),
        warnings=result.warnings,
    )


@router.get("/anomalies", response_model=AnomalyResponse,
            summary="Flagged surges and drops in the index")
def anomalies(
    window: int = Query(14, ge=5, le=90,
                        description="Trailing days used to estimate normal volatility"),
    threshold: float = Query(3.5, ge=1.0, le=20.0,
                             description="Modified z-score threshold"),
) -> AnomalyResponse:
    """Days whose movement is large relative to the series' own recent volatility.

    Grouped into episodes, because a multi-day event is one event. A four-day
    surge flags its onset and its return, not each day in between: detection
    runs on day-over-day change, and the middle of a plateau barely moves.

    Every flag is statistical. The `hypotheses` field offers candidate
    explanations to check, never a cause.
    """
    from apix.analytics.anomaly import (
        collapse_episodes,
        detect_and_label,
        episode_direction,
        episode_peak,
    )
    from apix.api.deps import build_provenance
    from apix.api.schemas import AnomalyEpisode, AnomalyPoint, AnomalyResponse

    rows = read_all(
        "SELECT index_date, index_value FROM apix.apix_index "
        "WHERE frequency = 'daily' ORDER BY index_date")
    if not rows:
        raise HTTPException(status_code=404, detail="no daily index built yet")

    flags = detect_and_label(
        [(r["index_date"], r["index_value"]) for r in rows],
        window=window, threshold=threshold)

    episodes = []
    for group in collapse_episodes(flags):
        peak = episode_peak(group)
        episodes.append(AnomalyEpisode(
            start=group[0].index_date,
            end=group[-1].index_date,
            direction=episode_direction(group),
            peak_date=peak.index_date,
            peak_change_pct=round(peak.change_pct, 4),
            days=[AnomalyPoint(
                index_date=a.index_date, index_value=a.index_value,
                change_pct=round(a.change_pct, 4), score=round(a.score, 4),
                direction=a.direction, hypotheses=a.hypotheses) for a in group],
        ))

    return AnomalyResponse(
        window=window,
        threshold=threshold,
        n_flagged=len(flags),
        episodes=episodes,
        note=(
            "Flags are statistical, not causal. A day is flagged when its "
            "movement is extreme against a rolling median and median absolute "
            "deviation of the preceding window, with previously flagged days "
            "excluded from that baseline so an event cannot distort the "
            "yardstick used to judge what follows it."),
        provenance=build_provenance(),
    )


@router.get("/nowcast", response_model=NowcastResponse,
            summary="Rolling-origin scoreboard for short-horizon forecasts")
def nowcast(
    horizon: list[int] = Query([1, 7], description="Days ahead to score"),
    min_train: int = Query(21, ge=10, le=200,
                           description="Smallest training window an origin may use"),
):
    """Can the index be nowcast, and is any model worth publishing?

    Returns every model and every baseline on the same footing, scored by
    rolling origin so each number is a mean over dozens of forecasts rather than
    one lucky split.

    The endpoint exists as much to publish a negative result as a positive one.
    A price index is close to a random walk, and `recommendation` currently says
    that no model earns its place: read it before reading the table.
    """
    from apix.analytics.nowcast import evaluate
    from apix.api.deps import build_provenance
    from apix.api.schemas import NowcastScore

    rows = read_all(
        "SELECT index_date, index_value FROM apix.apix_index "
        "WHERE frequency = 'daily' ORDER BY index_date")
    if not rows:
        raise HTTPException(status_code=404, detail="no daily index built yet")

    report = evaluate([(r["index_date"], r["index_value"]) for r in rows],
                      horizons=tuple(horizon), min_train=min_train)
    return NowcastResponse(
        n_observations=report.n_observations,
        season=report.season,
        min_train=report.min_train,
        scores=[NowcastScore(**s.as_dict(), verdict=report.verdict_for(s))
                for s in report.scores],
        recommendation=report.recommendation,
        warnings=report.warnings,
        provenance=build_provenance(),
    )
