"""Executing a collection cycle.

FOUR PROPERTIES, AND WHY EACH IS NON-OBVIOUS
--------------------------------------------

**One cell's failure does not abort the cycle.** A daily collection that stops
at the first timeout collects nothing on a bad network day, which is the day the
data matters most. Every cell is isolated; a failure becomes a recorded outcome,
not an exception that unwinds the run.

**Retries are narrow.** Only outcomes that could plausibly differ on a second
attempt are retried: a timeout, a 429, a 5xx. A `ParseError` is not retried,
because the page shape will not change in thirty seconds and retrying it just
sends a second request for nothing. A `PolicyRefused` is never retried under any
circumstance -- it is the correct outcome, and retrying a refusal is the exact
behaviour an ethics review would look for.

**Backoff is additive to the politeness delay, never a substitute for it.** The
throttle still applies in full before a retry; the backoff is extra time on top.

**Raw is written before cleaning.** Cleaning is a batch operation -- outlier
detection needs a day's worth of comparable quotes, not the four a single cell
returns -- so it happens once at the end of the cycle. If the process dies
between the two, the raw payloads are already durable and the day can be
re-cleaned without re-scraping. That is the whole reason the schema keeps raw
append-only and immutable.

CONCURRENCY
-----------
`workers_per_source` defaults to 1, and should stay there. `OriginThrottle`
serialises an origin behind an `asyncio.Lock`, so a second worker pointed at the
same origin does not make it faster -- it makes it wait in a queue. The option
exists because "the rate limit holds under concurrency" is a claim worth being
able to test rather than assert, and `tests/test_orchestration.py` runs four
workers at one origin and checks the recorded intervals.

Different sources DO run concurrently, and that is where the real speedup is:
two airlines at an 8-second floor collect a 60-cell matrix in the time one
would.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

from apix.orchestration.matrix import Cell, CollectionPlan
from apix.pipeline.clean import clean_quotes
from apix.sources.base import (
    BlockedByPolicy,
    FareQuote,
    NoFlights,
    ParseError,
    SoldOut,
)

log = logging.getLogger("apix.orchestration")

#: Cell outcomes. Mirrors the CHECK constraint on apix.collection_cell.
OK = "ok"
SOLD_OUT = "sold_out"
NO_FLIGHTS = "no_flights"
REFUSED = "refused"
FAILED = "failed"
SKIPPED = "skipped"

#: Terminal outcomes: a second attempt cannot change them.
TERMINAL = frozenset({OK, SOLD_OUT, NO_FLIGHTS, REFUSED, SKIPPED})


@dataclass(slots=True)
class CellResult:
    cell: Cell
    outcome: str
    attempts: int = 0
    quotes: int = 0
    error_detail: str | None = None
    duration_s: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.outcome in (OK, SOLD_OUT, NO_FLIGHTS)


@dataclass(slots=True)
class CycleReport:
    scrape_date: dt.date
    mode: str
    planned: int
    results: list[CellResult] = field(default_factory=list)
    quotes_written: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    cleaning_summary: str | None = None
    run_id: int | None = None

    @property
    def ok(self) -> int:
        return sum(1 for r in self.results if r.succeeded)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.succeeded)

    @property
    def elapsed_s(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def by_outcome(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.outcome] = out.get(r.outcome, 0) + 1
        return dict(sorted(out.items()))

    def summary(self) -> str:
        parts = [f"{k}={v}" for k, v in self.by_outcome().items()]
        return (f"{self.scrape_date} [{self.mode}] {self.ok}/{self.planned} cells ok, "
                f"{self.quotes_written} fares, {self.elapsed_s / 60:.1f} min "
                f"({', '.join(parts)})")


def classify(exc: BaseException) -> tuple[str, bool]:
    """Map an exception to (outcome, retryable).

    Retryability is decided here and nowhere else, so the policy is one list a
    reviewer can read rather than a set of `except` clauses spread across the
    runner.
    """
    from apix.net.session import FetchFailed, PolicyRefused

    if isinstance(exc, (PolicyRefused, BlockedByPolicy)):
        return REFUSED, False                 # correct outcome; never retry
    if isinstance(exc, SoldOut):
        return SOLD_OUT, False                # real information, not a failure
    if isinstance(exc, NoFlights):
        return NO_FLIGHTS, False
    if isinstance(exc, ParseError):
        return FAILED, False                  # the page changed; a retry cannot help
    if isinstance(exc, FetchFailed):
        status = getattr(exc, "status", None)
        if status is None or status == 429 or status >= 500:
            return FAILED, True               # transient: timeout, throttle, server
        return FAILED, False                  # 4xx will not improve
    if isinstance(exc, asyncio.TimeoutError):
        return FAILED, True
    return FAILED, True


class CycleRunner:
    """Walks a plan, one source per worker group, isolating every cell."""

    def __init__(
        self,
        session,
        sources: dict[str, object],
        *,
        persister=None,
        max_attempts: int = 2,
        backoff_base_s: float = 15.0,
        workers_per_source: int = 1,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        recorder: "RunRecorder | None" = None,
    ) -> None:
        self.session = session
        self.sources = sources
        self.persister = persister
        self.max_attempts = max(1, max_attempts)
        self.backoff_base_s = backoff_base_s
        self.workers_per_source = max(1, workers_per_source)
        self._sleep = sleep
        self._clock = clock
        self.recorder = recorder

    # -- one cell ----------------------------------------------------------

    async def run_cell(self, cell: Cell, scrape_date: dt.date
                       ) -> tuple[CellResult, list[tuple[int, list[FareQuote]]]]:
        """Collect one cell. Returns the result and any (request_id, quotes) pairs.

        Never raises. A cell that cannot be collected is a recorded outcome; the
        cycle continues.
        """
        source = self.sources.get(cell.source_code)
        started = self._clock()
        if source is None:
            return CellResult(cell, SKIPPED, 0, 0,
                              f"no adapter registered for {cell.source_code}"), []

        req = cell.request(scrape_date)
        collected: list[tuple[int, list[FareQuote]]] = []
        last_detail: str | None = None
        outcome = FAILED

        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                # Additive to the politeness delay the session applies anyway.
                await self._sleep(self.backoff_base_s * (2 ** (attempt - 2)))
            try:
                # fetch and parse separately rather than via `collect`, because
                # the audit row id lives on the fetch result and raw_quote must
                # point back at the request that produced it.
                result = await source.fetch(req)
                quotes = source.parse(req, result)
                for q in quotes:
                    q.validate()
                collected.append((getattr(result, "request_id", 0), quotes))
                return CellResult(cell, OK, attempt, len(quotes), None,
                                  self._clock() - started), collected
            except BaseException as exc:  # noqa: BLE001 - isolation is the point
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                    raise
                outcome, retryable = classify(exc)
                last_detail = f"{type(exc).__name__}: {exc}"
                if outcome in TERMINAL or not retryable:
                    break
                log.warning("cell %s attempt %d/%d failed: %s",
                            cell, attempt, self.max_attempts, last_detail)

        return CellResult(cell, outcome, attempt, 0, last_detail,
                          self._clock() - started), collected

    # -- one cycle ---------------------------------------------------------

    async def run(self, plan: CollectionPlan, *, mode: str = "manual") -> CycleReport:
        report = CycleReport(scrape_date=plan.scrape_date, mode=mode,
                             planned=len(plan), started_at=self._clock())
        if self.recorder:
            report.run_id = self.recorder.open(plan, mode)

        harvested: list[tuple[int, list[FareQuote]]] = []
        lock = asyncio.Lock()

        async def worker(queue: asyncio.Queue) -> None:
            while True:
                try:
                    cell = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    result, quotes = await self.run_cell(cell, plan.scrape_date)
                    async with lock:
                        report.results.append(result)
                        harvested.extend(quotes)
                        if self.recorder:
                            self.recorder.cell(report.run_id, result)
                finally:
                    queue.task_done()

        # One queue per source. Cells for different sources therefore proceed
        # in parallel while each origin stays serialised behind its own throttle.
        tasks = []
        for code in plan.sources:
            queue: asyncio.Queue = asyncio.Queue()
            for cell in plan.for_source(code):
                queue.put_nowait(cell)
            for _ in range(self.workers_per_source):
                tasks.append(asyncio.create_task(worker(queue)))

        if tasks:
            await asyncio.gather(*tasks)

        report.results.sort(key=lambda r: str(r.cell))
        report.quotes_written = self._persist(harvested, report)
        report.finished_at = self._clock()
        if self.recorder:
            self.recorder.close(report)
        return report

    # -- persistence -------------------------------------------------------

    def _persist(self, harvested: Sequence[tuple[int, list[FareQuote]]],
                 report: CycleReport) -> int:
        """Clean the whole cycle at once, then write.

        Cleaning per cell would run outlier detection on the three or four
        quotes one route x window yields, where a median absolute deviation
        means nothing. A day of quotes across the matrix is the smallest honest
        comparison set, which is also what the seeded path uses, so the two
        agree about what a clean fare is.
        """
        if self.persister is None or not harvested:
            return 0

        every: list[FareQuote] = [q for _, qs in harvested for q in qs]
        if not every:
            return 0
        result = clean_quotes(every)
        report.cleaning_summary = result.report.summary()

        keep = {id(q) for q in result.persistable}
        written = 0
        with self.persister.batch():
            for request_id, quotes in harvested:
                subset = [q for q in quotes if id(q) in keep]
                if not subset:
                    continue
                q0 = subset[0]
                raw_id = self.persister.save_raw(
                    request_id=request_id,
                    source_code=q0.source_code,
                    origin=q0.origin,
                    destination=q0.destination,
                    departure_date=q0.departure_date,
                    scrape_date=q0.scrape_date,
                    payload={"quotes": len(subset), "request_id": request_id},
                    parser_version="orchestrated-v1",
                )
                if raw_id is None:      # already stored: the cycle is idempotent
                    continue
                written += self.persister.save_fares(raw_id, subset)
        return written


class RunRecorder:
    """Writes collection_run and collection_cell rows.

    Separate from the runner so the runner is testable with no database, which
    is the only way the retry and isolation logic gets covered at all.
    """

    def __init__(self, conn_factory=None) -> None:
        from apix import db

        self._conn = conn_factory or db.connection
        self._scrape_date: dt.date | None = None

    def open(self, plan: CollectionPlan, mode: str) -> int | None:
        self._scrape_date = plan.scrape_date
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO apix.collection_run "
                    "(scrape_date, mode, planned_cells) VALUES (%s,%s,%s) "
                    "RETURNING run_id",
                    (plan.scrape_date, mode, len(plan)))
                row = cur.fetchone()
                return row["run_id"] if isinstance(row, dict) else row[0]
        except Exception as exc:  # noqa: BLE001 - never let bookkeeping stop a run
            log.warning("could not open collection_run: %s", exc)
            return None

    def cell(self, run_id: int | None, result: CellResult) -> None:
        if run_id is None:
            return
        c = result.cell
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO apix.collection_cell "
                    "(run_id, source_code, origin, destination, window_days, "
                    " departure_date, attempts, outcome, quotes, error_detail, "
                    " finished_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
                    "ON CONFLICT DO NOTHING",
                    (run_id, c.source_code, c.origin, c.destination, c.window_days,
                     (self._scrape_date or dt.date.today())
                     + dt.timedelta(days=c.window_days),
                     result.attempts, result.outcome,
                     result.quotes if result.outcome == OK else 0,
                     result.error_detail))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record cell %s: %s", c, exc)

    def close(self, report: CycleReport) -> None:
        if report.run_id is None:
            return
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE apix.collection_run SET finished_at = now(), "
                    "ok_cells = %s, failed_cells = %s, quotes_written = %s, "
                    "notes = %s WHERE run_id = %s",
                    (report.ok, report.failed, report.quotes_written,
                     report.cleaning_summary, report.run_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("could not close collection_run: %s", exc)
