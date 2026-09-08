"""The audit trail. This module IS the ethical-scraping evidence.

Every outbound request writes exactly one `request_audit` row, whether it
succeeded, was refused by robots.txt, timed out, or returned nothing. Every
robots.txt we read is stored verbatim in `robots_snapshot`.

Three properties make the trail worth trusting.

**It cannot be skipped.** `PolicyEnforcedSession` writes the row in a `finally`,
so a crash mid-fetch still produces a record. A request that happened but was not
audited would make the whole table meaningless.

**It cannot be forged in the permissive direction.** The `audit_no_disallowed_fetch`
CHECK constraint rejects any row claiming a successful fetch of a disallowed URL.
Even a buggy writer cannot record a compliant-looking lie.

**It is append-only in practice.** Nothing in the codebase updates or deletes an
audit row. The demo query in `v_compliance_summary` reads it live.

A NullAuditWriter is provided so that the network layer is fully testable, and
usable in a dry run, with no database at hand.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from apix.ethics.robots import RobotsSnapshot

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AuditRecord:
    """One outbound request, as it will be stored."""
    source_code: str
    url: str
    user_agent: str
    robots_allowed: bool
    delay_applied_s: float
    outcome: str
    method: str = "GET"
    robots_rule: str | None = None
    robots_snapshot_id: int | None = None
    intent_denied: bool = False
    http_status: int | None = None
    bytes_returned: int | None = None
    duration_ms: int | None = None
    error_detail: str | None = None

    def validate(self) -> None:
        """Mirror the database CHECK constraints, so a violation is caught here
        with a readable message instead of surfacing as an opaque SQL error.

        These two conditions are the whole compliance guarantee. If this method
        ever disagrees with db/schema.sql, the tests fail.
        """
        if not self.robots_allowed and self.outcome not in (
            "blocked_by_robots",
            "blocked_by_intent",
        ):
            raise ValueError(
                f"audit_no_disallowed_fetch: robots_allowed=False requires outcome in "
                f"(blocked_by_robots, blocked_by_intent), got {self.outcome!r} for "
                f"{self.url!r}. A disallowed URL must never be recorded as fetched."
            )
        if self.intent_denied and self.outcome != "blocked_by_intent":
            raise ValueError(
                f"audit_intent_consistent: intent_denied=True requires "
                f"outcome='blocked_by_intent', got {self.outcome!r}"
            )
        if self.delay_applied_s < 0:
            raise ValueError("delay_applied_s must not be negative")


class AuditWriter(Protocol):
    def record(self, rec: AuditRecord) -> int: ...
    def save_snapshot(self, source_code: str, snap: RobotsSnapshot) -> int: ...


class NullAuditWriter:
    """In-memory writer for tests and dry runs.

    Still validates every record, so a test that would violate a database
    constraint fails in the test rather than only in production.
    """

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []
        self.snapshots: list[tuple[str, RobotsSnapshot]] = []
        self._next_id = 1

    def record(self, rec: AuditRecord) -> int:
        rec.validate()
        self.records.append(rec)
        rid, self._next_id = self._next_id, self._next_id + 1
        return rid

    def save_snapshot(self, source_code: str, snap: RobotsSnapshot) -> int:
        self.snapshots.append((source_code, snap))
        sid, self._next_id = self._next_id, self._next_id + 1
        snap.snapshot_id = sid
        return sid

    # -- convenience for assertions ---------------------------------------

    @property
    def outcomes(self) -> list[str]:
        return [r.outcome for r in self.records]

    def blocked(self) -> list[AuditRecord]:
        return [r for r in self.records if not r.robots_allowed]

    def fetched(self) -> list[AuditRecord]:
        return [r for r in self.records if r.robots_allowed]


class PostgresAuditWriter:
    """Persists the trail. Resolves and caches source_code -> source_id."""

    _INSERT_AUDIT = """
        INSERT INTO apix.request_audit
            (source_id, url, method, user_agent, robots_allowed, robots_rule,
             robots_snapshot_id, intent_denied, delay_applied_s, http_status,
             bytes_returned, duration_ms, outcome, error_detail)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING request_id
    """

    _INSERT_SNAPSHOT = """
        INSERT INTO apix.robots_snapshot
            (source_id, fetched_at, robots_url, http_status, body, body_sha256,
             crawl_delay_s)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        RETURNING snapshot_id
    """

    def __init__(self, conn_factory=None) -> None:
        from apix import db  # local import keeps psycopg optional at module load

        self._conn = conn_factory or db.connection
        self._source_ids: dict[str, int] = {}

    def _source_id(self, code: str) -> int:
        if code in self._source_ids:
            return self._source_ids[code]
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT source_id FROM apix.source WHERE code = %s", (code,))
            row = cur.fetchone()
            if row is None:
                raise LookupError(
                    f"source {code!r} is not registered in apix.source. "
                    f"Load it from config/sources.yaml before scraping."
                )
            sid = row["source_id"] if isinstance(row, dict) else row[0]
        self._source_ids[code] = sid
        return sid

    def record(self, rec: AuditRecord) -> int:
        rec.validate()
        sid = self._source_id(rec.source_code)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                self._INSERT_AUDIT,
                (sid, rec.url, rec.method, rec.user_agent, rec.robots_allowed,
                 rec.robots_rule, rec.robots_snapshot_id, rec.intent_denied,
                 rec.delay_applied_s, rec.http_status, rec.bytes_returned,
                 rec.duration_ms, rec.outcome, rec.error_detail),
            )
            row = cur.fetchone()
            return row["request_id"] if isinstance(row, dict) else row[0]

    def save_snapshot(self, source_code: str, snap: RobotsSnapshot) -> int:
        sid = self._source_id(source_code)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                self._INSERT_SNAPSHOT,
                (sid, snap.fetched_at, snap.robots_url, snap.http_status,
                 snap.body, snap.sha256, snap.crawl_delay),
            )
            row = cur.fetchone()
            snapshot_id = row["snapshot_id"] if isinstance(row, dict) else row[0]
        snap.snapshot_id = snapshot_id
        return snapshot_id


class SnapshotStoreAdapter:
    """Adapts an AuditWriter to the `SnapshotStore` protocol RobotsGate expects."""

    def __init__(self, writer: AuditWriter) -> None:
        self._writer = writer

    def save(self, source_code: str, snap: RobotsSnapshot) -> int:
        return self._writer.save_snapshot(source_code, snap)
