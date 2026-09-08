# APIx Methodology

How the Real-time Airfare Price Index is constructed, why each choice mirrors
official Indian CPI practice, and where it deliberately does not.

---

## 1. Alignment with the CPI 2024 series

APIx is designed to slot into MoSPI's **current** CPI series, not the superseded one.
The facts below are from MoSPI's *Frequently Asked Questions (FAQs) on CPI 2024 Series*
and drive the formula choices in §3.

| Property | CPI 2024 (MoSPI) | APIx |
|---|---|---|
| Index reference period | 2024 = 100 | Configurable price reference period, default a 28-day window |
| Weight reference period | HCES 2023-24 | DGCA city-pair traffic, rolling 12 months |
| Classification | COICOP 2018: 12 divisions, 43 groups, 92 classes, 162 sub-classes | Maps to **Division 07 — Transport** |
| Elementary formula | **Jevons** | **Jevons** — identical |
| Upper-level formula | **Young / modified Laspeyres** | **Young / modified Laspeyres** — identical |
| Airfare collection | "collected through well-known online platforms", monthly | Automated, **daily** |
| Frequency | Monthly | Daily, aggregated to weekly and monthly |

Three consequences deserve emphasis, because they change what this project claims.

**The transport weight rose sharply.** Under CPI 2024, Transport carries **8.796%**
(Combined) against 6.394% under CPI 2012 — a 37.6% relative increase. Transport prices
now move the headline number substantially more than they used to.

**"Transport and communication" no longer exists as a single sub-group.** COICOP 2018
splits it into Division 07 Transport (8.796%) and Division 08 Information and
communication (3.609%). Any overlay chart must target Division 07, or it compares
airfares against a series half-composed of mobile tariffs.

**MoSPI already collects airfares online.** The CPI 2024 FAQ, answer 27, states plainly
that airfares are collected through online platforms, and answer 14 confirms online
collection for airfare alongside telephone and OTT. **The gap APIx closes is therefore
frequency and coverage of the advance-purchase surface, not the online/manual divide.**
CPI observes airfare monthly; a sector whose prices move 200–400% within a single booking
window is sampled twelve times a year. APIx observes it daily across five advance-purchase
windows. That is the honest claim, and it is a stronger one than "they still use paper".

---

## 2. Index structure

Three levels, mirroring CPI's item → sub-class → division hierarchy.

```
APIx (headline)                       <- Young / modified Laspeyres
 └── elementary aggregate (r, w)      <- Jevons over carriers
      └── price quote (r, w, c)       <- min total economy fare
```

**The elementary aggregate is a (route, advance-window) pair.** This is the analogue of a
CPI *item*. Carriers within that cell are the analogue of *markets/outlets*, which is
exactly the level at which MoSPI applies Jevons — the geometric mean of price relatives
across the markets where an item is priced.

**The price quote** for cell $(r,w)$ and carrier $c$ on day $t$ is the **lowest available
total economy fare** on a non-stop service, inclusive of taxes, UDF and statutory charges.
Lowest-available is what a price collector records and what a traveller faces; a mean over
booking classes would mix a product the traveller cannot buy into the price.

---

## 3. The formulae

### Notation

| Symbol | Meaning |
|---|---|
| $r$ | route (city pair) |
| $w$ | advance-purchase window in days, $w \in \{1,7,15,30,45\}$ |
| $c$ | carrier |
| $t$ | index date (daily) |
| $C_{r,w,t}$ | set of carriers observed in cell $(r,w)$ on day $t$ |
| $P^{t}_{r,w,c}$ | lowest total economy fare, INR |
| $\bar P^{0}_{r,w,c}$ | base price, price reference period $0$ |

### 3.1 Base price

The base price is the geometric mean of daily prices over the price reference period,
matching the geometric averaging MoSPI uses when linking series:

$$\bar P^{0}_{r,w,c} \;=\; \left( \prod_{t \in T_0} P^{t}_{r,w,c} \right)^{1/|T_0|}$$

Geometric rather than arithmetic because the denominator of a Jevons ratio must itself be
a geometric average, or the elementary index is biased at $t=0$ and does not start at 100.

### 3.2 Elementary index — Jevons

$$\boxed{\;I^{t}_{r,w} \;=\; 100 \times \prod_{c \in C_{r,w,t}} \left( \frac{P^{t}_{r,w,c}}{\bar P^{0}_{r,w,c}} \right)^{\frac{1}{|C_{r,w,t}|}}\;}$$

Equivalently, computed in logs for numerical stability, which is how
`apix/index/elementary.py` implements it:

$$\ln I^{t}_{r,w} \;=\; \ln 100 + \frac{1}{|C_{r,w,t}|}\sum_{c \in C_{r,w,t}} \left( \ln P^{t}_{r,w,c} - \ln \bar P^{0}_{r,w,c} \right)$$

Jevons is used because MoSPI uses it (CPI 2024 FAQ, answer 20). It also has the property
that matters most for airfares: it is invariant to the extreme right-skew of fare
distributions, where a single sold-out-adjacent quote can be five times the median. An
arithmetic mean of relatives (Carli) would let that one quote dominate the cell.

### 3.3 Upper-level index — Young / modified Laspeyres

$$\boxed{\;\text{APIx}_t \;=\; \frac{\sum_{r}\sum_{w} \omega_{r,w}\, I^{t}_{r,w}}{\sum_{r}\sum_{w} \omega_{r,w}}\;}
\qquad \omega_{r,w} = \omega^{\text{route}}_{r} \cdot \omega^{\text{win}}_{w}$$

with $\sum_r \omega^{\text{route}}_r = 1$ and $\sum_w \omega^{\text{win}}_w = 1$, so the
denominator is 1 whenever the basket is fully observed. It is written explicitly because
it is **not** 1 under missing data (§4).

**Why Young and not Laspeyres.** A true Laspeyres index takes its weights from the same
period as its base prices. Ours cannot: the weights come from DGCA traffic over a rolling
12 months, while base prices come from a 28-day price reference period. When the weight
reference period differs from the price reference period, the estimator is a **Young
index**, sometimes called a modified Laspeyres. This is precisely the form MoSPI names in
CPI 2024 FAQ answer 21, so using it is alignment rather than approximation. Calling it
"Laspeyres" would be a misstatement a price statistician would catch immediately.

### 3.4 Route weights

From DGCA monthly city-pair passenger traffic, both directions combined, over the most
recent 12 months:

$$\omega^{\text{route}}_{r} \;=\; \frac{Q_r}{\sum_{r' \in \text{basket}} Q_{r'}}$$

where $Q_r$ is total passengers carried. Weights are stamped with their reference period
in `route_weight` and never overwritten, so any historical index value stays reproducible.

#### 3.4.1 The basket, and why it is eight sectors

| Route | Passengers (12m to 2026-05) | Weight |
|---|---:|---:|
| BLR-DEL | 4,879,073 | 0.2072001 |
| BOM-DEL | 3,751,418 | 0.1593118 |
| DEL-HYD | 3,095,780 | 0.1314687 |
| CCU-DEL | 2,866,361 | 0.1217260 |
| BLR-BOM | 2,310,386 | 0.0981153 |
| BLR-HYD | 2,251,473 | 0.0956135 |
| DEL-MAA | 2,221,545 | 0.0943425 |
| BLR-CCU | 2,171,615 | 0.0922221 |
| **Total** | **23,547,651** | **1.0000000** |

Six of these came with the problem statement. **DEL-HYD and BLR-CCU were added
in the basket review of 8 September 2026**, and the reason is that DEL-HYD
carried more traffic than three routes already in the basket. It was absent
because nobody had revisited the starting list, which is not a methodological
reason. Together the two lift coverage of traffic among the six metros from
56.3% to 72.5%.

The weights are regenerated by `make weights` from DGCA city-pair traffic, never
hand-edited. Shares are rounded to seven decimal places and the rounding residual
(~$10^{-7}$) is assigned to the largest route by the largest-remainder method, so
the published block sums to **exactly** 1 rather than to 0.9999999. A test asserts
it, because an index whose weights do not partition unity is quietly renormalising
and the reader cannot see it.

The largest sectors still omitted are BLR-MAA (1,508,372), HYD-MAA (1,459,042)
and BOM-CCU (1,309,042). Adding them is arithmetic, not a design change: all
three are metro trunk routes like the eight here. What *would* change the
character of the index is a thin regional sector, where one carrier sets the
price and sold-out days are common — a harder measurement problem, named in §7.

### 3.5 Window weights — a stated assumption, not a finding

$\omega^{\text{win}}_{w}$ should be the share of bookings made at each advance-purchase
distance. **No public booking-lead-time distribution for Indian domestic aviation exists.**
The default is therefore equal weights, $\omega^{\text{win}}_{w} = 1/5$, recorded in the
database with `weight_basis = 'equal-weight (assumption)'`.

This is the weakest assumption in the index and it is surfaced rather than buried. The
back-test reports a sensitivity analysis across three alternative window-weight vectors so
the reader can see how much the headline moves. If MoSPI or an airline supplies a real
booking curve, one config change replaces it.

### 3.6 Temporal aggregation

Weekly and monthly APIx are the **arithmetic mean of daily index values**, not an index of
mean prices:

$$\text{APIx}_{[t_1,t_2]} \;=\; \frac{1}{|[t_1,t_2]|}\sum_{t=t_1}^{t_2} \text{APIx}_t$$

Because the weights $\omega_{r,w}$ are fixed within a period, this is *identically equal*
to aggregating the period-mean elementary indices:

$$\frac{1}{T}\sum_t \sum_{r,w}\omega_{r,w} I^t_{r,w} \;=\; \sum_{r,w}\omega_{r,w}\left(\frac{1}{T}\sum_t I^t_{r,w}\right)$$

so daily, weekly and monthly figures are mutually consistent by construction. The worked
example in §5 verifies this numerically, and `tests/test_index_math.py` asserts it.

Averaging *prices* first would break that identity, because the geometric mean inside
Jevons does not commute with an arithmetic mean over days.

---

## 4. Cleaning

Raw quotes become index-ready fares in five stages. Every stage is a pure
function, and a `CleaningReport` records what each one discarded. That report is
published alongside the index: a statistical office needs the discard rate before
it will trust the series.

**Nothing is ever deleted.** Rejected and flagged rows are written to the database
with their flags and excluded at query time, so every exclusion is countable and
reversible.

### 4.1 The trap this avoids

The obvious reading of "detect outliers in fare data" is to find unusually high
fares and drop them. Here that would destroy the measurement.

APIx exists because airfares swing 200 to 400 percent within a booking window.
Those swings are the signal. A detector tuned to remove statistically extreme
fares would delete exactly what the index is built to capture, and it would do so
hardest during festival peaks and fuel shocks, which is when the number matters
most. The index would look reassuringly smooth and be wrong.

So the rule is narrow:

> We detect **data errors**. We do not detect **expensive flights**.

Two consequences follow. **No comparison ever crosses a scrape date**, so a
genuine surge cannot be flagged. **Thresholds are deliberately loose**, because
we are hunting parse failures that misplace a decimal, which show up as
order-of-magnitude deviations rather than as merely dear tickets.

### 4.2 Stages

| Stage | What it does |
|---|---|
| Plausibility | Rejects what cannot be a domestic economy fare: below 500 rupees, above 200,000, non-positive, or an out-of-range window |
| Deduplicate | Collapses byte-identical repeats. Two fare brands on one flight are two observations, not a duplicate, and the same flight seen through two sources is corroboration |
| Decompose | Verifies base, taxes, UDF and convenience reconcile to the total within one rupee |
| Outliers | Flags data errors within same-day comparison groups |
| Select | Takes the lowest available total fare per route, window and carrier |

The plausibility bounds are judgment calls and are stated as such. The lower bound
exists because statutory charges alone typically exceed 400 rupees, so anything
below 500 means a component was captured instead of the total.

### 4.3 Why modified z-score on log fares

The elementary aggregate uses the geometric mean of price relatives, so the
cleaner works on the same log scale. Consistency between the two is deliberate.

$$M_i = \frac{0.6745\,(\ln P_i - \operatorname{median}(\ln P))}{\operatorname{MAD}(\ln P)}, \qquad \text{flag if } |M_i| > 3.5$$

**Plain z-score is unusable.** The mean and standard deviation have a breakdown
point of zero, so a single large parse error drags the mean toward itself and
inflates the standard deviation enough to mask its own detection. On a sample of
six quotes, one contaminant at 900,000 rupees produces a z-score below 2.5 and
escapes. The same value scores above 10 under the median and MAD, whose breakdown
point is one half. This is asserted in `tests/test_cleaning.py`.

**Tukey fences are robust but wrongly shaped.** They are symmetric on the raw
scale while fare distributions are strongly right-skewed. On realistic skewed
data the lower fence falls below zero, so it can never flag a suspiciously cheap
quote and a one-rupee fare would pass. IQR remains available as a configurable
alternative for comparison.

The 0.6745 constant makes the score comparable to a standard z-score under
normality. The 3.5 threshold is the Iglewicz and Hoaglin recommendation.

### 4.4 Small samples

Below five quotes in a comparison group, nothing is flagged. Robust dispersion
cannot be estimated from three points, and flagging there would systematically
discard the cheapest or dearest carrier on exactly the thin routes where coverage
is already weakest. The report counts how many groups went unassessed.

Sold-out quotes are set aside before assessment so they cannot shift a group's
median.

---

## 5. Missing data

A cell can be empty because a route was sold out, a scrape failed, or a carrier dropped
the route. These are different things and are handled differently.

| Situation | Treatment |
|---|---|
| Carrier missing from a cell, others present | Jevons runs over observed carriers only. The geometric mean is self-renormalising, so no imputation is needed. `carriers_used` records who was in it. |
| Entire cell missing, < 3 consecutive days | Carry forward the last elementary index, flagged `is_imputed`. Matches CPI practice for temporarily unavailable items. |
| Entire cell missing, ≥ 3 consecutive days | Drop the cell from that day's aggregate and renormalise by observed weight. `coverage_ratio` falls below 1 and is published. |
| Sold out / no seats | **Not** a price of zero or infinity. Excluded from the index, recorded with `is_sold_out`, and reported as a scarcity indicator alongside the index. |

The renormalisation in row three is why §3.3's denominator is written out. Silently
renormalising while presenting an unchanged headline would misstate coverage.

### 5.1 A missed collection day cannot be backfilled

This is the most important operational fact in the project and it constrains the
design of the scheduler.

**A fare is a price quoted at a moment, for a departure a fixed number of days
away.** If the cycle for 5 September never ran, the T+7 fare for a 12 September
departure *as it stood on 5 September* no longer exists. Nobody holds it. Asking
the airline on 8 September returns the price of that same seat with three days to
go — a T+3 observation, which belongs in a different cell of the matrix and
carries a different lead-time premium.

A collector that silently re-ran the matrix and filed today's answers under
Friday's date would produce a series that looks complete and is wrong, and wrong
*in a direction*: fares rise as departure nears, so every backfilled day would be
biased upward. `apix/orchestration/backfill.py` makes that impossible by deciding
on the date:

| Case | Action |
|---|---|
| The date is **today** | Genuine recovery. The cycle failed an hour ago; the observations are still the ones the schedule intended. Re-run only the missing cells. |
| The date is in the **past** | Nothing can be recovered. Write the missing cells to `collection_cell` with `outcome='missed'`, and let §5's imputation rules handle them. |
| The date is in the **future** | Refused. There is nothing to collect yet. |

The gap is a database row rather than an absence, because a gap that exists only
as an absence cannot be audited and is indistinguishable from a cell that was
collected and then cleaned away. Once recorded it flows through the existing
machinery: the elementary index carries forward and is flagged `is_imputed`, the
coverage ratio published beside every index value falls, and the shortfall is
visible in the output.

This is what a statistical agency does with non-response. It is recorded and
imputed under a stated rule; it is never invented.

### 5.2 Collection schedule

One cycle per day at **02:00 IST**, fixed wall-clock time rather than a 24-hour
interval. An interval drifts by however long each cycle takes, and after a month
the index would be observed at a different hour than it was at the start. Airfares
move intraday, so that drift would be a measurement artefact rather than a
scheduling detail. 02:00 also puts our traffic at an airline's lowest-load hour.

The index is built at 04:30, separately, so a short collection produces a short
day rather than a failed index build. `make cron` prints the crontab;
`python -m apix.cli schedule` is the equivalent long-lived process.

---

## 6. Worked example

Two routes, two windows, two carriers, two days. Every figure below is reproduced by
`tests/test_index_math.py::test_worked_example_matches_methodology_doc`, which fails if
this document and the engine ever disagree.

### 6.1 Weights

Real DGCA passenger traffic, 2025-06 to 2026-05, both directions:

| Route | Passengers | $\omega^{\text{route}}$ |
|---|---:|---:|
| DEL-BLR | 4,879,073 | 0.565330 |
| DEL-BOM | 3,751,418 | 0.434670 |
| **Total** | **8,630,491** | **1.000000** |

Windows T+7 and T+30 at equal weight, $\omega^{\text{win}} = 0.5$ each. Cell weights
$\omega_{r,w}$ are the products:

| Cell | $\omega_{r,w}$ |
|---|---:|
| DEL-BLR, T+7 | 0.282665 |
| DEL-BLR, T+30 | 0.282665 |
| DEL-BOM, T+7 | 0.217335 |
| DEL-BOM, T+30 | 0.217335 |
| **Sum** | **1.000000** |

### 6.2 Prices (INR, total fare)

| Route | Window | Carrier | Base $\bar P^0$ | Day 1 | Day 2 |
|---|---|---|---:|---:|---:|
| DEL-BLR | T+7 | QP | 5,200 | 5,980 | 6,240 |
| DEL-BLR | T+7 | SG | 4,900 | 5,390 | 5,880 |
| DEL-BLR | T+30 | QP | 4,100 | 4,305 | 4,182 |
| DEL-BLR | T+30 | SG | 3,800 | 3,914 | 3,990 |
| DEL-BOM | T+7 | QP | 4,800 | 5,760 | 6,000 |
| DEL-BOM | T+7 | SG | 4,500 | 5,040 | 5,310 |
| DEL-BOM | T+30 | QP | 3,700 | 3,811 | 3,774 |
| DEL-BOM | T+30 | SG | 3,500 | 3,605 | 3,640 |

### 6.3 Elementary indices, day 1

DEL-BLR, T+7. Price relatives $5980/5200 = 1.15$ and $5390/4900 = 1.10$:

$$I^{1}_{\text{DEL-BLR},7} = 100\times(1.15 \times 1.10)^{1/2} = 100\times\sqrt{1.2650} = 100 \times 1.124722 = \mathbf{112.4722}$$

DEL-BOM, T+7. Relatives $5760/4800 = 1.20$ and $5040/4500 = 1.12$:

$$I^{1}_{\text{DEL-BOM},7} = 100\times(1.20\times1.12)^{1/2} = 100\times\sqrt{1.3440} = \mathbf{115.9310}$$

All four cells:

| Cell | Relatives | $I^1$ | $I^2$ |
|---|---|---:|---:|
| DEL-BLR, T+7 | 1.15, 1.10 | 112.4722 | 120.0000 |
| DEL-BLR, T+30 | 1.05, 1.03 | 103.9952 | 103.4891 |
| DEL-BOM, T+7 | 1.20, 1.12 | 115.9310 | 121.4496 |
| DEL-BOM, T+30 | 1.03, 1.03 | 103.0000 | 102.9951 |

### 6.4 Aggregation

Day 1:

$$\text{APIx}_1 = 0.282665(112.4722) + 0.282665(103.9952) + 0.217335(115.9310) + 0.217335(103.0000)$$
$$= 31.7924 + 29.3966 + 25.1957 + 22.3855 = \mathbf{108.7691}$$

Day 2:

$$\text{APIx}_2 = 0.282665(120.0000) + 0.282665(103.4891) + 0.217335(121.4496) + 0.217335(102.9951)$$
$$= 33.9198 + 29.2536 + 26.3953 + 22.3844 = \mathbf{111.9522}$$

Day-over-day change: $111.9522/108.7691 - 1 = +2.93\%$.

### 6.5 The consistency check

Two-day mean of daily APIx: $(108.7691 + 111.9522)/2 = \mathbf{110.3607}$.

Aggregating the two-day mean elementary indices instead:

$$0.282665\left(\tfrac{112.4722+120.0000}{2}\right) + 0.282665\left(\tfrac{103.9952+103.4891}{2}\right) + 0.217335\left(\tfrac{115.9310+121.4496}{2}\right) + 0.217335\left(\tfrac{103.0000+102.9951}{2}\right) = \mathbf{110.3607}$$

Identical, as section 3.6 requires.

---

## 6A. The CPI overlay

The overlay makes the case for high-frequency collection in two parts, and only
the second needs the official series.

**Part one needs no CPI data.** For each month, contrast the spread of the daily
index against the single value a once-a-month collector would have recorded. On
the demo dataset, August shows the point plainly:

| Month (synthetic demo data) | Days | Low | High | Range | Mid-month reading | Missed vs peak |
|---|---:|---:|---:|---:|---:|---:|
| Aug 2026 | 31 | 96.29 | 143.22 | 46.92 | 101.78 | 41.44 (41%) |

A collector sampling mid-month records 101.78. The index peaked at 143.22 in the
same month and returned near its starting level before the next visit. The
monthly figure is not wrong; it is a point estimate of something that moved 41%
above it and came back unobserved. That is the gap APIx closes, and it is
measurable without reference to any external series.

**Part two overlays the official CPI Division 07 Transport series**, both rebased
to 100 at their first common month. Rebasing rather than dual axes is deliberate:
APIx is 100 over its own price reference period and the CPI 2024 series is 100 in
calendar 2024, and a second independent axis can be scaled to manufacture any
apparent agreement between two lines.

**The official series ships absent.** MoSPI publishes CPI through eSankhyiki,
whose data endpoints return the portal application rather than data when called
from outside India, and `cpi.mospi.gov.in` refuses connections. The series could
not be retrieved programmatically, so the panel renders an empty state naming the
export to download and the command to load it, rather than an approximation.
`scripts/load_cpi.py` accepts the CSV; `tests/test_cpi_loader.py` asserts that no
CPI value reaches the demo database unless a human put it there.

---

## 6B. Validation and back-test

`make backtest` writes a report, a machine-readable result and a chart to
`data/backtest/`.

### The external comparison does not run, and why

The problem statement asks for results validated against publicly available DGCA
monthly average-fare data. **No such series was found.** DGCA's Tariff Monitoring
Unit checks fares on 78 routes monthly by looking at airline websites, but that
is a regulatory compliance function. What DGCA publishes monthly is traffic and
load factor. Fare figures appear in parliamentary answers as occasional point
estimates for named sectors, not as a series that can be joined against a daily
index.

Reporting a correlation against numbers nobody can check would be worse than
reporting none, so the comparison ships unrun. `scripts/load_dgca_fares.py`
accepts any reference that does become available and the back-test then scores it
automatically. `fare_basis` is a required field because "average fare" means
different things in a tariff band, a realised-revenue series and a quoted fare,
and a comparison that does not say which is uninterpretable.

### What is validated instead

**Identity checks**, all of which the construction guarantees and any of which
failing would mean the engine contradicts this document.

| Check | Result on the demo dataset |
|---|---|
| Index equals 100 over the price reference period | 100.1639 |
| Monthly equals the mean of its daily values | largest gap 0.000045 points |
| Route contributions sum to the headline | gap 0.000043 points |
| Every published value carries its coverage | 66 of 66 |

**Robustness against the flagged assumption.** Section 3.5 states that equal
weighting of advance-purchase windows is an assumption, and the largest one in
the index. Rebuilding the whole series under alternative weight vectors moves the
headline by at most **2.63 index points**, with a mean gap near 1. The assumption
is therefore material but not dominant, and that number belongs next to the
caveat rather than leaving a reader to guess.

**Benchmark against a naive mean fare**, to test whether the CPI-aligned
machinery earns its complexity. On the seeded dataset the two are nearly
indistinguishable, correlating 0.95 on changes. That is not evidence the
methodology is unnecessary: the generator applies one surge multiplier across
every route and carrier, and when everything moves together no aggregation
choice has anything to distinguish. The comparison only becomes informative on
real data where routes move independently. The report says so where the number
appears.

### Scoring choices

Where an external series does exist, movement is scored rather than level:
Pearson correlation on month-over-month changes, Spearman rank correlation, and
direction agreement. Absolute error is reported only after rebasing and is
labelled a shape comparison. APIx tracks the lowest quoted fare and any revenue
benchmark tracks realised revenue; scoring absolute error between them would fail
a correct index for measuring what it was built to measure. Correlation is
suppressed entirely below six paired periods.

---

## 6C. Lead-time elasticity

The analytical artifact the problem statement asks for, as distinct from the
index. A monthly collection samples one booking horizon, so it cannot see the
shape of this curve at all, let alone that the shape differs by route.

### The model

$$\text{fare}(w) = L\,\bigl(1 + a\,e^{-w/\tau}\bigr)$$

$L$ is the far-advance floor, $a$ the last-minute premium as a multiple of that
floor, and $\tau$ the number of days over which the premium decays by a factor
of $e$. Fitted per route by nonlinear least squares on **individual fares**, not
on the five window means: fitting the means would discard the dispersion the
standard errors are meant to reflect and would leave a three-parameter curve with
two residual degrees of freedom.

### Why not a single elasticity

An earlier specification called for an OLS fit of $\log(\text{fare})$ on the
booking window, and for that fit to recover $a$ and $\tau$. Those requirements
are incompatible: $a$ and $\tau$ are not parameters of a log-linear model, and
fares approach an asymptote rather than declining at a constant proportional
rate.

Both are therefore reported, with the log-linear slope labelled as what it is.
It averages a steep final fortnight together with a nearly flat far-advance
period, so quoting it alone would imply a constant percentage effect and hide
the structure the curve exists to show.

### Recovery against known truth

`apix/seed.py` generates fares from a known $a$ and $\tau$ per route, so the
fitter is checked against ground truth rather than against its own output.
Fitted on the demo database:

| Route | $a$ fitted | $a$ true | $\tau$ fitted | $\tau$ true | $R^2$ |
|---|---:|---:|---:|---:|---:|
| BOM-DEL | 1.417 | 1.42 | 10.05 | 10.0 | 0.802 |
| BLR-DEL | 1.293 | 1.30 | 11.07 | 11.0 | 0.780 |
| DEL-HYD | 1.214 | 1.22 | 11.66 | 11.5 | 0.758 |
| DEL-MAA | 1.180 | 1.18 | 12.21 | 12.0 | 0.752 |
| CCU-DEL | 1.053 | 1.05 | 13.51 | 13.5 | 0.705 |
| BLR-CCU | 1.003 | 1.00 | 13.16 | 13.0 | 0.695 |
| BLR-BOM | 0.949 | 0.95 | 14.10 | 14.0 | 0.672 |
| BLR-HYD | 0.711 | 0.72 | 16.06 | 16.0 | 0.560 |

The true value falls inside the 95% interval in every case. `tests/test_elasticity.py`
asserts this, so a change to either the generator or the fitter that breaks the
correspondence fails the suite.

### Limitations

1. **Recovery on synthetic data is not validation on real data.** These figures
   confirm the estimator is unbiased for the process that generated them. Real
   fares are not drawn from that process, and the residual variance here is
   entirely the generator's own within-day dispersion.
2. **$R^2$ falls on flatter routes** because the same time-of-day noise is a
   larger share of a smaller signal. It is not evidence that the model fits
   those routes worse in any meaningful sense.
3. **The fit pools all carriers and departure times.** A per-carrier fit would
   need more observations per cell than the current basket produces.
4. **No causal claim.** This describes how quoted fares vary with the booking
   horizon. It does not establish why, and it says nothing about what a traveller
   would pay under a different booking policy.

---

## 6D. Anomaly detection

Flags days whose movement is large **relative to the series' own recent
volatility**. That is a statistical statement about the index, not a claim about
the world, and every part of the design exists to keep the two apart.

### The statistic

For each day $t$, on the day-over-day proportional change
$c_t = (P_t - P_{t-1})/P_{t-1}$, using only the preceding $W = 14$ days:

$$z_t = \frac{0.6745\,(c_t - \operatorname{med}(H_t))}{\operatorname{MAD}(H_t)},
\qquad
H_t = \{\,c_j : t-W \le j < t,\ j \notin F\,\}$$

where $F$ is the set of days already flagged. A day is flagged when
$|z_t| > 3.5$.

Three choices in that formula are load-bearing.

**Median and MAD, not mean and standard deviation.** The same reasoning as the
cleaner in §4.3: a mean and standard deviation have a breakdown point of zero, so
the surge being looked for inflates the very yardstick meant to detect it and can
hide inside its own contribution. The 0.6745 factor and the 3.5 threshold are
Iglewicz and Hoaglin's, and are the same constants the cleaner uses, so the two
components cannot disagree about what "extreme" means.

**Changes, not levels.** A level-based detector flags every day of a sustained
plateau. What matters is when the series *moves*; a surge that arrives and
persists should be flagged on arrival and on its return, not once per day in
between.

**Flagged days are excluded from the baseline** — the $j \notin F$ condition.
Without it an event poisons the yardstick used to judge what follows it: a
genuine surge enters the trailing window, shifts the median and MAD, and then
ordinary days score as extreme against the distorted baseline. On a four-day
surge that produced a tail of six false positives running two weeks past the
event. `tests/test_anomaly.py::test_an_event_does_not_poison_the_baseline_that_follows_it`
is the regression.

### Episodes

Flags within two days of each other are one episode. Grouping **ignores
direction**, deliberately: because detection runs on day-over-day changes, a
price excursion always produces at least two flags — the move away from the
baseline and the move back — and those are one event. An earlier version split on
direction and therefore reported every one-day spike as two episodes, which is
precisely the event-count inflation the grouping exists to prevent. An episode's
direction is that of its largest single move.

A four-day plateau flags its onset and its return and nothing between, because
the middle of a plateau barely moves. Those two flags are genuinely days apart,
so proximity grouping cannot join them across the elevated middle. The guarantee
the detector can actually deliver is therefore **two flags for a four-day event,
not four or more** — not "always one episode". The test states it that way.

### Hypotheses are never causes

`label_hypotheses` attaches candidate explanations to a flag raised on purely
statistical grounds, phrased as things to check:

> flagged, and Diwali falls in this window

never

> Diwali caused this.

The seasonal windows are deliberately coarse and carry "date varies by year;
verify", because most Indian festival dates are lunar. Where no candidate
matches, the label is `no candidate identified; cause unknown` — the system says
it does not know rather than reaching for the nearest holiday. This module holds
no evidence about causation and asserts none.

### On the demo database

Two flags, one episode: 21 August 2026, +18.2%, $z = 6.57$, against the eight-day
surge the generator injects from 20 August. A detector that cannot find a surge
deliberately put there will not find a real one, so
`tests/test_anomaly.py` asserts it does.

The detector lived in the dashboard's JavaScript until it was moved here. That
put it beyond the reach of the test suite and would have required a second
implementation for the API, giving two copies of the methodology to drift apart.
The browser now renders flags this module produced.

---

## 6E. Short-horizon nowcasting — a negative result

A daily index is published with a lag: collection runs at 02:00 and aggregation
after it, so a user asking for today's number before that finishes gets
yesterday's. A nowcast would fill the gap. This section reports the test of
whether one is worth publishing, and the answer is **no**.

### What was tested

Three baselines and two models, all scored the same way.

| | Method |
|---|---|
| baseline | `naive` — tomorrow equals today (random walk) |
| baseline | `seasonal_naive` — tomorrow equals the same weekday last week |
| baseline | `drift` — random walk with the average historical slope |
| model | `damped_trend` — Holt's linear method with damping, three parameters grid-fitted on one-step squared error |
| model | `seasonal_damped_trend` — the same, applied to a series with additive weekly factors removed and added back |

The baselines are not straw men. A price series is close to a random walk, so
`naive` is genuinely hard to beat at short horizons, and airfares have a real
weekly cycle, so `seasonal_naive` is the right bar at $h = 7$.

### How it was scored

**Rolling origin, not a single split.** One train/test split on a 66-point series
measures luck. Every origin from day 21 onward produces a forecast and is scored,
so each number is a mean over dozens of forecasts. Each origin fits on data
strictly before it, so no model ever sees a value it is asked to predict;
`tests/test_nowcast.py::test_rolling_origin_never_shows_a_model_its_target`
asserts it, because a leak here would be invisible in the output and would turn
the whole exercise into a tautology.

Reported in MASE — mean absolute error scaled by the in-sample one-step
seasonal-naive MAE — which is scale-free and therefore comparable across
horizons and across a rebased series.

### Results

| Model | $h$ | $n$ | MAE | RMSE | MASE | vs best baseline |
|---|---:|---:|---:|---:|---:|---|
| seasonal_damped_trend | 1 | 45 | 3.149 | 7.108 | 0.451 | beats `naive` by 11% |
| **naive** | 1 | 45 | 3.531 | 5.449 | 0.506 | best baseline |
| drift | 1 | 45 | 3.608 | 5.505 | 0.517 | baseline |
| damped_trend | 1 | 45 | 3.746 | 5.393 | 0.537 | loses to `naive` by 6% |
| seasonal_naive | 1 | 45 | 9.012 | 17.564 | 1.291 | baseline |
| **naive** | 7 | 39 | 10.321 | 18.866 | 1.479 | best baseline |
| seasonal_naive | 7 | 39 | 10.321 | 18.866 | 1.479 | baseline |
| drift | 7 | 39 | 11.171 | 20.149 | 1.601 | baseline |
| damped_trend | 7 | 39 | 13.630 | 23.933 | 1.953 | loses to `naive` by 32% |
| seasonal_damped_trend | 7 | 39 | 14.421 | 29.746 | 2.066 | loses to `naive` by 40% |

`seasonal_naive` and `naive` score identically at $h = 7$. That is arithmetic,
not a bug: with a seasonal period $m = 7$ and $h = 7$, the seasonal-naive
forecast is $y_{T+7-7} = y_T$, which is the naive forecast.

### The verdict, and why it is negative

**No model is recommended. Publish the naive baseline.**

At $h = 7$ nothing comes close: the best model loses to `naive` by 32%. The
damping and the seasonal factors are fitting noise at that horizon.

At $h = 1$ the interesting case. `seasonal_damped_trend` beats `naive` by 11% on
MAE — and **loses to it on RMSE**, 7.11 against 5.45. The two metrics disagree,
and when they do that is not a detail: a model that wins on mean absolute error
while losing on root mean squared error is usually closer and occasionally much
further out. It has a heavier error tail, and on inspection the tail falls at
turning points — which is exactly when anyone reads a nowcast. Publishing the MAE
win alone would be selecting the metric that flatters. On a 45-forecast sample
that is not a result, and `_recommend` is written to say so rather than to find a
winner.

### Why this is reported rather than dropped

Three reasons. A rejected model is a result: it tells a reader that at this
sample size the honest thing to publish is "tomorrow will be like today", and it
shows the test that established it. It documents the bar any future model has to
clear, on data already collected. And a project that only ever reports the
components that worked is not a project a statistical office should trust with a
price index.

`GET /v1/nowcast` returns the scoreboard and the verdict together, and the
dashboard shows both. `make nowcast` prints them.

### Caveats that matter more than the numbers

1. **This is seeded data.** The evaluation measures whether these methods can
   track a series with the generator's dynamics. Real airfares are not drawn from
   that process, and a real series would have to be re-scored before any of this
   transfers.
2. **66 observations is a short series** for a forecasting comparison. A 5%
   margin was treated as no improvement for exactly this reason.
3. **The verdict is about the headline index only.** Route-level or window-level
   series were not tested and could behave differently.

---

## 7. Known limitations

Stated plainly, because a statistical office would find them anyway.

1. **Carrier-restricted sample.** IndiGo and Air India are excluded on the compliance
   grounds in `docs/SOURCE_AUDIT.md`. Together they carry most Indian domestic traffic.
   APIx therefore measures fare movement among observable carriers, and every published
   value carries `observed_pax_share` so the reader knows what fraction of the route's
   traffic the index actually watches. Fixing this needs a licensed GDS feed or a DGCA
   data-sharing agreement, not a better scraper.

2. **Window weights are assumed, not measured** (§3.5). The largest single source of
   methodological uncertainty. Sensitivity analysis is reported, not hidden.

3. **Lowest-available fare is not average revenue.** APIx tracks the price a traveller is
   quoted. DGCA's average-fare figures reflect realised revenue across all booking
   classes. The two should correlate but must not be expected to match in level, which
   is why the back-test scores **correlation and direction agreement**, not absolute error
   against DGCA levels.

4. **Non-stop economy only.** Connecting itineraries and premium cabins are excluded.
   This keeps the product definition constant, which is what a price index requires, but
   it means APIx does not describe the whole market.

5. **Thirty days is a short back-test.** It can demonstrate correlation and
   responsiveness. It cannot establish stable seasonal behaviour, and no claim to that
   effect is made.

6. **No causal claim.** APIx is a price index. Anomaly flags mark statistical outliers
   against a rolling baseline; attributing one to a festival or a fuel-price move is a
   labelled hypothesis in the UI, never an inference the system asserts.
