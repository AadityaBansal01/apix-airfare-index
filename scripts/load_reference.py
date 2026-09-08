#!/usr/bin/env python3
"""Load reference data into an empty database.

Populates airports, carriers, routes, route weights, advance windows, and the
source registry from config/. Idempotent: safe to re-run.

The source registry matters beyond bookkeeping. `PostgresAuditWriter` resolves
source_code to source_id through it, so a source absent from this table cannot
write an audit row, and a source that cannot write an audit row cannot fetch.
Registration is therefore a third guard against scraping something we excluded.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from apix import db  # noqa: E402

AIRPORTS = [
    ("DEL", "Delhi", "DELHI", "DL"),
    ("BOM", "Mumbai", "MUMBAI", "MH"),
    ("BLR", "Bengaluru", "BENGALURU", "KA"),
    ("CCU", "Kolkata", "KOLKATA", "WB"),
    ("HYD", "Hyderabad", "HYDERABAD", "TG"),
    ("MAA", "Chennai", "CHENNAI", "TN"),
]

CARRIERS = [
    ("QP", "Akasa Air", True),
    ("SG", "SpiceJet", True),
    ("6E", "IndiGo", True),
    ("AI", "Air India", False),
    ("IX", "Air India Express", True),
]


def load(conn, basket_path, sources_path, weights_csv) -> dict:
    basket = yaml.safe_load(Path(basket_path).read_text())
    sources = yaml.safe_load(Path(sources_path).read_text())
    counts = {}

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO apix.airport (iata, city_name, dgca_city_name, state) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (iata) DO NOTHING", AIRPORTS)
        counts["airports"] = len(AIRPORTS)

        cur.executemany(
            "INSERT INTO apix.carrier (iata, name, is_lcc) VALUES (%s,%s,%s) "
            "ON CONFLICT (iata) DO NOTHING", CARRIERS)
        counts["carriers"] = len(CARRIERS)

        for w in basket["advance_windows"]:
            cur.execute(
                "INSERT INTO apix.advance_window (window_days, label, weight, weight_basis) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (window_days) DO UPDATE "
                "SET weight = EXCLUDED.weight, weight_basis = EXCLUDED.weight_basis",
                (w["days"], w["label"], w["weight"], w["weight_basis"]))
        counts["windows"] = len(basket["advance_windows"])

        wref = basket["weight_reference"]
        for r in basket["routes"]:
            o, d = sorted((r["origin"], r["destination"]))
            cur.execute(
                "INSERT INTO apix.route (origin, destination) VALUES (%s,%s) "
                "ON CONFLICT (origin, destination) DO NOTHING RETURNING route_id", (o, d))
            row = cur.fetchone()
            if row is None:
                cur.execute("SELECT route_id FROM apix.route WHERE origin=%s AND destination=%s",
                            (o, d))
                row = cur.fetchone()
            rid = row["route_id"] if isinstance(row, dict) else row[0]
            cur.execute(
                "INSERT INTO apix.route_weight "
                "(route_id, weight_ref_start, weight_ref_end, passengers, weight, source_url) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (route_id, weight_ref_start) DO UPDATE "
                "SET weight = EXCLUDED.weight, passengers = EXCLUDED.passengers",
                (rid, wref["start"], wref["end"], r["passengers"], r["weight"],
                 wref.get("source_dataset")))
        counts["routes"] = len(basket["routes"])

        for s in sources["sources"]:
            cur.execute(
                "INSERT INTO apix.source "
                "(code, display_name, base_url, kind, tier, enabled, exclusion_reason, "
                " audit_verdict, audited_on) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (code) DO UPDATE SET tier=EXCLUDED.tier, "
                "enabled=EXCLUDED.enabled, exclusion_reason=EXCLUDED.exclusion_reason, "
                "audit_verdict=EXCLUDED.audit_verdict, audited_on=EXCLUDED.audited_on",
                (s["code"], s["display_name"], s["base_url"], s["kind"], s["tier"],
                 s["enabled"], s.get("exclusion_reason"), s["audit_verdict"],
                 sources["audit_date"]))
        counts["sources"] = len(sources["sources"])

        # Pseudo-source for robots.txt fetches, which belong to no single adapter
        # but must still be auditable.
        cur.execute(
            "INSERT INTO apix.source "
            "(code, display_name, base_url, kind, tier, enabled, exclusion_reason, "
            " audit_verdict, audited_on) "
            "VALUES ('__robots__','robots.txt fetches','-','official',1,FALSE,"
            "'internal pseudo-source for robots.txt retrieval','scrape_ok',%s) "
            "ON CONFLICT (code) DO NOTHING", (sources["audit_date"],))
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--basket", type=Path, default=REPO / "config/basket.yaml")
    ap.add_argument("--sources", type=Path, default=REPO / "config/sources.yaml")
    ap.add_argument("--weights", type=Path, default=REPO / "data/reference/route_weights.csv")
    a = ap.parse_args()
    with db.connection() as conn:
        counts = load(conn, a.basket, a.sources, a.weights)
    for k, v in counts.items():
        print(f"  {k:10s} {v}")
    print("reference data loaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
