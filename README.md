# APIx — a daily airfare price index for India

Response to Smart India Hackathon 2026 Problem Statement **SIH26056**,
"Real-time Airfare Price Index", MoSPI (DIID).

A daily, weighted airfare price index for eight Indian trunk routes. It uses the
same elementary and upper-level formulas as India's CPI 2024 series (Jevons, then
Young / modified Laspeyres) with weights from DGCA passenger traffic, so it could
augment the CPI Transport division rather than sit beside it as a curiosity.

**Live dashboard: https://apix-india.vercel.app**
· [Methodology](docs/METHODOLOGY.md) · [Source audit](docs/SOURCE_AUDIT.md) · [API](docs/API.md)

An independent prototype. Not affiliated with, endorsed by, or published by
MoSPI, DGCA, or any airline named here.

> **Status: the pipeline is built and tested; no real fares have been collected
> yet.** Every number in this document, in the demo and on the dashboard comes
> from a synthetic fare generator. The collection, cleaning and index code is the
> same code a live run would use, so this demonstrates that the system works. It
> does not describe what Indian airfares did.

---

## Run it

Needs **Docker**, **Python 3.12**, and **Node 20+** for the dashboard.
`make init` and the first `npm install` need the network; after that the
pipeline, API and tests run offline.

```bash
python3 -m venv .venv && source .venv/bin/activate   # make init installs into
                                                     # whatever Python is active
make init      # python deps + playwright chromium
make demo      # database, 66 days of synthetic data, base prices, index  (~10s)
make api       # http://localhost:8000/docs
```

`make demo` takes about ten seconds once the `postgres:16-alpine` image, the
Python wheels and the Playwright Chromium build are cached. A first-ever run
downloads all three and takes considerably longer. The API's `/docs` page loads
Swagger UI from a CDN, so it needs the network; the JSON endpoints do not.

In a second shell:

```bash
make export     # write the dashboard's JSON payload
make dashboard  # http://localhost:5173
```

The dashboard is a static export and does not need the API running. It opens on
a plain-language page written for a reader who is not a statistician; the analyst
views are behind the tabs beside it.

`make demo` drops and recreates the schema every run, so it is safe to re-run.

```bash
make test       # 276 with a database; 218 pass and 58 skip without one
make backtest   # internal consistency checks and weight sensitivity
make compliance # audit-log summary per source, straight from the database
make evidence   # re-run robots.txt checks against the live sites (needs network)
```

Two notes on those. `make backtest` does **not** validate the index against an
external benchmark; no such benchmark exists (see below). And after a seeded
`make demo` the compliance log holds only the seeder's own 66 rows, because no
live collection has run: the robots.txt refusals behind the exclusion table
below come from `make evidence`, which contacts the live origins.

---

## Collecting on a schedule

```bash
make scrape-plan   # what a cycle would collect, and how long it would take. No network.
make scrape        # run one cycle over the route x window matrix
make backfill      # assess yesterday; re-run it if recoverable, record the gap if not
make cron          # print the crontab that runs all of the above daily at 02:00 IST
```

`make scrape-plan` reaches no network at all and prints the same plan object the
runner executes, so it cannot describe something other than what would happen:

```
collection plan for 2026-09-09
  cells        : 80
  sources      : akasa, spicejet
  est. runtime : 6.0 min (360s, sources run concurrently)
```

**A missed day cannot be backfilled, and `backfill` refuses to pretend otherwise.**
A fare is a price quoted at a moment for a departure a fixed number of days away.
If Friday's cycle never ran, the T+7 price for a Friday-plus-7 departure as it
stood on Friday is gone; asking today returns a T+3 price, which belongs in a
different cell and is systematically higher. So `backfill` re-runs only a date
that is still today. For anything older it writes the missing cells as
`outcome='missed'`, and the index engine's existing imputation path carries them
with the published coverage ratio falling to match. See METHODOLOGY.md §5.1.

---

## What it does

Collects airfares daily across eight trunk routes and five advance-purchase
windows, cleans them, and computes a price index that mirrors MoSPI methodology:
**Jevons** at the elementary level (a geometric average of price changes),
**Young / modified Laspeyres** above it (fixed weights from an earlier period),
with route weights from DGCA passenger traffic.

The gap it closes is **frequency**. MoSPI already collects airfares online, but
records a price once a month. The problem statement's premise is that this sector
can swing 200 to 400 percent within a booking window; if so, monthly sampling
misses most of what happens. We have not independently verified that range, and
the synthetic generator used here encodes a milder 1.6x to 2.3x last-minute
premium.

What a monthly visit misses is measurable on the demo data. **These fares are
generated, not collected**, and the surge below was deliberately injected to test
the detector, so read this as what the system would surface, not as evidence
about Indian airfares:

| Synthetic demo dataset | Index |
|---|---:|
| Peak, 23 August | 143.22 |
| What a mid-month collector would have recorded | 101.78 |
| Missed | **41.4 points, 41%** |

The rise started and finished between two monthly visits.

---

## Three findings that changed the design

Each was verified against a primary source, and each contradicts an assumption in
the problem statement, so they are stated here rather than in a supporting
document.

**MoSPI already collects airfares online.** The CPI 2024 FAQ, answer 27, says so
plainly. The argument for this project is frequency and coverage of the booking
window, not online versus manual collection.

**"Transport and communication" no longer exists.** CPI 2024 adopted COICOP 2018,
splitting it into Division 07 Transport and Division 08 Information and
communication. Transport's published weight is **8.796%** (Combined), against
6.394% for the same grouping under the CPI 2012 structure. Any overlay must
target Division 07 or it compares airfares against mobile tariffs. Both figures
are quoted from the CPI 2024 FAQ; they are not derived in this repo.

**There is no public DGCA monthly average-fare series.** DGCA's Tariff Monitoring
Unit checks 78 routes monthly as a regulatory function; what DGCA publishes is
traffic and load factor. So the external back-test comparison ships unrun rather
than scored against numbers nobody can check.

---

## The honest constraint

The index **may legally observe** about 8.3% of Indian domestic passenger
traffic, and currently **collects from one carrier**.

| Carrier | Share | Status |
|---|---:|---|
| IndiGo | 63.9% | robots.txt disallows its booking paths |
| Air India | 15.2% | behind bot management that only evasion would pass |
| Air India Express | 11.5% | robots.txt disallows |
| Akasa Air | 5.3% | **adapter written and smoke-tested; no live run yet** |
| SpiceJet | 3.0% | cleared by the audit, adapter not yet written |

Seven of the eleven audited sources forbid automated access to flight search in
their own robots.txt: IndiGo, Air India Express, MakeMyTrip, Cleartrip,
EaseMyTrip, Ixigo and Goibibo. Two more, Air India and Yatra, permit it but
enforce an anti-automation control that only evasion would get past.

Excluding them is a compliance decision, not a capability limit, and it is the
single biggest constraint on the project. Closing it needs a licensed aggregator
feed or a data-sharing arrangement with DGCA, not a better scraper.

Every published index value carries an `observed_pax_share` field, so a reader
who inspects the data can see it is not a market-wide measure. `make evidence`
re-runs the robots.txt checks behind this table against the live origins and
writes the refusals into the audit log. Full reasoning, with each file quoted
verbatim, in [docs/SOURCE_AUDIT.md](docs/SOURCE_AUDIT.md).

---

## Layout

```
apix/
  ethics/        robots.txt matcher (RFC 9309), audit log, compliance gate
  net/           rate limiter, crawler identity, policy-enforced session
  sources/       FareSource interface + Akasa Air and SpiceJet adapters
  orchestration/ the daily cycle: plan, runner, backfill, schedule
  pipeline/      cleaning, outlier detection, persistence
  index/         Jevons elementary, Young aggregation, index build
  validation/    back-test metrics and the validation suite
  analytics/     lead-time elasticity, anomaly detection, nowcast evaluation
  reference/     DGCA and MoSPI reference-data loaders
  api/           FastAPI service (read-only)
  cli.py         scrape / backfill / schedule / seed / base / index / series / nowcast
  seed.py        synthetic fare generator for the demo
api/           Vercel serverless entry point, wraps apix/api
config/        basket, weights, per-source scraping policy
db/schema.sql  PostgreSQL schema; constraints enforce the methodology
dashboard/     React + Recharts, static export
docs/          methodology, source audit, deployment, dashboard, API
scripts/       weights, seeding, export, back-test, reference loaders
tests/         385 tests (2 skip without a database)
```

---

## Where the numbers come from

| Input | Source | Licence |
|---|---|---|
| Route weights | DGCA monthly city-pair passenger traffic | ODbL-1.0 (open licence, attribution required) |
| Carrier shares | DGCA monthly domestic carrier statistics | ODbL-1.0 |
| CPI structure and weights | MoSPI CPI 2024 series FAQ | Public |
| Fares | Akasa Air, within its robots.txt | See source audit |

Route weights are computed, not typed in: `make weights` rebuilds them from the
DGCA dataset. The demo's fares are synthetic and labelled as such everywhere they
appear.

---

## Two rules the code enforces

**A disallowed URL is not fetched, and cannot be recorded as a successful
fetch.** The adapter has no socket of its own. Every request goes through a
session that applies our own stricter intent rules, then robots.txt, then the
rate limiter, and writes an audit row in a `finally`. Redirects are re-checked
against the URL actually landed on, so an allowed URL cannot be used to reach a
disallowed one. A database CHECK constraint refuses any audit row that marks a
URL disallowed and still reports success, so a crawler bug cannot be silently
written as a clean fetch.

The standard library was not good enough for this. `urllib.robotparser` ignores
wildcards and takes the first matching rule instead of the most specific, so
against the committed snapshots it permits paths Cleartrip and Goibibo plainly
disallow. `apix/ethics/matcher.py` implements RFC 9309 properly, and 34 tests
cover it using rules taken verbatim from the audited files.

**A fare's components either reconcile or are all absent.** Either base, taxes,
UDF (User Development Fee) and fees sum to the total within one rupee, or all
five are cleared and only the observed total is kept. Enforced in the cleaner and
again by a schema constraint, so a partial or fabricated decomposition cannot be
stored.

---

## Documentation

| Document | What is in it |
|---|---|
| [METHODOLOGY.md](docs/METHODOLOGY.md) | Formulas in LaTeX, worked example with real DGCA weights, cleaning rules, missing-data policy, validation, limitations |
| [SOURCE_AUDIT.md](docs/SOURCE_AUDIT.md) | Every source, its robots.txt verbatim, bot-management fingerprinting, and why each exclusion was made |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Why the scraper cannot run on Vercel, and what can |
| [API.md](docs/API.md) | Endpoint reference |
| [DASHBOARD.md](docs/DASHBOARD.md) | Views and the two non-negotiable UI rules |

---

## What this does not yet do

1. **No real fares have been collected.** The pipeline is real and tested; the
   prices are not. The basket is 8 routes times 5 advance windows, 40 cells a
   day, one carrier at present, which is roughly 180 quotes a day. Reaching the
   30-day minimum the problem statement asks for therefore takes about four
   weeks; matching this demo's 66-day span takes just over two months. The
   8-second crawl delay is not the limiting factor, the basket size is.
2. **The external back-test does not run.** No public DGCA average-fare series
   exists to score against. The harness is complete and runs the moment a
   reference is loaded with `scripts/load_dgca_fares.py`.
3. **The CPI overlay ships empty.** MoSPI's data endpoints return the portal
   application rather than data from outside India. `scripts/load_cpi.py` fills
   it from a CSV exported by hand.
4. **Advance-window weights are assumed, not measured.** No public
   booking-lead-time distribution for India exists. Equal weighting is the
   default; the back-test shows the headline moves at most 2.63 index points
   across plausible alternatives.
5. **Only one scraper adapter is written.** SpiceJet is cleared by the audit and
   is next.
6. **The naive-benchmark comparison is not yet informative.** On synthetic data
   the index and an unweighted mean of fares correlate 0.95, because the
   generator moves every route together. That says nothing about whether the
   methodology earns its complexity; only real data, where routes move
   independently, can answer that.

---

## Licence and attribution

Contains DGCA and Ministry of Civil Aviation data under ODbL-1.0. CPI structure
and weights from MoSPI publications. Airline names and trademarks belong to their
owners; this project is not affiliated with any of them.
