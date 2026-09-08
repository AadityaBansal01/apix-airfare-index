"""Shared API dependencies: read-only database access and provenance assembly.

TWO RULES THIS MODULE ENFORCES

**The API never writes.** The scraper and index engine write; this process only
reads. Keeping the split means a compromised or overloaded public endpoint cannot
corrupt the published series. `read_all` is the only database entry point the
routers get, and it takes a SELECT.

**Provenance is computed once, from the data.** The seeded flag is derived by
asking the database which sources the fares actually came from, not from an
environment variable a deployment might forget to set. If seeded rows are in
there, the API says so regardless of configuration.
"""
from __future__ import annotations

import datetime as dt
import functools
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import yaml

from apix.api.schemas import Provenance
from apix.settings import REPO_ROOT

API_VERSION = "1.0.0"


class DatabaseUnavailable(RuntimeError):
    pass


def read_all(sql: str, params: Sequence[Any] = ()) -> list[dict]:
    """Run a read-only query. Refuses anything that is not a SELECT or WITH."""
    stripped = sql.strip().lstrip("(").lower()
    if not (stripped.startswith("select") or stripped.startswith("with")):
        raise ValueError("the API is read-only; only SELECT queries are permitted")
    try:
        from apix import db
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailable(str(exc)) from exc


def read_one(sql: str, params: Sequence[Any] = ()) -> dict | None:
    rows = read_all(sql, params)
    return rows[0] if rows else None


@functools.lru_cache(maxsize=1)
def basket_config() -> dict:
    return yaml.safe_load((REPO_ROOT / "config/basket.yaml").read_text())


def is_seeded() -> bool:
    """True when any fare in the database came from the synthetic generator.

    Derived from the data rather than from configuration, because a badge that
    depends on an environment variable is a badge that eventually goes missing on
    the one deployment where it mattered.
    """
    try:
        row = read_one(
            "SELECT EXISTS (SELECT 1 FROM apix.fare f "
            "JOIN apix.source s USING (source_id) WHERE s.code = 'seed') AS seeded")
        return bool(row and row["seeded"])
    except DatabaseUnavailable:
        return False


def latest_coverage() -> tuple[Decimal | None, Decimal | None]:
    row = read_one(
        "SELECT coverage_ratio, observed_pax_share FROM apix.apix_index "
        "WHERE frequency = 'daily' ORDER BY index_date DESC LIMIT 1")
    if not row:
        return None, None
    return row["coverage_ratio"], row["observed_pax_share"]


def build_provenance() -> Provenance:
    cfg = basket_config()
    ref = cfg["index_reference"]
    wref = cfg["weight_reference"]
    coverage, pax = latest_coverage()

    if pax is not None:
        market = (
            f"Carriers observable under the source audit account for "
            f"{float(pax):.1%} of Indian domestic passenger traffic. IndiGo, "
            f"Air India and Air India Express are excluded on robots.txt and "
            f"bot-management grounds, so this index measures fare movement "
            f"within a minority of the market, not the market as a whole."
        )
    else:
        market = "Market coverage unavailable; no index has been built yet."

    basket = (
        f"Latest basket coverage {float(coverage):.1%}."
        if coverage is not None else "Basket coverage unavailable."
    )

    return Provenance(
        index_reference=f"{ref['price_ref_start']} to {ref['price_ref_end']} = 100",
        weight_reference=f"{wref['start']} to {wref['end']}",
        is_seeded=is_seeded(),
        coverage_note=f"{basket} {market}",
    )


def parse_date(value: str | None, field: str) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date such as 2026-09-04") from exc
