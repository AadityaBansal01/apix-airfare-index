#!/usr/bin/env python3
"""Run the back-test and validation suite over the built index.

    python scripts/backtest.py                    # full run, chart + report
    python scripts/backtest.py --no-chart         # metrics only
    python scripts/backtest.py --out data/backtest

Produces:
    <out>/backtest.json      machine-readable results
    <out>/backtest.md        the written report
    <out>/backtest.png       chart: series, robustness band, benchmark

WHAT THIS VALIDATES, AND WHAT IT CANNOT
---------------------------------------
The problem statement asks for results validated against publicly available DGCA
monthly average-fare data. That series does not appear to exist: DGCA's Tariff
Monitoring Unit checks fares on 78 routes monthly as a regulatory function, and
what DGCA publishes monthly is traffic and load factor, not fares. Fare figures
appear in parliamentary answers as occasional point estimates, not as a joinable
series.

Rather than report correlation against numbers nobody can check, this runs the
validation that is actually possible: identity checks the index must satisfy by
construction, robustness against the assumptions the methodology flags, and a
comparison against a naive benchmark to show the methodology earns its keep. If
a reference series is ever loaded into apix.dgca_fare_reference, the external
comparison runs automatically and is reported alongside.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402

from apix import db  # noqa: E402
from apix.index.aggregate import cell_weights  # noqa: E402
from apix.index.build import month_start  # noqa: E402
from apix.validation import metrics  # noqa: E402
from apix.validation.suite import (  # noqa: E402
    SuiteResult,
    benchmark_against_naive,
    check_base_period,
    check_contributions_reconstruct,
    check_coverage_reported,
    check_temporal_consistency,
    robustness_over_window_weights,
)

D = Decimal


def load_basket() -> dict:
    return yaml.safe_load((REPO / "config/basket.yaml").read_text())


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def fetch_series(frequency: str) -> list[dict]:
    return db.fetch_all(
        "SELECT index_date, index_value, coverage_ratio, observed_pax_share, "
        "n_cells FROM apix.apix_index WHERE frequency = %s ORDER BY index_date",
        (frequency,))


def fetch_elementary() -> dict[dt.date, dict[tuple[int, int], Decimal]]:
    out: dict[dt.date, dict[tuple[int, int], Decimal]] = defaultdict(dict)
    for r in db.fetch_all(
        "SELECT index_date, route_id, window_days, index_value "
        "FROM apix.elementary_index ORDER BY index_date"
    ):
        out[r["index_date"]][(r["route_id"], r["window_days"])] = D(str(r["index_value"]))
    return dict(out)


def fetch_naive_mean_fare() -> list[tuple[dt.date, float]]:
    """Unweighted mean of usable fares per day: the benchmark to beat."""
    return [
        (r["scrape_date"], float(r["mean_fare"]))
        for r in db.fetch_all("""
            SELECT scrape_date, avg(total_fare) AS mean_fare
            FROM apix.fare
            WHERE NOT is_outlier AND NOT is_sold_out
            GROUP BY scrape_date ORDER BY scrape_date
        """)
    ]


def fetch_dgca_reference() -> list[dict]:
    return db.fetch_all("""
        SELECT r.route_code, d.ref_month, d.avg_fare, d.fare_basis, d.source_url
        FROM apix.dgca_fare_reference d
        JOIN apix.route r USING (route_id)
        ORDER BY d.ref_month, r.route_code
    """)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def run(out_dir: Path, make_chart: bool = True) -> dict:
    basket = load_basket()
    ref = basket["index_reference"]
    wref = basket["weight_reference"]
    price_ref_start, price_ref_end = ref["price_ref_start"], ref["price_ref_end"]

    daily = fetch_series("daily")
    monthly = fetch_series("monthly")
    if not daily:
        raise SystemExit("no daily index built. Run: make demo")

    result = SuiteResult()

    # -- identity checks --------------------------------------------------
    in_ref = [D(str(r["index_value"])) for r in daily
              if price_ref_start <= r["index_date"] <= price_ref_end]
    result.checks.append(check_base_period(in_ref))

    by_month: dict[dt.date, list[Decimal]] = defaultdict(list)
    for r in daily:
        by_month[month_start(r["index_date"])].append(D(str(r["index_value"])))
    aggregates = {r["index_date"]: D(str(r["index_value"])) for r in monthly}
    result.checks.append(check_temporal_consistency(by_month, aggregates))

    latest = daily[-1]["index_date"]
    contribs = [D(str(r["contribution"])) for r in db.fetch_all(
        "SELECT contribution FROM apix.apix_route_index "
        "WHERE index_date = %s AND frequency = 'daily'", (latest,))]
    if contribs:
        result.checks.append(check_contributions_reconstruct(
            D(str(daily[-1]["index_value"])), contribs))

    result.checks.append(check_coverage_reported(daily))

    # -- robustness over the flagged assumption ---------------------------
    route_w = {r["route_id"]: D(str(r["weight"])) for r in db.fetch_all(
        "SELECT route_id, weight FROM apix.route_weight "
        "WHERE weight_ref_start = %s", (wref["start"],))}
    windows = [w["days"] for w in basket["advance_windows"]]
    scenarios_cfg = basket.get("window_weight_scenarios", {})
    scenarios = {
        name: {w: D(str(v)) for w, v in zip(windows, vals)}
        for name, vals in scenarios_cfg.items()
    }
    elementary = fetch_elementary()
    if scenarios and route_w and elementary:
        result.robustness = robustness_over_window_weights(
            elementary, route_w, scenarios)

    # -- benchmark against a naive mean -----------------------------------
    naive = fetch_naive_mean_fare()
    naive_by_date = dict(naive)
    paired_dates = [r["index_date"] for r in daily if r["index_date"] in naive_by_date]
    if len(paired_dates) >= 2:
        idx_vals = [float(r["index_value"]) for r in daily
                    if r["index_date"] in naive_by_date]
        naive_vals = [naive_by_date[d] for d in paired_dates]
        result.benchmark = benchmark_against_naive(idx_vals, naive_vals)

    # -- external comparison, if any reference exists ---------------------
    dgca = fetch_dgca_reference()
    external: dict = {
        "available": bool(dgca),
        "n_observations": len(dgca),
    }
    if dgca:
        by_m: dict[dt.date, list[float]] = defaultdict(list)
        for r in dgca:
            by_m[r["ref_month"]].append(float(r["avg_fare"]))
        ref_months = sorted(by_m)
        monthly_by_m = {r["index_date"]: float(r["index_value"]) for r in monthly}
        shared = [m for m in ref_months if m in monthly_by_m]
        if len(shared) >= 2:
            comparison = metrics.compare(
                [monthly_by_m[m] for m in shared],
                [statistics.fmean(by_m[m]) for m in shared],
                label="DGCA average fare")
            external["comparison"] = comparison.as_dict()
            external["months"] = [m.isoformat() for m in shared]
        else:
            external["note"] = (
                f"{len(shared)} overlapping month(s); at least 2 are needed.")
    else:
        external["note"] = (
            "No DGCA reference fares loaded. No public route-wise monthly "
            "average-fare series was found: DGCA's Tariff Monitoring Unit "
            "checks 78 routes monthly as a regulatory function, and DGCA's "
            "monthly publications carry traffic and load factor, not fares. "
            "Load any reference you do obtain with scripts/load_dgca_fares.py "
            "and this comparison runs automatically.")

    seeded = bool(db.fetch_one(
        "SELECT EXISTS (SELECT 1 FROM apix.fare f JOIN apix.source s "
        "USING (source_id) WHERE s.code = 'seed') AS seeded")["seeded"])

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "seeded": seeded,
        "span": {
            "start": daily[0]["index_date"].isoformat(),
            "end": daily[-1]["index_date"].isoformat(),
            "n_days": len(daily),
            "meets_30_day_requirement": len(daily) >= 30,
        },
        "price_reference": f"{price_ref_start} to {price_ref_end}",
        "internal_validation": result.as_dict(),
        "external_validation": external,
        "coverage": {
            "basket_coverage_latest": float(daily[-1]["coverage_ratio"]),
            "observed_pax_share_latest": (
                float(daily[-1]["observed_pax_share"])
                if daily[-1]["observed_pax_share"] is not None else None),
        },
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "backtest.json").write_text(json.dumps(payload, indent=1, default=str))
    (out_dir / "backtest.md").write_text(render_report(payload))
    if make_chart:
        try:
            chart(out_dir / "backtest.png", daily, monthly, naive, result.robustness)
            payload["chart"] = str(out_dir / "backtest.png")
        except Exception as exc:  # noqa: BLE001 - a chart must not fail the run
            print(f"  chart skipped: {exc}", file=sys.stderr)

    return payload


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def render_report(p: dict) -> str:
    iv = p["internal_validation"]
    ev = p["external_validation"]
    span = p["span"]

    lines = [
        "# APIx back-test and validation report",
        "",
        f"Generated {p['generated_at']}.",
        "",
        f"**Span:** {span['start']} to {span['end']}, {span['n_days']} daily "
        f"observations. The problem statement asks for at least 30 days: "
        f"**{'met' if span['meets_30_day_requirement'] else 'NOT met'}**.",
        "",
        f"**Price reference period:** {p['price_reference']} = 100.",
        "",
        "---",
        "",
        "## 1. External validation against DGCA fares",
        "",
    ]

    if ev.get("available") and ev.get("comparison"):
        c = ev["comparison"]
        lines += [
            f"Reference observations: {ev['n_observations']} across "
            f"{c['n_periods']} months.",
            "",
            "| Score | Value | n |",
            "|---|---:|---:|",
        ]
        for name, s in c["scores"].items():
            v = "n/a" if s["value"] is None else f"{s['value']:.4f}"
            lines.append(f"| {name} | {v} | {s['n']} |")
        if c.get("note"):
            lines += ["", f"*{c['note']}*"]
    else:
        lines += [
            "**Not run.** " + ev.get("note", ""),
            "",
            "This is a genuine gap in the deliverable and is stated rather than "
            "filled with an estimate. Scoring an index against fabricated ground "
            "truth would be worse than not scoring it.",
        ]

    lines += [
        "",
        "---",
        "",
        "## 2. Internal validation",
        "",
        f"{iv['n_checks'] - iv['n_failed']} of {iv['n_checks']} checks passed.",
        "",
    ]
    for c in iv["checks"]:
        mark = "PASS" if c["passed"] else "**FAIL**"
        lines.append(f"- {mark} — **{c['name']}**: {c['detail']}")

    lines += ["", "---", "", "## 3. Robustness: advance-window weights", ""]
    if iv["robustness"]:
        lines += [
            "The equal-weighting of advance-purchase windows is the largest "
            "stated assumption in the methodology, because no public "
            "booking-lead-time distribution for India exists. This is what it is "
            "worth.",
            "",
            "| Scenario | Mean | Min | Max | Max gap vs equal | Mean gap |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for r in iv["robustness"]:
            lines.append(
                f"| {r['scenario']} | {r['mean']} | {r['min']} | {r['max']} | "
                f"{r['max_gap_vs_baseline']} | {r['mean_gap_vs_baseline']} |")
        worst = max(iv["robustness"], key=lambda r: r["max_gap_vs_baseline"])
        lines += [
            "",
            f"Worst case: the headline moves at most "
            f"**{worst['max_gap_vs_baseline']} index points** under "
            f"`{worst['scenario']}`, with a mean gap of "
            f"{worst['mean_gap_vs_baseline']}.",
        ]
    else:
        lines.append("Not run: no scenarios configured.")

    lines += ["", "---", "", "## 4. Benchmark against a naive mean fare", ""]
    b = iv.get("benchmark") or {}
    if b.get("n"):
        lines += [
            f"Paired observations: {b['n']}.",
            "",
            f"- Correlation of changes with the naive series: "
            f"**{b.get('correlation_on_changes')}**",
            f"- Maximum divergence after rebasing: "
            f"**{b.get('max_divergence_points')} index points**",
            f"- Mean divergence: {b.get('mean_divergence_points')} index points",
            "",
            f"*{b.get('note', '')}*",
        ]
        r = b.get("correlation_on_changes")
        if r is not None and r > 0.9 and p.get("seeded"):
            lines += [
                "",
                "**Read this result carefully.** The two series are nearly "
                "indistinguishable here, which on its face suggests the "
                "CPI-aligned machinery is not earning its complexity. That "
                "conclusion does not follow from this run, because the "
                "underlying data is synthetic and the generator applies one "
                "surge multiplier across every route and carrier at once. When "
                "everything moves together, weighting and the choice of "
                "elementary formula have almost nothing to distinguish, so any "
                "reasonable aggregation returns the same answer.",
                "",
                "The comparison becomes informative only on real data, where "
                "routes move independently. Until then this test establishes "
                "that the engine is not producing something wild, and nothing "
                "stronger. Treating it as evidence either for or against the "
                "methodology would be overreading it.",
            ]

    cov = p["coverage"]
    lines += [
        "", "---", "",
        "## 5. Coverage",
        "",
        f"- Basket coverage on the latest day: "
        f"{cov['basket_coverage_latest']:.1%}",
        f"- Share of domestic passenger traffic observable: "
        + (f"{cov['observed_pax_share_latest']:.1%}"
           if cov["observed_pax_share_latest"] is not None else "unavailable"),
        "",
        "The second figure is the binding constraint on this project. It is not "
        "improved by better engineering; it needs a licensed aggregator feed or "
        "a data-sharing arrangement with DGCA.",
        "",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def chart(path: Path, daily, monthly, naive, robustness) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    INK, GOLD, GREEN, TERRA = "#1F3A34", "#D9A441", "#4E8C6A", "#C4703E"

    dates = [r["index_date"] for r in daily]
    values = [float(r["index_value"]) for r in daily]

    fig, axes = plt.subplots(2, 1, figsize=(11, 8), height_ratios=[2, 1])
    fig.patch.set_facecolor("#EEF1EA")

    ax = axes[0]
    ax.set_facecolor("white")
    ax.plot(dates, values, color=INK, lw=2, label="APIx (daily)", zorder=3)
    if monthly:
        for m in monthly:
            start = m["index_date"]
            end = min(month_start(start).replace(day=28) + dt.timedelta(days=4),
                      dates[-1])
            ax.hlines(float(m["index_value"]), start, end,
                      color=GOLD, lw=2.5, zorder=2)
        ax.plot([], [], color=GOLD, lw=2.5, label="Monthly aggregate")

    # Naive benchmark, rebased onto the index's own scale for shape comparison.
    naive_by_date = dict(naive)
    nd = [d for d in dates if d in naive_by_date]
    if len(nd) > 2:
        nv = [naive_by_date[d] for d in nd]
        scale = values[0] / nv[0] if nv[0] else 1
        ax.plot(nd, [v * scale for v in nv], color=TERRA, lw=1.4, ls="--",
                label="Naive mean fare (rebased)", zorder=1)

    ax.axhline(100, color="#7C8D87", ls=":", lw=1)
    ax.set_ylabel("Index (reference period = 100)")
    ax.set_title("APIx back-test: daily index, monthly aggregate and naive benchmark",
                 fontsize=13, fontweight="bold", color=INK, loc="left")
    ax.legend(frameon=False, fontsize=9, loc="upper left")
    ax.grid(axis="y", color="#e4e9e1")
    ax.spines[["top", "right"]].set_visible(False)

    ax2 = axes[1]
    ax2.set_facecolor("white")
    if robustness:
        names = [r["scenario"] for r in robustness]
        gaps = [r["max_gap_vs_baseline"] for r in robustness]
        ax2.barh(names, gaps, color=[GREEN if g < 2 else GOLD if g < 5 else TERRA
                                     for g in gaps])
        for i, g in enumerate(gaps):
            ax2.text(g, i, f" {g:.2f}", va="center", fontsize=9, color=INK)
        ax2.set_xlabel("Maximum headline movement vs equal weighting (index points)")
        ax2.set_title("Robustness: advance-window weight scenarios",
                      fontsize=11, fontweight="bold", color=INK, loc="left")
    ax2.grid(axis="x", color="#e4e9e1")
    ax2.spines[["top", "right"]].set_visible(False)
    ax2.xaxis.set_major_locator(MaxNLocator(integer=False))

    fig.tight_layout()
    fig.savefig(path, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=REPO / "data/backtest")
    ap.add_argument("--no-chart", action="store_true")
    a = ap.parse_args()

    p = run(a.out, make_chart=not a.no_chart)
    iv = p["internal_validation"]
    ev = p["external_validation"]

    print(f"\nspan {p['span']['start']} .. {p['span']['end']} "
          f"({p['span']['n_days']} days, 30-day requirement "
          f"{'met' if p['span']['meets_30_day_requirement'] else 'NOT met'})\n")
    print(f"internal validation: {iv['n_checks'] - iv['n_failed']}/{iv['n_checks']} passed")
    for c in iv["checks"]:
        print(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['name']}: {c['detail']}")

    if iv["robustness"]:
        print("\nrobustness, advance-window weights:")
        for r in iv["robustness"]:
            print(f"  {r['scenario']:<15} mean={r['mean']:>8}  "
                  f"max gap vs equal = {r['max_gap_vs_baseline']:>6}")

    b = iv.get("benchmark") or {}
    if b.get("n"):
        print(f"\nbenchmark vs naive mean fare: correlation on changes "
              f"{b.get('correlation_on_changes')}, max divergence "
              f"{b.get('max_divergence_points')} points")

    print(f"\nexternal validation: "
          f"{'run' if ev.get('comparison') else 'NOT RUN'}")
    if not ev.get("comparison"):
        print(f"  {ev.get('note', '')[:200]}")

    print(f"\nwrote {a.out}/backtest.json, backtest.md"
          + ("" if a.no_chart else ", backtest.png"))
    return 0 if iv["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
