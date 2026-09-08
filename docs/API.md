# Public API

```bash
make api          # serves on :8000
make api-check    # smoke-test every endpoint
```

Interactive docs at <http://localhost:8000/docs>, OpenAPI at `/openapi.json`.

## The rule every response follows

**No endpoint returns a bare index number.**

Anything carrying an index value also carries a `provenance` block: whether the
underlying fares were observed or synthetic, what share of the basket was
present, and what share of passenger traffic the observable carriers represent.

A consumer wiring this series into a policy input will not go looking for a
methodology page before putting the number on a chart. So the caveats travel with
the figure. It costs a few hundred bytes per response, which is the correct
trade.

```json
{
  "index": { "index_date": "2026-09-04", "index_value": "103.0839",
             "coverage_ratio": "1.0000", "observed_pax_share": "0.0829" },
  "change_vs_previous": -0.0148,
  "provenance": {
    "seeded": true,
    "basis": "SEEDED — figures are computed from synthetic quotes and are not observations of the market",
    "observable_share": 0.082912,
    "observable_carriers": ["Akasa Air", "SpiceJet"]
  }
}
```

Check `provenance.seeded` before use. The CSV export repeats the basis on every
row, because a downloaded file gets separated from its API response immediately.

## Endpoints

| Path | Purpose |
|---|---|
| `GET /v1/index/latest` | Most recent value with change on the previous period |
| `GET /v1/index/series` | Historical series. `?format=csv` for a spreadsheet |
| `GET /v1/index/routes` | Per-sector breakdown for one date |
| `GET /v1/index/routes/{code}` | Series for one sector |
| `GET /v1/index/cells` | Elementary indices, the level at which Jevons runs |
| `GET /v1/index/leadtime` | Observed fare levels by advance-purchase window |
| `GET /v1/methodology` | Formulae, reference periods, basket, limitations |
| `GET /v1/compliance` | Source audit, request summary, robots.txt snapshots |
| `GET /v1/compliance/requests` | Raw request log, refusals included |
| `GET /health` | Liveness plus whether an index is available |

All take `frequency` of `daily`, `weekly` or `monthly` where it applies.

## Three design decisions

**Versioned paths.** Everything is under `/v1`. A consumer wiring this into a
pipeline needs to know a later change will not silently alter what they receive.

**Contributions sum to the headline.** `/v1/index/routes` returns each sector's
weight and contribution, and `contributions_sum` alongside `headline_index`. The
aggregation is checkable with a calculator rather than on trust.

**Compliance is an endpoint, not a document.** `/v1/compliance` serves the source
audit, the request log and the stored robots.txt snapshots. Refused requests are
served rather than filtered out — they are the evidence that refusals were
enforced rather than merely intended:

```json
{ "source": "cleartrip", "robots_allowed": false,
  "outcome": "blocked_by_robots",
  "robots_rule": "robots:disallow:/flights/search*" }
```

## Deployment

The API needs PostgreSQL, so it is not on Vercel with the dashboard. The
dashboard reads a static JSON export instead, which is what makes the demo
immune to a conference network.

To deploy the API for real, it needs a hosted Postgres (Neon, Supabase, RDS) and
any container host. Both the API and the export read provenance from
`apix/reference/provenance.py`, so a deployed API and the static dashboard cannot
disagree about whether figures are observed or synthetic.

## Not built yet

- Authentication. The series is public and read-only, so there is nothing to
  protect; a production deployment would still want rate limiting.
- SDMX. MoSPI and RBI consume SDMX-ML for official series. The JSON here carries
  the same information but an SDMX endpoint would be needed for real integration.
- A revision policy, and the `vintage` parameter that would go with it.
