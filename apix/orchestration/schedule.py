"""When to collect.

APScheduler was the obvious choice and is not used, for the reason stated at the
top of `apix/settings.py`: a dependency in a path that must not fail to import is
a liability, and this path is thirty lines of asyncio. What follows is a daily
loop with the two properties that actually matter, plus the cron recipe it is
equivalent to, so an operator can pick either.

WHY A FIXED LOCAL CLOCK TIME, NOT AN INTERVAL
---------------------------------------------
"Every 24 hours" drifts: a cycle that takes 40 minutes moves the next start 40
minutes later, and after a month the index is being observed at a different time
of day than it was at the start. Airfares move intraday, so that drift is a
measurement artefact, not a scheduling detail. The loop therefore targets a wall
clock time in Asia/Kolkata and computes the sleep to the next occurrence.

WHY 02:00 IST
-------------
Two reasons, one methodological and one about being a good guest. Collecting at
the same quiet hour each day holds the time-of-day effect constant across the
series, so a day-over-day change is a change in fares rather than in when we
looked. And it puts our traffic at an airline's lowest-load hour.

THE CRON EQUIVALENT
-------------------
    # /etc/cron.d/apix -- daily collection at 02:00 IST
    CRON_TZ=Asia/Kolkata
    0 2 * * *  apix  cd /srv/apix && make scrape >> /var/log/apix/scrape.log 2>&1
    30 4 * * * apix  cd /srv/apix && make index >> /var/log/apix/index.log 2>&1

The gap between the two is deliberate: the index is built after collection has
finished rather than as part of it, so a failed collection produces a short day
rather than a failed index build.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

log = logging.getLogger("apix.orchestration.schedule")

IST = ZoneInfo("Asia/Kolkata")

#: The daily collection hour, in IST. See the module docstring.
DEFAULT_HOUR = 2
DEFAULT_MINUTE = 0


def next_run_at(now: dt.datetime, hour: int = DEFAULT_HOUR,
                minute: int = DEFAULT_MINUTE) -> dt.datetime:
    """The next occurrence of hour:minute IST strictly after `now`.

    Pure, so the schedule is testable without waiting a day for it.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    local = now.astimezone(IST)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += dt.timedelta(days=1)
    return target


def seconds_until(now: dt.datetime, hour: int = DEFAULT_HOUR,
                  minute: int = DEFAULT_MINUTE) -> float:
    return (next_run_at(now, hour, minute) - now.astimezone(IST)).total_seconds()


@dataclass(slots=True)
class DailySchedule:
    """Runs one coroutine per day at a fixed IST wall-clock time."""

    hour: int = DEFAULT_HOUR
    minute: int = DEFAULT_MINUTE
    max_cycles: int | None = None      # None = forever; a number bounds a test
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(IST)

    async def run(self, cycle: Callable[[dt.date], Awaitable[object]]) -> list:
        """Sleep to the next slot, run one cycle, repeat.

        A cycle that raises is logged and the schedule continues. A scheduler
        that dies on one bad night is worse than no scheduler, because the
        failure is silent from the next morning onwards.
        """
        out = []
        n = 0
        while self.max_cycles is None or n < self.max_cycles:
            wait = seconds_until(self.now(), self.hour, self.minute)
            log.info("next collection in %.0f min", wait / 60)
            await self.sleep(wait)
            day = self.now().astimezone(IST).date()
            try:
                out.append(await cycle(day))
            except Exception as exc:  # noqa: BLE001 - one bad night is not fatal
                log.exception("collection cycle for %s failed: %s", day, exc)
                out.append(exc)
            n += 1
        return out


CRON_RECIPE = """\
# /etc/cron.d/apix — APIx daily collection
# Installed by an operator who prefers cron to a long-lived process. Equivalent
# to `python -m apix.cli schedule`, minus the supervision problem.
CRON_TZ=Asia/Kolkata
SHELL=/bin/bash

# 02:00 IST — collect the route x window matrix for today.
0 2 * * *   apix  cd /srv/apix && make scrape  >> /var/log/apix/scrape.log 2>&1

# 04:30 IST — build the index from whatever collection managed to get. Separate
# so a short collection produces a short day rather than no index at all.
30 4 * * *  apix  cd /srv/apix && make index   >> /var/log/apix/index.log  2>&1

# 05:00 IST — record any cell yesterday's cycle never collected. Writes gaps,
# never invents observations: see apix/orchestration/backfill.py.
0 5 * * *   apix  cd /srv/apix && python -m apix.cli backfill --date yesterday \\
                    >> /var/log/apix/backfill.log 2>&1
"""
