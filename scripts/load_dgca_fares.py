#!/usr/bin/env python3
"""Load reference average fares for the external back-test comparison.

WHY THIS IS A LOADER
--------------------
The problem statement asks for validation against publicly available DGCA
monthly average-fare data. Searching for that series did not find one. DGCA's
Tariff Monitoring Unit checks fares on 78 routes monthly by looking at airline
websites, but that is a regulatory compliance function; what DGCA publishes
monthly is traffic and load factor. Fare numbers appear in Lok Sabha and Rajya
Sabha answers as occasional point estimates for named sectors, not as a series
anyone can join against a daily index.

So the external comparison ships unrun rather than run against invented ground
truth, and this script is how it gets run the moment a real reference exists.
Anything can be loaded provided its basis is recorded: a parliamentary answer, a
TMU extract obtained by request, or a licensed aggregator's realised-fare series.
`fare_basis` is mandatory precisely because "average fare" means different things
in each of those, and a comparison that does not say which is uninterpretable.

EXPECTED COLUMNS (case-insensitive, extra columns ignored)
----------------------------------------------------------
    route           BOM-DEL   or  DEL-BOM   (direction is ignored; routes are
                                             stored canonically)
    month           2026-07   or  Jul-2026
    avg_fare        7250.00
    fare_basis      free text, REQUIRED unless --fare-basis is given
    source_url      free text, REQUIRED unless --source-url is given

Usage:
    python scripts/load_dgca_fares.py --csv fares.csv \
        --fare-basis "DGCA TMU monitored fare band midpoint" \
        --source-url "https://www.dgca.gov.in/..."
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from apix import db  # noqa: E402
from scripts.load_cpi import parse_month  # noqa: E402


def canonical_route(raw: str) -> tuple[str, str]:
    """Routes are stored with origin before destination alphabetically."""
    parts = [p.strip().upper() for p in str(raw).replace("_", "-").split("-")]
    parts = [p for p in parts if p]
    if len(parts) != 2 or not all(len(p) == 3 for p in parts):
        raise ValueError(f"unrecognised route {raw!r}; use a form like BOM-DEL")
    a, b = sorted(parts)
    return a, b


def load(csv_path: Path, default_basis: str | None,
         default_source: str | None) -> tuple[int, int, list[str]]:
    rows: list[tuple] = []
    skipped = 0
    unknown_routes: set[str] = set()

    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT route_id, origin, destination FROM apix.route")
        route_ids = {(r["origin"], r["destination"]): r["route_id"]
                     for r in cur.fetchall()}

    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise SystemExit(f"{csv_path} has no header row")
        lower = {(f or "").strip().lower(): f for f in reader.fieldnames}

        route_col = next((lower[k] for k in ("route", "sector", "city_pair")
                          if k in lower), None)
        month_col = next((lower[k] for k in ("month", "period", "date")
                          if k in lower), None)
        fare_col = next((lower[k] for k in ("avg_fare", "average_fare", "fare",
                                            "value") if k in lower), None)
        if not (route_col and month_col and fare_col):
            raise SystemExit(
                f"need route, month and fare columns; found {reader.fieldnames}")

        basis_col = lower.get("fare_basis")
        source_col = lower.get("source_url")

        for i, row in enumerate(reader, start=2):
            try:
                pair = canonical_route(row[route_col])
                month = parse_month(row[month_col])
                fare = float(str(row[fare_col]).replace(",", "").replace("₹", "").strip())
            except (ValueError, KeyError, TypeError) as exc:
                print(f"  line {i}: skipped ({exc})", file=sys.stderr)
                skipped += 1
                continue
            if fare <= 0:
                print(f"  line {i}: skipped (non-positive fare {fare})", file=sys.stderr)
                skipped += 1
                continue
            if pair not in route_ids:
                unknown_routes.add(f"{pair[0]}-{pair[1]}")
                skipped += 1
                continue

            basis = (row.get(basis_col) if basis_col else None) or default_basis
            source = (row.get(source_col) if source_col else None) or default_source
            if not basis or not source:
                raise SystemExit(
                    "every observation needs a fare_basis and a source_url, "
                    "either as CSV columns or via --fare-basis / --source-url. "
                    "'Average fare' means different things in different sources "
                    "and a comparison that does not say which is uninterpretable.")
            rows.append((route_ids[pair], month, fare, basis.strip(), source.strip()))

    if not rows:
        raise SystemExit("no usable rows found")

    with db.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO apix.dgca_fare_reference
                (route_id, ref_month, avg_fare, fare_basis, source_url)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (route_id, ref_month)
            DO UPDATE SET avg_fare = EXCLUDED.avg_fare,
                          fare_basis = EXCLUDED.fare_basis,
                          source_url = EXCLUDED.source_url,
                          fetched_at = now()
            """,
            rows)
    return len(rows), skipped, sorted(unknown_routes)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, required=True)
    ap.add_argument("--fare-basis", default=None,
                    help="what 'average fare' means in this source")
    ap.add_argument("--source-url", default=None)
    a = ap.parse_args()

    if not a.csv.exists():
        raise SystemExit(f"{a.csv} does not exist")

    written, skipped, unknown = load(a.csv, a.fare_basis, a.source_url)
    print(f"loaded {written} reference fares"
          + (f" ({skipped} rows skipped)" if skipped else ""))
    if unknown:
        print(f"  routes not in the basket, ignored: {', '.join(unknown)}")
    print("\nnow re-run the back-test:")
    print("  python scripts/backtest.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
