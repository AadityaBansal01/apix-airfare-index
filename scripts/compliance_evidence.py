#!/usr/bin/env python3
"""Generate genuine compliance evidence against live sources.

Makes a small number of real requests so the audit trail contains observed
robots.txt snapshots, real inter-request gaps, and real refusals, rather than
only the zero-delay rows the synthetic seeder writes.

What this does, precisely:

*   Fetches robots.txt from each source and stores it verbatim with its hash.
*   Fetches two or three permitted pages from the one airline whose robots.txt
    allows it, at the configured delay.
*   ATTEMPTS a URL that robots.txt disallows on each excluded source, so the
    trail shows a refusal that was enforced rather than merely intended. The
    request is never sent; the gate refuses it before egress and writes the
    refusal row.

Total footprint is roughly a dozen requests spread over a couple of minutes,
which is less than one human opening the sites in a browser.

    python scripts/compliance_evidence.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from apix.ethics.audit import PostgresAuditWriter  # noqa: E402
from apix.net.session import (  # noqa: E402
    FetchFailed,
    PolicyEnforcedSession,
    PolicyRefused,
    load_policies,
)
from apix.settings import Settings  # noqa: E402

#: Pages we may fetch, on the source whose robots.txt permits it.
PERMITTED = [
    ("akasa", "https://www.akasaair.com/"),
    ("akasa", "https://www.akasaair.com/about-us"),
]

#: URLs we expect to be refused. Listing them is the point: the trail should
#: show that the system tried the disallowed path and was stopped, not that it
#: quietly never looked.
EXPECT_REFUSAL = [
    ("indigo", "https://www.goindigo.in/booking/search"),
    ("makemytrip", "https://www.makemytrip.com/flight/search"),
    ("cleartrip", "https://www.cleartrip.com/flights/search"),
    ("akasa", "https://www.akasaair.com/payment/confirm"),
]


async def main() -> int:
    session = PolicyEnforcedSession(
        PostgresAuditWriter(),
        policies=load_policies(),
        settings=Settings(store_raw_bodies=False, max_retries=1),
    )

    print("permitted fetches")
    for source, url in PERMITTED:
        try:
            r = await session.get(url, source_code=source)
            print(f"  {source:<12} {url:<45} HTTP {r.status}  {len(r.body):>8,}B")
        except (FetchFailed, PolicyRefused) as exc:
            print(f"  {source:<12} {url:<45} {type(exc).__name__}: {str(exc)[:44]}")

    print("\nattempted, expected to be refused")
    for source, url in EXPECT_REFUSAL:
        try:
            await session.get(url, source_code=source)
            print(f"  {source:<12} {url:<45} NOT REFUSED — investigate")
        except PolicyRefused as exc:
            kind = "intent rule" if exc.intent_denied else "robots.txt"
            print(f"  {source:<12} {url:<45} refused by {kind}")
        except FetchFailed as exc:
            print(f"  {source:<12} {url:<45} fetch failed: {str(exc)[:34]}")

    await session.aclose()

    from apix import db

    print("\ncompliance summary")
    rows = db.fetch_all("SELECT * FROM apix.v_compliance_summary ORDER BY source")
    hdr = f"  {'source':<14}{'req':>5}{'sent':>6}{'blocked':>9}{'min gap':>9}{'avg gap':>9}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        g = lambda v: f"{float(v):.1f}s" if v is not None else "    –"
        print(f"  {r['source']:<14}{r['requests']:>5}{r['requests_sent']:>6}"
              f"{r['robots_blocked']:>9}{g(r['min_delay_s']):>9}{g(r['avg_delay_s']):>9}")

    snaps = db.fetch_all(
        "SELECT s.code, r.robots_url, r.http_status, length(r.body) AS bytes "
        "FROM apix.robots_snapshot r JOIN apix.source s USING (source_id) "
        "ORDER BY s.code")
    print(f"\nrobots.txt snapshots stored: {len(snaps)}")
    for s in snaps:
        print(f"  {s['code']:<14}{s['robots_url']:<44} HTTP {s['http_status']}  "
              f"{s['bytes']}B")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
