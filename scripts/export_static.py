#!/usr/bin/env python3
"""Export the built index to static JSON for the deployed dashboard.

WHY A STATIC EXPORT
-------------------
The deployed dashboard runs on Vercel, which has no access to the PostgreSQL
instance that holds the index. Three ways to bridge that, and the trade-off is
worth stating rather than hiding:

1.  **Static export (chosen).** The index is computed locally by the same engine
    the tests cover, then written to JSON that ships with the front end. No
    database in production, nothing to provision, and the demo cannot fail
    because a network is down. The cost is that the deployed figures are a
    snapshot and go stale until the next export.
2.  Serverless Python functions against a hosted Postgres. Live, but it makes a
    presentation depend on a database being reachable from a conference network,
    which is exactly the failure this project is supposed to avoid.
3.  Rebuild the index in the browser. Wrong on its face: the methodology would
    then exist twice, in Python and in JavaScript, and the two would drift.

The exported payload records `generated_at` and the data provenance, so the
dashboard can state plainly how old the snapshot is and where it came from.

PROVENANCE IS NOT OPTIONAL
--------------------------
`provenance.seeded` is computed from the source registry, not passed in by hand.
If any fare behind the index came from the synthetic generator, the flag is true
and the dashboard shows a badge on every view carrying an index value. A seeded
number must never be readable as a measurement.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from apix import db  # noqa: E402


def _json_default(o):
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (dt.date, dt.datetime)):
        return o.isoformat()
    raise TypeError(f"not JSON serialisable: {type(o)}")


def fetch(sql: str, params=()) -> list[dict]:
    return [dict(r) for r in db.fetch_all(sql, params)]


def export(out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    def write(name: str, payload) -> None:
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(payload, default=_json_default, indent=1))
        counts[name] = len(payload) if isinstance(payload, list) else 1

    # -- headline series, all three frequencies ---------------------------
    series = fetch("""
        SELECT index_date, frequency, index_value, n_cells, coverage_ratio,
               observed_pax_share
        FROM apix.apix_index
        ORDER BY frequency, index_date
    """)
    write("series", series)

    # -- route breakdown ---------------------------------------------------
    routes = fetch("""
        SELECT ri.index_date, ri.frequency, r.route_code, r.origin, r.destination,
               ri.index_value, ri.weight, ri.contribution
        FROM apix.apix_route_index ri
        JOIN apix.route r USING (route_id)
        ORDER BY ri.frequency, ri.index_date, r.route_code
    """)
    write("routes", routes)

    # -- lead-time curve: elementary index by advance window ---------------
    leadtime = fetch("""
        SELECT r.route_code, e.window_days, e.index_date, e.index_value,
               e.n_carriers, e.n_quotes, e.is_imputed
        FROM apix.elementary_index e
        JOIN apix.route r USING (route_id)
        ORDER BY r.route_code, e.window_days, e.index_date
    """)
    write("leadtime", leadtime)

    # -- observed fare levels by window, the elasticity artifact -----------
    fares_by_window = fetch("""
        SELECT r.route_code, f.window_days,
               count(*)                        AS n,
               round(avg(f.total_fare), 2)     AS mean_fare,
               round(min(f.total_fare), 2)     AS min_fare,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY f.total_fare)
                                               AS median_fare
        FROM apix.fare f
        JOIN apix.route r USING (route_id)
        WHERE NOT f.is_outlier AND NOT f.is_sold_out
        GROUP BY r.route_code, f.window_days
        ORDER BY r.route_code, f.window_days
    """)
    write("leadtime_fares", fares_by_window)

    # -- observed fare by route x window x DAY ------------------------------
    # The fare calendar shows rupees rather than index points, and these are the
    # rupees: an actual mean of observed fares for that sector, that booking
    # window, on that day. Deriving a daily fare by scaling one overall mean by
    # the day's index would have been easy and would have put a number on screen
    # that nobody ever observed.
    fares_daily = fetch("""
        SELECT route_code, window_days, scrape_date, n, mean_fare, min_fare
        FROM apix.v_leadtime_curve
        ORDER BY route_code, window_days, scrape_date
    """)
    write("fares_daily", fares_daily)

    # -- fitted lead-time curve, per route ---------------------------------
    # Fitted here in Python and shipped as parameters, not refitted in the
    # browser. The model is covered by tests; a JavaScript reimplementation
    # would be a second copy of the methodology and would drift from the first.
    from apix.analytics.elasticity import fit_route

    observations: dict[str, tuple[list[float], list[float]]] = {}
    for r in fetch("""
        SELECT rt.route_code, f.window_days, f.total_fare
        FROM apix.fare f JOIN apix.route rt USING (route_id)
        WHERE NOT f.is_outlier AND NOT f.is_sold_out
    """):
        w, y = observations.setdefault(r["route_code"], ([], []))
        w.append(float(r["window_days"]))
        y.append(float(r["total_fare"]))

    fits = []
    for code, (w, y) in sorted(observations.items()):
        result = fit_route(code, w, y)
        if result.exponential is None:
            fits.append({"route_code": code, "fitted": False,
                         "warnings": result.warnings})
            continue
        e = result.exponential
        fits.append({
            "route_code": code,
            "fitted": True,
            "floor": e.floor.value,
            "amplitude": e.amplitude.value,
            "amplitude_ci": [e.amplitude.ci_low, e.amplitude.ci_high],
            "decay_days": e.decay_days.value,
            "decay_days_ci": [e.decay_days.ci_low, e.decay_days.ci_high],
            "r_squared": e.r_squared,
            "n": e.n,
            "percent_per_day": (result.log_linear.percent_per_day
                                if result.log_linear else None),
            "warnings": result.warnings,
        })
    write("leadtime_fit", fits)

    # -- anomalies ---------------------------------------------------------
    # Detected in Python and shipped as flags, not recomputed in the browser.
    # The detector was JavaScript-only until now, which put it beyond the reach
    # of the test suite and would have needed a second implementation for the
    # API.
    from apix.analytics.anomaly import (
        collapse_episodes, detect_and_label, episode_direction, episode_peak,
    )

    daily = fetch("SELECT index_date, index_value FROM apix.apix_index "
                  "WHERE frequency = 'daily' ORDER BY index_date")
    flags = detect_and_label([(r["index_date"], r["index_value"]) for r in daily])
    write("anomalies", [
        {
            "start": g[0].index_date,
            "end": g[-1].index_date,
            "direction": episode_direction(g),
            "peak_date": episode_peak(g).index_date,
            "peak_change_pct": round(episode_peak(g).change_pct, 4),
            "peak_index_value": float(episode_peak(g).index_value),
            "score": round(episode_peak(g).score, 4),
            "hypotheses": episode_peak(g).hypotheses,
            "days": [d.index_date for d in g],
        }
        for g in collapse_episodes(flags)
    ])

    # -- nowcast scoreboard ------------------------------------------------
    # Shipped even though the verdict is negative. A rejected model is a result:
    # it tells a reader that the naive baseline is the honest thing to publish
    # at this sample size, and it shows the test that established it.
    from apix.analytics.nowcast import evaluate as evaluate_nowcast

    write("nowcast", evaluate_nowcast(
        [(r["index_date"], r["index_value"]) for r in daily]).as_dict())

    # -- compliance evidence ----------------------------------------------
    compliance = fetch("SELECT * FROM apix.v_compliance_summary ORDER BY source")
    write("compliance", compliance)

    # Internal pseudo-sources are filtered in Python rather than with a SQL LIKE.
    # psycopg scans the whole statement, comments included, for placeholders, so
    # a percent sign anywhere in the text has to be escaped. Not worth the
    # footgun for a two-element exclusion.
    sources = [
        s for s in fetch("""
            SELECT code, display_name, base_url, kind, tier, enabled,
                   exclusion_reason, audit_verdict, audited_on
            FROM apix.source ORDER BY tier, code
        """)
        if not s["code"].startswith("__")
    ]
    write("sources", sources)

    # -- basket and weights ------------------------------------------------
    basket = fetch("""
        SELECT r.route_code, r.origin, r.destination, rw.passengers, rw.weight,
               rw.weight_ref_start, rw.weight_ref_end
        FROM apix.route_weight rw
        JOIN apix.route r USING (route_id)
        ORDER BY rw.weight DESC
    """)
    write("basket", basket)

    windows = fetch("""
        SELECT window_days, label, weight, weight_basis
        FROM apix.advance_window WHERE in_basket ORDER BY window_days
    """)
    write("windows", windows)

    # -- CPI overlay -------------------------------------------------------
    # The official series, if a human has loaded it with scripts/load_cpi.py.
    # Empty is the normal state: MoSPI's data endpoints are not reachable
    # programmatically from outside India, and a fabricated government
    # statistic is worse than an empty chart.
    cpi = fetch("""
        SELECT series_code, ref_month, sector, index_value, base_year,
               description, source_url
        FROM apix.cpi_reference
        ORDER BY ref_month
    """)
    write("cpi", cpi)

    # Within-month dispersion of the DAILY index.
    #
    # This is the substance of the overlay argument and it does not depend on
    # having the CPI series at all: it quantifies what a once-a-month
    # observation cannot see, by contrasting the spread of daily values inside
    # a month against the single number a monthly collection would record.
    # `sampled_on_15th` is that counterfactual made concrete: the value a
    # collector visiting mid-month would have written down.
    dispersion = fetch("""
        WITH daily AS (
            SELECT index_date, index_value,
                   date_trunc('month', index_date)::date AS ref_month
            FROM apix.apix_index WHERE frequency = 'daily'
        )
        SELECT d.ref_month,
               count(*)                                    AS n_days,
               round(min(d.index_value), 4)                AS min_index,
               round(max(d.index_value), 4)                AS max_index,
               round(avg(d.index_value), 4)                AS mean_index,
               round(stddev_samp(d.index_value), 4)        AS sd_index,
               round(max(d.index_value) - min(d.index_value), 4) AS range_index,
               (SELECT round(x.index_value, 4) FROM daily x
                 WHERE x.ref_month = d.ref_month
                 ORDER BY abs(extract(day FROM x.index_date) - 15), x.index_date
                 LIMIT 1)                                  AS sampled_on_15th
        FROM daily d
        GROUP BY d.ref_month
        ORDER BY d.ref_month
    """)
    write("cpi_dispersion", dispersion)

    # -- provenance --------------------------------------------------------
    # Computed by apix.reference.provenance, the same module the API uses, so
    # the dashboard and the API can never disagree about whether these figures
    # are observations or synthetic.
    from apix.reference import provenance as provenance_module

    write("provenance", provenance_module.compute().as_dict())
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path,
                    default=REPO / "dashboard/public/data")
    args = ap.parse_args()
    counts = export(args.out)
    width = max(len(k) for k in counts)
    for name, n in counts.items():
        print(f"  {name:<{width}}  {n:>5} records")
    print(f"\nexported to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
