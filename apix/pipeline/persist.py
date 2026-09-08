"""Write raw quotes and cleaned fares to PostgreSQL.

Ingestion is idempotent. Re-running a scrape for a day that was already
collected must not duplicate rows, because a statistical series that changes
when you re-run the collector is not reproducible. Both raw and cleaned inserts
use ON CONFLICT DO NOTHING against the unique indexes declared in db/schema.sql,
so the database enforces it rather than the caller remembering to.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
from decimal import Decimal
from typing import Iterable, Iterator, Sequence

from apix.pipeline.clean import CleaningResult, outlier_verdict_for
from apix.sources.base import FareQuote

log = logging.getLogger(__name__)

CLEANER_VERSION = "clean-v1"


class ReferenceMissing(LookupError):
    """A route, carrier or window is absent from reference data."""


class Persister:
    def __init__(self, conn_factory=None) -> None:
        from apix import db

        self._conn = conn_factory or db.connection
        self._source_ids: dict[str, int] = {}
        self._route_ids: dict[tuple[str, str], int] = {}
        self._shared = None

    # -- connection reuse --------------------------------------------------

    @contextlib.contextmanager
    def batch(self) -> Iterator["Persister"]:
        """Hold one connection and one transaction across many saves.

        Without this, every `save_raw` and `save_fares` acquires its own
        connection and commits its own transaction. Seeding a 66-day dataset
        makes roughly four thousand such round trips and takes over half an
        hour, which breaks the promise that a judge can run the demo in ten
        minutes.

        Scope a batch to one collection day rather than a whole run: a day is
        the natural unit of atomicity, and a failure part-way through then costs
        one day rather than everything.
        """
        with self._conn() as conn:
            previous, self._shared = self._shared, conn
            try:
                yield self
            finally:
                self._shared = previous

    @contextlib.contextmanager
    def _cursor(self):
        if self._shared is not None:
            with self._shared.cursor() as cur:
                yield cur
        else:
            with self._conn() as conn, conn.cursor() as cur:
                yield cur

    # -- reference lookups -------------------------------------------------

    def _source_id(self, cur, code: str) -> int:
        if code not in self._source_ids:
            cur.execute("SELECT source_id FROM apix.source WHERE code = %s", (code,))
            row = cur.fetchone()
            if row is None:
                raise ReferenceMissing(f"source {code!r} not registered")
            self._source_ids[code] = _val(row, "source_id")
        return self._source_ids[code]

    def _route_id(self, cur, origin: str, destination: str) -> int:
        """Routes are stored canonically with origin < destination."""
        key = tuple(sorted((origin, destination)))
        if key not in self._route_ids:
            cur.execute(
                "SELECT route_id FROM apix.route WHERE origin = %s AND destination = %s",
                key,
            )
            row = cur.fetchone()
            if row is None:
                raise ReferenceMissing(
                    f"route {key[0]}-{key[1]} is not in the basket. Add it to "
                    f"config/basket.yaml and re-run scripts/load_reference.py."
                )
            self._route_ids[key] = _val(row, "route_id")
        return self._route_ids[key]

    # -- raw ---------------------------------------------------------------

    def save_raw(
        self,
        *,
        request_id: int,
        source_code: str,
        origin: str,
        destination: str,
        departure_date: dt.date,
        scrape_date: dt.date,
        payload: object,
        parser_version: str,
    ) -> int | None:
        """Store the untouched payload. Returns raw_id, or None if already stored."""
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        digest = hashlib.sha256(blob).digest()
        with self._cursor() as cur:
            sid = self._source_id(cur, source_code)
            cur.execute(
                """
                INSERT INTO apix.raw_quote
                    (request_id, source_id, scrape_date, origin, destination,
                     departure_date, window_days, payload, payload_sha256,
                     parser_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING raw_id
                """,
                (request_id, sid, scrape_date, origin, destination, departure_date,
                 (departure_date - scrape_date).days, json.dumps(payload, default=str),
                 digest, parser_version),
            )
            row = cur.fetchone()
            return _val(row, "raw_id") if row else None

    # -- cleaned -----------------------------------------------------------

    def save_fares(self, raw_id: int, quotes: Sequence[FareQuote]) -> int:
        """Persist cleaned fares, flagged ones included.

        A flagged fare is stored WITH its flag rather than dropped, so the
        exclusion is auditable and a threshold change can be re-evaluated
        against data we already hold.
        """
        if not quotes:
            return 0
        sql = """
            INSERT INTO apix.fare
                (raw_id, source_id, route_id, origin, destination, carrier,
                 flight_number, departure_at, arrival_at, stops, fare_class,
                 fare_brand, scrape_date, departure_date, window_days,
                 base_fare, taxes, udf, convenience_fee, other_charges,
                 total_fare, currency, components_complete, is_sold_out,
                 is_outlier, outlier_method, outlier_score, quality_flags,
                 cleaner_version)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
        """
        with self._cursor() as cur:
            rows = []
            for q in quotes:
                sid = self._source_id(cur, q.source_code)
                rid = self._route_id(cur, q.origin, q.destination)
                is_outlier, method, score = outlier_verdict_for(q)
                rows.append((
                    raw_id, sid, rid, q.origin, q.destination, q.carrier,
                    q.flight_number, q.departure_at, q.arrival_at, q.stops,
                    q.fare_class, q.fare_brand, q.scrape_date, q.departure_date,
                    q.window_days, q.base_fare, q.taxes, q.udf, q.convenience_fee,
                    q.other_charges, q.total_fare, q.currency,
                    q.components_complete, q.is_sold_out, is_outlier, method,
                    score, q.quality_flags, CLEANER_VERSION))
            # One round trip for the whole batch instead of one per fare.
            # `executemany` does not report per-row counts, so the return value
            # is the number of rows offered; ON CONFLICT may silently skip some.
            cur.executemany(sql, rows)
            return len(rows)

    def save_result(self, raw_id: int, result: CleaningResult) -> int:
        return self.save_fares(raw_id, result.persistable)


def _val(row, key: str):
    if row is None:
        return None
    return row[key] if isinstance(row, dict) else row[0]
