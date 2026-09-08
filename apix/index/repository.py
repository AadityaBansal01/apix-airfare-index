"""Database access for the index engine.

Kept apart from apix/index/build.py so the methodology stays testable without a
database and the SQL stays reviewable without wading through statistics.

Every write is idempotent on its natural key. Rebuilding an index for a date
that was already built overwrites it with the same inputs rather than
duplicating, so a rerun is safe and a revision is deliberate.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Mapping, Sequence

from apix.index.build import Cell, ENGINE_VERSION, IndexBuild

log = logging.getLogger(__name__)


class IndexRepository:
    def __init__(self, conn_factory=None) -> None:
        from apix import db

        self._conn = conn_factory or db.connection

    # -- reference --------------------------------------------------------

    def route_weights(self, weight_ref_start: dt.date) -> dict[int, Decimal]:
        rows = self._all(
            "SELECT route_id, weight FROM apix.route_weight "
            "WHERE weight_ref_start = %s", (weight_ref_start,))
        return {r["route_id"]: Decimal(str(r["weight"])) for r in rows}

    def window_weights(self) -> dict[int, Decimal]:
        rows = self._all(
            "SELECT window_days, weight FROM apix.advance_window WHERE in_basket")
        return {r["window_days"]: Decimal(str(r["weight"])) for r in rows}

    def basket_cells(self, weight_ref_start: dt.date) -> list[Cell]:
        return [
            (r, w)
            for r in self.route_weights(weight_ref_start)
            for w in self.window_weights()
        ]

    # -- prices -----------------------------------------------------------

    def index_prices(self, index_date: dt.date) -> dict[Cell, dict[str, Decimal]]:
        """Lowest usable total fare per (route, window, carrier) on one day.

        Outliers and sold-out rows are excluded here rather than in Python, so
        the exclusion rule lives in one place and matches `fare_index_cell`.
        """
        rows = self._all(
            """
            SELECT route_id, window_days, carrier, min(total_fare) AS price,
                   count(*) AS n
            FROM apix.fare
            WHERE scrape_date = %s AND NOT is_outlier AND NOT is_sold_out
            GROUP BY route_id, window_days, carrier
            """,
            (index_date,))
        out: dict[Cell, dict[str, Decimal]] = defaultdict(dict)
        for r in rows:
            out[(r["route_id"], r["window_days"])][r["carrier"]] = Decimal(str(r["price"]))
        return dict(out)

    def quote_counts(self, index_date: dt.date) -> dict[Cell, int]:
        rows = self._all(
            "SELECT route_id, window_days, count(*) AS n FROM apix.fare "
            "WHERE scrape_date = %s AND NOT is_outlier AND NOT is_sold_out "
            "GROUP BY route_id, window_days", (index_date,))
        return {(r["route_id"], r["window_days"]): r["n"] for r in rows}

    def prices_over_period(
        self, start: dt.date, end: dt.date
    ) -> dict[tuple[int, int, str], list[Decimal]]:
        """Daily lowest fares per cell and carrier across the price reference
        period, for base-price computation."""
        rows = self._all(
            """
            SELECT route_id, window_days, carrier, scrape_date,
                   min(total_fare) AS price
            FROM apix.fare
            WHERE scrape_date BETWEEN %s AND %s
              AND NOT is_outlier AND NOT is_sold_out
            GROUP BY route_id, window_days, carrier, scrape_date
            ORDER BY scrape_date
            """,
            (start, end))
        out: dict[tuple[int, int, str], list[Decimal]] = defaultdict(list)
        for r in rows:
            out[(r["route_id"], r["window_days"], r["carrier"])].append(
                Decimal(str(r["price"])))
        return dict(out)

    # -- base prices ------------------------------------------------------

    def save_base_prices(
        self,
        base: Mapping[tuple[int, int, str], Decimal],
        counts: Mapping[tuple[int, int, str], int],
        price_ref_start: dt.date,
        price_ref_end: dt.date,
    ) -> int:
        if not base:
            return 0
        with self._conn() as conn, conn.cursor() as cur:
            for (route_id, window, carrier), value in base.items():
                cur.execute(
                    """
                    INSERT INTO apix.base_price
                        (route_id, window_days, carrier, price_ref_start,
                         price_ref_end, base_price, n_observations, method)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'geometric_mean')
                    ON CONFLICT (route_id, window_days, carrier, price_ref_start)
                    DO UPDATE SET base_price = EXCLUDED.base_price,
                                  n_observations = EXCLUDED.n_observations,
                                  price_ref_end = EXCLUDED.price_ref_end
                    """,
                    (route_id, window, carrier, price_ref_start, price_ref_end,
                     value, counts.get((route_id, window, carrier), 1)))
        return len(base)

    def base_prices(self, price_ref_start: dt.date) -> dict[tuple[int, int, str], Decimal]:
        rows = self._all(
            "SELECT route_id, window_days, carrier, base_price FROM apix.base_price "
            "WHERE price_ref_start = %s", (price_ref_start,))
        return {
            (r["route_id"], r["window_days"], r["carrier"]): Decimal(str(r["base_price"]))
            for r in rows
        }

    # -- index history ----------------------------------------------------

    def last_known_elementary(self, before: dt.date) -> dict[Cell, Decimal]:
        """Most recent elementary value per cell strictly before `before`."""
        rows = self._all(
            """
            SELECT DISTINCT ON (route_id, window_days)
                   route_id, window_days, index_value
            FROM apix.elementary_index
            WHERE index_date < %s
            ORDER BY route_id, window_days, index_date DESC
            """,
            (before,))
        return {
            (r["route_id"], r["window_days"]): Decimal(str(r["index_value"]))
            for r in rows
        }

    def imputed_history(
        self, start: dt.date, end: dt.date
    ) -> dict[Cell, dict[dt.date, bool]]:
        rows = self._all(
            "SELECT route_id, window_days, index_date, is_imputed "
            "FROM apix.elementary_index WHERE index_date BETWEEN %s AND %s",
            (start, end))
        out: dict[Cell, dict[dt.date, bool]] = defaultdict(dict)
        for r in rows:
            out[(r["route_id"], r["window_days"])][r["index_date"]] = r["is_imputed"]
        return dict(out)

    # -- writes -----------------------------------------------------------

    def save_build(self, build: IndexBuild, weight_ref_start: dt.date,
                   price_ref_start: dt.date) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            if build.frequency == "daily":
                for cell, o in build.elementary.items():
                    if o.index_value is None:
                        continue
                    cur.execute(
                        """
                        INSERT INTO apix.elementary_index
                            (route_id, window_days, index_date, index_value,
                             n_carriers, n_quotes, carriers_used, formula,
                             is_imputed, imputation_note, engine_version)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,'jevons',%s,%s,%s)
                        ON CONFLICT (route_id, window_days, index_date)
                        DO UPDATE SET index_value = EXCLUDED.index_value,
                                      n_carriers = EXCLUDED.n_carriers,
                                      n_quotes = EXCLUDED.n_quotes,
                                      carriers_used = EXCLUDED.carriers_used,
                                      is_imputed = EXCLUDED.is_imputed,
                                      imputation_note = EXCLUDED.imputation_note
                        """,
                        (cell[0], cell[1], build.index_date, o.index_value,
                         max(1, len(o.carriers_used)), max(1, o.n_quotes),
                         list(o.carriers_used) or ["-"],
                         o.is_imputed, o.imputation_note, ENGINE_VERSION))

            cur.execute(
                """
                INSERT INTO apix.apix_index
                    (index_date, frequency, index_value, n_cells, coverage_ratio,
                     observed_pax_share, weight_ref_start, price_ref_start,
                     formula, engine_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'young_modified_laspeyres',%s)
                ON CONFLICT (index_date, frequency)
                DO UPDATE SET index_value = EXCLUDED.index_value,
                              n_cells = EXCLUDED.n_cells,
                              coverage_ratio = EXCLUDED.coverage_ratio,
                              observed_pax_share = EXCLUDED.observed_pax_share,
                              computed_at = now()
                """,
                (build.index_date, build.frequency, build.index_value,
                 build.n_cells, build.coverage_ratio, build.observed_pax_share,
                 weight_ref_start, price_ref_start, ENGINE_VERSION))

            for route_id, value in build.route_values.items():
                cur.execute(
                    """
                    INSERT INTO apix.apix_route_index
                        (index_date, frequency, route_id, index_value, weight,
                         contribution)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (index_date, frequency, route_id)
                    DO UPDATE SET index_value = EXCLUDED.index_value,
                                  weight = EXCLUDED.weight,
                                  contribution = EXCLUDED.contribution
                    """,
                    (build.index_date, build.frequency, route_id, value,
                     build.route_weights_used.get(route_id, Decimal(0)),
                     build.route_contributions.get(route_id, Decimal(0))))

    def scrape_dates(self, start: dt.date | None = None,
                     end: dt.date | None = None) -> list[dt.date]:
        sql = "SELECT DISTINCT scrape_date FROM apix.fare"
        params: list = []
        if start and end:
            sql += " WHERE scrape_date BETWEEN %s AND %s"
            params = [start, end]
        sql += " ORDER BY scrape_date"
        return [r["scrape_date"] for r in self._all(sql, tuple(params))]

    def series(self, frequency: str = "daily") -> list[dict]:
        return self._all(
            "SELECT * FROM apix.apix_index WHERE frequency = %s ORDER BY index_date",
            (frequency,))

    # -- helper -----------------------------------------------------------

    def _all(self, sql: str, params: Sequence = ()) -> list[dict]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
