#!/usr/bin/env python3
"""Load the official CPI Transport series for the APIx-versus-CPI overlay.

WHY THIS IS A LOADER AND NOT A SCRAPER
--------------------------------------
The overlay is the argument this whole project rests on: a monthly official
series against a daily one, showing what monthly collection cannot see. It is
therefore the last place to put a number nobody has checked.

MoSPI publishes CPI through the eSankhyiki portal. Its data API lives at
api.mospi.gov.in and, at the time of writing, requires TLS legacy renegotiation
(handled in apix/net/transport.py) and returns the portal's single-page
application shell for the endpoint paths discoverable from the front end. I could
not retrieve the Division 07 series programmatically from outside India, and I
will not fabricate a government statistic to make a chart look complete. So this
script loads a file a human downloaded, and the chart stays empty until someone
does.

WHERE TO GET THE FILE
---------------------
    https://esankhyiki.mospi.gov.in/macroindicators?product=cpi

Select: CPI 2024 series (base 2024=100), All India, Combined sector,
Division 07 "Transport", monthly. Export to CSV.

EXPECTED COLUMNS (case-insensitive, extra columns ignored)
----------------------------------------------------------
    month           2026-07  or  Jul-2026  or  2026-07-01
    index_value     123.4
    sector          combined | rural | urban      (optional, default combined)
    base_year       2024                          (optional, default 2024)
    description     free text                     (optional)

Usage:
    python scripts/load_cpi.py --csv ~/Downloads/cpi_transport.csv
    python scripts/load_cpi.py --csv file.csv --series-code CPI_DIV07_TRANSPORT
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from apix import db  # noqa: E402

DEFAULT_SERIES = "CPI_DIV07_TRANSPORT"
DEFAULT_SOURCE_URL = "https://esankhyiki.mospi.gov.in/macroindicators?product=cpi"

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def parse_month(raw: str) -> dt.date:
    """Accept the several shapes a MoSPI export or a hand-made CSV may use.

    Always normalised to the first of the month, because a monthly index has no
    day and storing one invites a false join against a daily series.
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("empty month")

    if m := re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", s):
        return dt.date(int(m.group(1)), int(m.group(2)), 1)

    if m := re.fullmatch(r"([A-Za-z]{3,9})[-\s/](\d{4})", s):
        name = m.group(1)[:3].lower()
        if name in _MONTHS:
            return dt.date(int(m.group(2)), _MONTHS[name], 1)

    if m := re.fullmatch(r"(\d{1,2})[-/](\d{4})", s):
        return dt.date(int(m.group(2)), int(m.group(1)), 1)

    raise ValueError(f"unrecognised month {raw!r}; use 2026-07 or Jul-2026")


def load(csv_path: Path, series_code: str, source_url: str) -> tuple[int, int]:
    rows: list[tuple] = []
    skipped = 0
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise SystemExit(f"{csv_path} has no header row")
        lower = {(f or "").strip().lower(): f for f in reader.fieldnames}

        month_col = next((lower[k] for k in ("month", "period", "date") if k in lower), None)
        value_col = next((lower[k] for k in
                          ("index_value", "index", "value") if k in lower), None)
        if not month_col or not value_col:
            raise SystemExit(
                f"need a month column (month/period/date) and a value column "
                f"(index_value/index/value); found {reader.fieldnames}")

        sector_col = lower.get("sector")
        base_col = lower.get("base_year")
        desc_col = lower.get("description")

        for i, row in enumerate(reader, start=2):
            try:
                month = parse_month(row[month_col])
                value = float(str(row[value_col]).replace(",", "").strip())
            except (ValueError, KeyError, TypeError) as exc:
                print(f"  line {i}: skipped ({exc})", file=sys.stderr)
                skipped += 1
                continue
            if value <= 0:
                print(f"  line {i}: skipped (non-positive index {value})", file=sys.stderr)
                skipped += 1
                continue
            sector = (row.get(sector_col) or "combined").strip().lower() if sector_col else "combined"
            if sector not in ("rural", "urban", "combined"):
                sector = "combined"
            base_year = int(row.get(base_col) or 2024) if base_col else 2024
            description = (row.get(desc_col) or "CPI Division 07 Transport").strip() \
                if desc_col else "CPI Division 07 Transport"
            rows.append((series_code, month, sector, value, base_year,
                         description, source_url))

    if not rows:
        raise SystemExit("no usable rows found")

    with db.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO apix.cpi_reference
                (series_code, ref_month, sector, index_value, base_year,
                 description, source_url)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (series_code, ref_month, sector, base_year)
            DO UPDATE SET index_value = EXCLUDED.index_value,
                          description = EXCLUDED.description,
                          fetched_at  = now()
            """,
            rows)
    return len(rows), skipped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", type=Path, required=True,
                    help="CSV exported from eSankhyiki")
    ap.add_argument("--series-code", default=DEFAULT_SERIES)
    ap.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    a = ap.parse_args()

    if not a.csv.exists():
        raise SystemExit(f"{a.csv} does not exist")

    written, skipped = load(a.csv, a.series_code, a.source_url)
    print(f"loaded {written} CPI observations as {a.series_code}"
          + (f" ({skipped} rows skipped)" if skipped else ""))
    print("\nnow re-export the dashboard payload:")
    print("  make export")
    return 0


if __name__ == "__main__":
    sys.exit(main())
