"""Read-only queries for serving the API.

Kept apart from `apix.index.repository`, which exists to build the index and is
write-oriented. Serving has different needs: date ranges, joins to human-readable
route codes, and shapes that suit a JSON payload rather than the engine's
internal dictionaries. Mixing the two would leave the engine carrying query
methods it never calls and the API reaching into structures meant for
computation.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Sequence

FREQUENCIES = ("daily", "weekly", "monthly")


class Queries:
    def __init__(self, fetch_all=None, fetch_one=None) -> None:
        if fetch_all is None or fetch_one is None:
            from apix import db

            fetch_all = fetch_all or db.fetch_all
            fetch_one = fetch_one or db.fetch_one
        self._all = fetch_all
        self._one = fetch_one

    # -- headline ---------------------------------------------------------

    def latest(self, frequency: str) -> dict | None:
        return self._one(
            """
            SELECT index_date, frequency, index_value, n_cells, coverage_ratio,
                   observed_pax_share, weight_ref_start, price_ref_start,
                   formula, engine_version, computed_at
            FROM apix.apix_index
            WHERE frequency = %s
            ORDER BY index_date DESC
            LIMIT 1
            """,
            (frequency,))

    def series(
        self,
        frequency: str,
        start: dt.date | None = None,
        end: dt.date | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        sql = [
            """
            SELECT index_date, frequency, index_value, n_cells, coverage_ratio,
                   observed_pax_share, computed_at
            FROM apix.apix_index
            WHERE frequency = %s
            """
        ]
        params: list[Any] = [frequency]
        if start:
            sql.append("AND index_date >= %s")
            params.append(start)
        if end:
            sql.append("AND index_date <= %s")
            params.append(end)
        sql.append("ORDER BY index_date")
        if limit:
            sql.append("LIMIT %s")
            params.append(limit)
        return self._all(" ".join(sql), tuple(params))

    def previous(self, frequency: str, before: dt.date) -> dict | None:
        return self._one(
            "SELECT index_date, index_value FROM apix.apix_index "
            "WHERE frequency = %s AND index_date < %s "
            "ORDER BY index_date DESC LIMIT 1",
            (frequency, before))

    # -- routes -----------------------------------------------------------

    def routes(self, frequency: str, index_date: dt.date) -> list[dict]:
        return self._all(
            """
            SELECT r.route_code, r.origin, r.destination,
                   ri.index_value, ri.weight, ri.contribution
            FROM apix.apix_route_index ri
            JOIN apix.route r USING (route_id)
            WHERE ri.frequency = %s AND ri.index_date = %s
            ORDER BY ri.weight DESC
            """,
            (frequency, index_date))

    def route_series(
        self, route_code: str, frequency: str,
        start: dt.date | None = None, end: dt.date | None = None,
    ) -> list[dict]:
        sql = [
            """
            SELECT ri.index_date, ri.index_value, ri.weight, ri.contribution
            FROM apix.apix_route_index ri
            JOIN apix.route r USING (route_id)
            WHERE r.route_code = %s AND ri.frequency = %s
            """
        ]
        params: list[Any] = [route_code, frequency]
        if start:
            sql.append("AND ri.index_date >= %s")
            params.append(start)
        if end:
            sql.append("AND ri.index_date <= %s")
            params.append(end)
        sql.append("ORDER BY ri.index_date")
        return self._all(" ".join(sql), tuple(params))

    def known_routes(self) -> list[str]:
        return [
            r["route_code"]
            for r in self._all(
                "SELECT route_code FROM apix.route WHERE in_basket ORDER BY route_code")
        ]

    # -- cells ------------------------------------------------------------

    def cells(self, index_date: dt.date, route_code: str | None = None) -> list[dict]:
        """Elementary indices: one route by one advance-purchase window."""
        sql = [
            """
            SELECT r.route_code, e.window_days, e.index_date, e.index_value,
                   e.n_carriers, e.n_quotes, e.carriers_used, e.is_imputed,
                   e.imputation_note, e.formula
            FROM apix.elementary_index e
            JOIN apix.route r USING (route_id)
            WHERE e.index_date = %s
            """
        ]
        params: list[Any] = [index_date]
        if route_code:
            sql.append("AND r.route_code = %s")
            params.append(route_code)
        sql.append("ORDER BY r.route_code, e.window_days")
        return self._all(" ".join(sql), tuple(params))

    def leadtime(self) -> list[dict]:
        """Observed fare levels by advance-purchase window."""
        return self._all(
            """
            SELECT r.route_code, f.window_days,
                   count(*)                     AS n,
                   round(avg(f.total_fare), 2)  AS mean_fare,
                   round(min(f.total_fare), 2)  AS min_fare,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY f.total_fare)
                                                AS median_fare
            FROM apix.fare f
            JOIN apix.route r USING (route_id)
            WHERE NOT f.is_outlier AND NOT f.is_sold_out
            GROUP BY r.route_code, f.window_days
            ORDER BY r.route_code, f.window_days
            """)

    # -- reference --------------------------------------------------------

    def basket(self) -> list[dict]:
        return self._all(
            """
            SELECT r.route_code, r.origin, r.destination, rw.passengers,
                   rw.weight, rw.weight_ref_start, rw.weight_ref_end,
                   rw.source_url
            FROM apix.route_weight rw
            JOIN apix.route r USING (route_id)
            ORDER BY rw.weight DESC
            """)

    def windows(self) -> list[dict]:
        return self._all(
            "SELECT window_days, label, weight, weight_basis "
            "FROM apix.advance_window WHERE in_basket ORDER BY window_days")

    def sources(self) -> list[dict]:
        rows = self._all(
            "SELECT code, display_name, base_url, kind, tier, enabled, "
            "exclusion_reason, audit_verdict, audited_on FROM apix.source "
            "ORDER BY tier, code")
        return [r for r in rows if not r["code"].startswith("__")]

    def compliance(self) -> list[dict]:
        return self._all(
            "SELECT * FROM apix.v_compliance_summary ORDER BY source")

    def robots_snapshots(self) -> list[dict]:
        return self._all(
            """
            SELECT s.code AS source, rs.robots_url, rs.http_status,
                   rs.fetched_at, rs.crawl_delay_s,
                   length(rs.body)               AS body_bytes,
                   encode(rs.body_sha256, 'hex') AS body_sha256
            FROM apix.robots_snapshot rs
            JOIN apix.source s USING (source_id)
            ORDER BY s.code, rs.fetched_at DESC
            """)

    def audit(self, limit: int = 200, source: str | None = None) -> list[dict]:
        sql = [
            """
            SELECT s.code AS source, a.requested_at, a.url, a.method,
                   a.robots_allowed, a.robots_rule, a.intent_denied,
                   a.delay_applied_s, a.http_status, a.outcome, a.bytes_returned,
                   a.duration_ms
            FROM apix.request_audit a
            JOIN apix.source s USING (source_id)
            """
        ]
        params: list[Any] = []
        if source:
            sql.append("WHERE s.code = %s")
            params.append(source)
        sql.append("ORDER BY a.requested_at DESC LIMIT %s")
        params.append(limit)
        return self._all(" ".join(sql), tuple(params))
