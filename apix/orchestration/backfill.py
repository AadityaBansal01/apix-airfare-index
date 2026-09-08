"""Backfilling a missed collection day, and the limits of what that can mean.

THE CENTRAL FACT
----------------
**A fare observation cannot be backfilled.** A fare is a price quoted at a
moment, for a departure a fixed number of days away. If the cycle for 5
September never ran, the T+7 fare for a 12 September departure *as it stood on 5
September* no longer exists. Nobody holds it. Asking the airline today returns
the price of that same seat with three days to go, which is a T+3 observation.
It belongs in a different cell of the matrix and carries a different lead-time
premium.

A collector that silently re-ran the matrix and wrote today's answers under
Friday's date would produce a series that looks complete and is wrong -- and
wrong in a direction, since fares rise as departure nears. It would bias every
backfilled day upward. This module exists to make that impossible.

WHAT BACKFILL ACTUALLY DOES
---------------------------
Three cases, decided by the date:

1.  **The date is today.** The cycle failed or was interrupted; the observations
    are still exactly the ones the schedule intended. Re-run the missing cells.
    This is a genuine backfill and the common case, because most failures are a
    network blip an hour ago, not a week ago.

2.  **The date is in the past.** Nothing can be recovered. `assess` reports which
    cells are missing and `record_gap` writes them as `outcome='missed'` so the
    gap is in the database rather than in someone's memory. The index engine
    already handles a missing cell: `build_elementary` carries the last known
    elementary index forward and marks it `is_imputed`, and the coverage ratio
    published alongside every index value falls accordingly. The gap is
    therefore visible in the published output, which is the correct behaviour
    for a statistical series.

3.  **The date is in the future.** Refused. There is nothing to collect yet.

WHAT A REAL STATISTICAL AGENCY DOES HERE
----------------------------------------
The same thing: non-response is recorded and imputed under a stated rule, never
invented. This module is the small version of that discipline.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

from apix.orchestration.matrix import Cell, CollectionPlan

log = logging.getLogger("apix.orchestration.backfill")


class NotBackfillable(Exception):
    """The requested date's observations are gone and cannot be reconstructed."""


@dataclass(slots=True)
class Assessment:
    scrape_date: dt.date
    today: dt.date
    planned: tuple[Cell, ...]
    already_collected: tuple[Cell, ...]
    missing: tuple[Cell, ...]

    @property
    def recoverable(self) -> bool:
        """Only today's observations are still the ones the schedule wanted."""
        return self.scrape_date == self.today

    @property
    def age_days(self) -> int:
        return (self.today - self.scrape_date).days

    def describe(self) -> str:
        lines = [
            f"backfill assessment for {self.scrape_date} "
            f"(today is {self.today}, {self.age_days} day(s) ago)",
            f"  planned cells   : {len(self.planned)}",
            f"  already have    : {len(self.already_collected)}",
            f"  missing         : {len(self.missing)}",
        ]
        if not self.missing:
            lines.append("  nothing to do: the day is complete.")
        elif self.recoverable:
            lines.append("  RECOVERABLE: the date is today, so re-running these "
                         "cells collects the observations the schedule intended.")
        else:
            lines.append(
                f"  NOT RECOVERABLE: these observations were quoted "
                f"{self.age_days} day(s) ago and no longer exist. Re-running the "
                f"matrix now would collect T+{{w-{self.age_days}}} prices and file "
                f"them as T+w, biasing the day upward. The cells will be recorded "
                f"as gaps instead; the index engine imputes them and the published "
                f"coverage ratio falls to match.")
        return "\n".join(lines)


def collected_cells(scrape_date: dt.date, conn_factory=None) -> set[tuple[str, str, int]]:
    """Which (origin, destination, window) cells already hold fares for a date."""
    from apix import db

    factory = conn_factory or db.connection
    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT origin, destination, window_days "
            "FROM apix.fare WHERE scrape_date = %s", (scrape_date,))
        rows = cur.fetchall()
    out = set()
    for r in rows:
        o = r["origin"] if isinstance(r, dict) else r[0]
        d = r["destination"] if isinstance(r, dict) else r[1]
        w = r["window_days"] if isinstance(r, dict) else r[2]
        # The fare table stores routes canonically; compare unordered.
        out.add((*sorted((o.strip(), d.strip())), int(w)))
    return out


def assess(plan: CollectionPlan, *, today: dt.date | None = None,
           conn_factory=None) -> Assessment:
    today = today or dt.date.today()
    if plan.scrape_date > today:
        raise NotBackfillable(
            f"{plan.scrape_date} is in the future; there is nothing to collect yet")

    have = collected_cells(plan.scrape_date, conn_factory)
    missing = tuple(
        c for c in plan.cells
        if (*sorted((c.origin, c.destination)), c.window_days) not in have
    )
    collected = tuple(c for c in plan.cells if c not in set(missing))
    return Assessment(
        scrape_date=plan.scrape_date, today=today, planned=plan.cells,
        already_collected=collected, missing=missing,
    )


def record_gap(assessment: Assessment, *, conn_factory=None,
               reason: str | None = None) -> int:
    """Write the unrecoverable cells as `outcome='missed'`.

    The row is the artifact. A gap that exists only as an absence is a gap
    nobody can audit, and it is indistinguishable from a cell that was collected
    and cleaned away.
    """
    from apix import db

    if not assessment.missing:
        return 0
    factory = conn_factory or db.connection
    detail = reason or (
        f"collection cycle for {assessment.scrape_date} did not run or did not "
        f"complete; observed {assessment.age_days} day(s) later, by which point "
        f"the quoted prices no longer existed")

    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apix.collection_run "
            "(scrape_date, finished_at, mode, planned_cells, ok_cells, "
            " failed_cells, notes) "
            "VALUES (%s, now(), 'backfill', %s, 0, %s, %s) RETURNING run_id",
            (assessment.scrape_date, len(assessment.missing),
             len(assessment.missing), detail))
        row = cur.fetchone()
        run_id = row["run_id"] if isinstance(row, dict) else row[0]
        for c in assessment.missing:
            cur.execute(
                "INSERT INTO apix.collection_cell "
                "(run_id, source_code, origin, destination, window_days, "
                " departure_date, attempts, outcome, error_detail, finished_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,0,'missed',%s, now()) "
                "ON CONFLICT DO NOTHING",
                (run_id, c.source_code, c.origin, c.destination, c.window_days,
                 assessment.scrape_date + dt.timedelta(days=c.window_days),
                 detail))
    return len(assessment.missing)


def gap_report(start: dt.date, end: dt.date, conn_factory=None) -> list[dict]:
    """Recorded gaps in a date range, for the coverage story in the dashboard."""
    from apix import db

    factory = conn_factory or db.connection
    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT r.scrape_date, count(*) AS missed_cells, "
            "       min(r.notes) AS reason "
            "FROM apix.collection_cell c JOIN apix.collection_run r USING (run_id) "
            "WHERE c.outcome = 'missed' AND r.scrape_date BETWEEN %s AND %s "
            "GROUP BY r.scrape_date ORDER BY r.scrape_date", (start, end))
        return [dict(r) for r in cur.fetchall()]
