# Dashboard

Live: <https://dashboard-b8w2t26ct-tushkums-projects.vercel.app>

## Visual language

Follows MakeMyTrip: a blue gradient header band, a white control card overlapping
it where MMT puts its search widget, light grey ground, white pill-cornered cards
with soft shadows, and a saffron-to-orange gradient reserved for emphasis. The
intent is that an Indian judge recognises the idiom immediately.

An earlier direction used a dark green and gold sidebar; those tokens are still
in `dashboard/src/styles/tokens.css` if that look is ever wanted again. The live
tokens are at the top of `dashboard/src/styles/app.css`.

**What transfers is the chrome. What does not is the information architecture.**
MakeMyTrip is a booking product; APIx is an official-statistics product. There is
no search, nothing is purchasable, and every figure carries its provenance.

| MakeMyTrip element | APIx equivalent |
|---|---|
| Search widget (from / to / date / travellers) | Index state: value, as-of date, basket size, coverage, traffic observed |
| One way / Round trip / Multi city pills | Daily / Weekly / Monthly frequency pills |
| Result list of bookable fares | Route index table with weights and contributions |
| "₹1,572 · BOOK NOW" | Index value and contribution. Nothing is clickable-to-buy |
| Price range slider | Lead-time curve: fare against advance-purchase window |

## Views

1. **Overview** — headline APIx, day and period change, the daily series with
   anomaly flags, and the anomaly table.
2. **Sectors** — heatmap of route index by day, plus the route breakdown table
   whose contributions sum to the headline.
3. **Lead time** — mean fare against booking window per sector, and the
   last-minute premium table.
4. **Compliance** — the source audit and the request log.
5. **Methodology** — formulae, reference periods, missing-data policy, basket,
   and known limitations.

## Two non-negotiable rules

**Seeded data is always labelled.** `provenance.seeded` is derived from the
source registry by the export script, never asserted by hand. When true, a
non-dismissible banner appears on every view carrying an index value, and the
footer repeats it. A seeded number must never be readable as a measurement.

**Coverage is always visible.** The 8.3% observable-traffic figure and the
basket coverage ratio appear in the control card on every view, and the Overview
carries the full explanation of which carriers are excluded and why.

## Data flow

The deployed dashboard reads static JSON, not a database.

```
PostgreSQL → scripts/export_static.py → dashboard/public/data/*.json → Vite build → Vercel
```

The index is computed locally by the engine the tests cover, then exported. No
database runs in production, so the demo cannot fail because a conference network
is down. The cost is that deployed figures are a snapshot; `provenance.generated_at`
records when, and the footer shows it.

Rebuild and redeploy:

```bash
make rebuild    # reset, seed, base prices, index
make evidence   # real robots.txt + audit rows from live sources
make deploy     # export, build, ship to Vercel
```
