# Deployment

## The short version

APIx does not fit on Vercel alone, and pretending otherwise would produce a demo
that breaks the first time it tries to collect data. Two of the four components
belong somewhere else.

| Component | Vercel? | Why |
|---|---|---|
| React dashboard | **Yes** | Static SPA build, exactly what Vercel is for |
| Read-only FastAPI | **Yes, with care** | Serverless Python functions; see the cold-start note below |
| PostgreSQL | **No** | Vercel runs no databases. Needs a managed Postgres |
| Playwright scraper | **No** | Explained below. This is not a configuration problem |

## Why the scraper cannot run on Vercel

Three independent blockers, any one of which is fatal.

**Execution time.** One collection cycle is 8 routes times 5 windows per source, with a
deliberate 8-second delay between requests. That is roughly 4 minutes of
deliberate waiting per source, by design, because the politeness floor is the
entire ethical argument. Vercel functions cap at 60 seconds on Pro and 300 on
Enterprise. Shortening the delay to fit would mean abandoning the compliance
posture to satisfy a hosting choice, which is backwards.

**Bundle size.** Chromium is roughly 300 MB. Vercel's serverless bundle limit is
250 MB uncompressed.

**Statefulness.** The throttler holds per-origin timing state so consecutive
requests stay spaced. Serverless functions are stateless and concurrent, so N
cold starts would fire N simultaneous requests at the same origin, which is
precisely the behaviour the rate limiter exists to prevent.

The scraper belongs on a small always-on VM or a container with a cron schedule.
A single shared-CPU instance is sufficient: the workload is dominated by
deliberate sleeping, not computation.

## Recommended topology

```
Vercel                         Managed Postgres            Small VM / container
├── dashboard (static SPA)  ─→ ├── apix schema        ←──  └── scraper + index
└── /api/* (FastAPI, read)  ─→ └── read replica opt.        (daily cron)
```

The scraper writes, the API only reads. That split is worth keeping even outside
Vercel: it means a compromised or overloaded public API cannot corrupt the series.

## Status

The API is built and the Vercel scaffolding is in place:

| File | Purpose |
|---|---|
| `api/index.py` | Vercel serverless entry point; sets `APIX_DISABLE_POOL` |
| `vercel.json` | Python runtime build and catch-all route |
| `.vercelignore` | Keeps the scraper, tests and reference CSVs out of the bundle |

Two things remain, and both need you rather than me.

1. **A managed Postgres.** Neon suits this best: plain Postgres 16, a free tier
   adequate for the demo, and a connection pooler that solves the problem below.
   Supabase and Vercel Postgres work equally well. Set `DATABASE_URL` to the
   **pooled** endpoint, not the direct one.
2. **Account access.** Deploying publishes under your Vercel account. The CLI is
   installed here (v57) but not authenticated, and I will not authenticate as
   you.

Once you have both:

```bash
vercel link && vercel env add DATABASE_URL production && vercel deploy --prod
```

Then load the schema and seed the remote database:

```bash
psql "$DATABASE_URL" -f db/schema.sql && python scripts/load_reference.py
python -m apix.cli seed --start 2026-07-01 --end 2026-09-04
python -m apix.cli base && python -m apix.cli index
```

## Serverless notes

**Connection pooling was the trap, and it is handled.** `apix/db.py` builds a
process-global pool, which is correct for a long-lived uvicorn server and wrong
for serverless, where each invocation is its own process and a pool per
invocation exhausts `max_connections` under a spike. `APIX_DISABLE_POOL=true`
turns the in-process pool off so the database's own pooler does the work. Both
modes are exercised by the test suite.

**Cold starts.** A Python function importing pandas or numpy starts slowly. The
read API imports neither: it serves rows the index engine already computed. Keep
analytics imports out of the API path.

**Seeded data announces itself.** `provenance.is_seeded` is derived by asking the
database which sources the fares came from, not from an environment variable, so
a deployed demo cannot accidentally present synthetic numbers as measurements
because someone forgot a flag. `/v1/methodology` adds a matching limitation line
when seeded data is present.

## Local, for now

Everything runs locally today with no cloud account at all:

```bash
make demo
```

That builds a clean database, seeds 66 days, computes base prices, and builds
38 daily index values in about 14 seconds.
