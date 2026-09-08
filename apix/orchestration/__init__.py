"""Scheduled collection: walking the route x window matrix, day after day.

The brief asked for scheduled daily extraction. Until now only a manual CLI
existed, which is a demo, not a collection system. This package is the
difference between the two.

    matrix.py    what to collect  -- the plan, and what it will cost in wall clock
    runner.py    how to collect it -- retries, isolation, concurrency
    backfill.py  what to do when a day was missed  -- which is mostly "admit it"
    schedule.py  when to collect  -- a daily loop, and the cron recipe it replaces

The one idea worth carrying out of here: **a missed fare observation is
permanently missed.** Everything in `backfill.py` follows from that.
"""
from apix.orchestration.matrix import Cell, CollectionPlan, build_plan
from apix.orchestration.runner import CellResult, CycleReport, CycleRunner

__all__ = [
    "Cell", "CollectionPlan", "build_plan",
    "CellResult", "CycleReport", "CycleRunner",
]
