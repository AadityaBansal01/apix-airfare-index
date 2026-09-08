"""The collection plan: which cells, in what order, at what cost.

A plan is pure data computed from config. It performs no I/O, which is what
makes `--dry-run` meaningful: the plan you print is the same object the runner
executes, not a description of it written separately and free to drift.

ORDERING IS NOT INCIDENTAL
--------------------------
Cells are interleaved across sources, so that consecutive cells hit *different*
origins wherever possible. With two sources and a per-origin floor of 8 seconds,
an interleaved order lets the throttle for one origin recover while the other is
being fetched, halving wall clock without shortening any origin's spacing by a
single millisecond. Grouping by source instead would serialise everything behind
one throttle for no benefit to anyone.

WHY WALL CLOCK IS PART OF THE PLAN
----------------------------------
The rate limits this project commits to are the binding constraint on how much
data it can ever hold, and that fact should be visible before a run starts, not
discovered after it. `estimated_seconds` is deliberately pessimistic: it charges
the full politeness delay for every cell including the first, and adds the mean
jitter rather than assuming the best case.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from apix.sources.base import SearchRequest


@dataclass(frozen=True, slots=True)
class Cell:
    """One route x window x source unit of collection."""
    source_code: str
    origin: str
    destination: str
    window_days: int

    def request(self, scrape_date: dt.date) -> SearchRequest:
        return SearchRequest(
            origin=self.origin,
            destination=self.destination,
            departure_date=scrape_date + dt.timedelta(days=self.window_days),
            scrape_date=scrape_date,
        )

    @property
    def route_code(self) -> str:
        return f"{self.origin}-{self.destination}"

    def __str__(self) -> str:
        return f"{self.source_code}:{self.route_code}:T+{self.window_days}"


@dataclass(frozen=True, slots=True)
class SourceCost:
    """What one source charges us in wall clock, from its own policy."""
    code: str
    min_delay_seconds: float
    jitter_seconds: float

    @property
    def mean_interval(self) -> float:
        # Jitter is uniform on [0, jitter] and additive, so its mean is half.
        return self.min_delay_seconds + self.jitter_seconds / 2.0


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    scrape_date: dt.date
    cells: tuple[Cell, ...]
    costs: dict[str, SourceCost] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.cells)

    @property
    def sources(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for c in self.cells:
            seen.setdefault(c.source_code, None)
        return tuple(seen)

    def for_source(self, code: str) -> tuple[Cell, ...]:
        return tuple(c for c in self.cells if c.source_code == code)

    def estimated_seconds(self) -> float:
        """Wall clock for the whole cycle, assuming sources run concurrently.

        Each origin is serialised by its own throttle, so a source's own cells
        cost `n * mean_interval` no matter how many workers are pointed at it.
        Different origins genuinely overlap. The cycle therefore takes as long
        as its slowest source, not the sum across sources.
        """
        if not self.cells:
            return 0.0
        per_source = []
        for code in self.sources:
            n = len(self.for_source(code))
            cost = self.costs.get(code)
            interval = cost.mean_interval if cost else 8.0
            per_source.append(n * interval)
        return max(per_source)

    def describe(self) -> str:
        secs = self.estimated_seconds()
        lines = [
            f"collection plan for {self.scrape_date}",
            f"  cells        : {len(self.cells)}",
            f"  sources      : {', '.join(self.sources) or 'none'}",
            f"  routes       : {len({c.route_code for c in self.cells})}",
            f"  windows      : {sorted({c.window_days for c in self.cells})}",
            f"  est. runtime : {secs / 60:.1f} min "
            f"({secs:.0f}s, sources run concurrently)",
        ]
        for code in self.sources:
            cost = self.costs.get(code)
            n = len(self.for_source(code))
            if cost:
                lines.append(
                    f"    {code:<12} {n:>4} cells @ {cost.mean_interval:.1f}s "
                    f"mean interval = {n * cost.mean_interval / 60:.1f} min")
        return "\n".join(lines)


def _interleave(groups: Sequence[Sequence[Cell]]) -> tuple[Cell, ...]:
    """Round-robin across sources so consecutive cells hit different origins."""
    out: list[Cell] = []
    i = 0
    while True:
        added = False
        for g in groups:
            if i < len(g):
                out.append(g[i])
                added = True
        if not added:
            return tuple(out)
        i += 1


def build_plan(
    basket: dict,
    source_codes: Iterable[str],
    scrape_date: dt.date,
    *,
    policies: dict | None = None,
    routes: Iterable[str] | None = None,
    windows: Iterable[int] | None = None,
) -> CollectionPlan:
    """Expand config into the full matrix.

    `routes` and `windows` narrow the plan without editing config, which is what
    makes a partial re-run of a failed cycle expressible on the command line.
    """
    wanted_routes = {r.upper() for r in routes} if routes else None
    wanted_windows = set(windows) if windows else None

    pairs = [
        (r["origin"], r["destination"])
        for r in basket["routes"]
        if wanted_routes is None
        or r["code"].upper() in wanted_routes
        or f'{r["origin"]}-{r["destination"]}'.upper() in wanted_routes
    ]
    window_days = [
        w["days"] for w in basket["advance_windows"]
        if wanted_windows is None or w["days"] in wanted_windows
    ]

    groups = []
    for code in source_codes:
        groups.append([
            Cell(source_code=code, origin=o, destination=d, window_days=w)
            for (o, d) in pairs
            for w in window_days
        ])

    costs = {}
    for code, pol in (policies or {}).items():
        costs[code] = SourceCost(
            code=code,
            min_delay_seconds=pol.min_delay_seconds,
            jitter_seconds=pol.jitter_seconds,
        )

    return CollectionPlan(
        scrape_date=scrape_date,
        cells=_interleave(groups),
        costs=costs,
    )


def load_basket(path: str | Path) -> dict:
    import yaml  # noqa: PLC0415

    return yaml.safe_load(Path(path).read_text())
