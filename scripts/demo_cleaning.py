#!/usr/bin/env python3
"""Show the cleaning pipeline on a synthetic day of quotes.

Generates a realistic day for two carriers across two advance-purchase windows,
injects the four fault types the pipeline is built to catch, and prints the
report. Useful for a demo and for eyeballing the discard rate after a threshold
change.

    python scripts/demo_cleaning.py
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import random
import sys
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apix.pipeline.clean import clean_quotes  # noqa: E402
from apix.sources.base import FareQuote  # noqa: E402

SCRAPE = dt.date(2026, 9, 4)


def synth_day(seed: int = 42) -> list[FareQuote]:
    rng = random.Random(seed)
    quotes: list[FareQuote] = []
    for carrier, floor in (("QP", 4600), ("SG", 4300)):
        for window in (7, 30):
            # Fares are lower further from departure: the lead-time effect the
            # elasticity model quantifies later.
            base = floor if window == 7 else int(floor * 0.72)
            for i in range(7):
                total = int(base * (1 + 0.11 * i) * rng.uniform(0.95, 1.12))
                b, t, u = int(total * .82), int(total * .10), int(total * .05)
                quotes.append(FareQuote(
                    source_code="akasa", origin="DEL", destination="BOM",
                    carrier=carrier,
                    departure_date=SCRAPE + dt.timedelta(days=window),
                    scrape_date=SCRAPE, total_fare=D(total),
                    flight_number=f"{carrier}{100+i}", fare_brand="SAVER",
                    base_fare=D(b), taxes=D(t), udf=D(u),
                    convenience_fee=D(total - b - t - u)))

    src = quotes[3]
    stripped = dict(base_fare=None, taxes=None, udf=None, convenience_fee=None)
    quotes.append(dataclasses.replace(quotes[0]))                       # duplicate
    quotes.append(dataclasses.replace(src, total_fare=src.total_fare * 10,
                                      flight_number="QP199", **stripped))  # 10x error
    quotes.append(dataclasses.replace(src, total_fare=D("12"),
                                      flight_number="QP198", **stripped))  # component grabbed
    quotes.append(dataclasses.replace(src, flight_number="QP197",
                                      is_sold_out=True))                   # sold out
    return quotes


def main() -> int:
    result = clean_quotes(synth_day())
    rep = result.report
    print(f"\n  {rep.summary()}\n")
    print(f"  rejected by reason : {rep.reasons()}")
    print(f"  comparison groups  : {rep.comparison_groups} "
          f"(too small to assess: {rep.groups_too_small_to_assess})")
    print(f"  discard rate       : {rep.discard_rate:.1%}")
    print(f"  component split on : {rep.component_completeness:.0%} of survivors\n")

    print("  index prices (lowest per route x window x carrier):")
    for (o, d, w, c), p in sorted(result.index_prices.items(),
                                  key=lambda kv: (kv[0][2], kv[0][3])):
        print(f"    {o}-{d}  T+{w:<3} {c}   Rs {p:>7,}")

    print("\n  retained but excluded from the index:")
    for f in result.flagged:
        tag = ("sold_out" if f.is_sold_out
               else next(t for t in f.quality_flags if t.startswith("outlier:")))
        print(f"    {f.flight_number:<7} Rs {f.total_fare:>8,}   {tag}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
