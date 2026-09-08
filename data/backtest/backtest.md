# APIx back-test and validation report

Generated 2026-09-05T22:52:26.176754+00:00.

**Span:** 2026-07-01 to 2026-09-04, 66 daily observations. The problem statement asks for at least 30 days: **met**.

**Price reference period:** 2026-07-01 to 2026-07-28 = 100.

---

## 1. External validation against DGCA fares

**Not run.** No DGCA reference fares loaded. No public route-wise monthly average-fare series was found: DGCA's Tariff Monitoring Unit checks 78 routes monthly as a regulatory function, and DGCA's monthly publications carry traffic and load factor, not fares. Load any reference you do obtain with scripts/load_dgca_fares.py and this comparison runs automatically.

This is a genuine gap in the deliverable and is stated rather than filled with an estimate. Scoring an index against fabricated ground truth would be worse than not scoring it.

---

## 2. Internal validation

4 of 4 checks passed.

- PASS — **base_period_is_100**: mean over the price reference period is 100.1639 (tolerance ±2.0)
- PASS — **monthly_equals_mean_of_daily**: largest gap 0.000045 index points in 2026-08-01 (tolerance 0.0001)
- PASS — **contributions_sum_to_headline**: contributions sum to 103.0839 against headline 103.0839, gap 0.000043
- PASS — **every_value_reports_coverage**: 66 published values, 0 without a valid coverage ratio

---

## 3. Robustness: advance-window weights

The equal-weighting of advance-purchase windows is the largest stated assumption in the methodology, because no public booking-lead-time distribution for India exists. This is what it is worth.

| Scenario | Mean | Min | Max | Max gap vs equal | Mean gap |
|---|---:|---:|---:|---:|---:|
| equal | 103.275 | 95.8259 | 143.2178 | 0.0 | 0.0 |
| near_heavy | 103.2065 | 95.4504 | 145.0365 | 2.6263 | 0.9184 |
| advance_heavy | 103.3455 | 95.7172 | 141.1116 | 2.4113 | 0.9411 |

Worst case: the headline moves at most **2.6263 index points** under `near_heavy`, with a mean gap of 0.9184.

---

## 4. Benchmark against a naive mean fare

Paired observations: 66.

- Correlation of changes with the naive series: **0.9483**
- Maximum divergence after rebasing: **3.272 index points**
- Mean divergence: 1.261 index points

*The naive series is an unweighted mean of observed fares, rebased. Divergence is what the weighting and the Jevons aggregation buy; if it were near zero the methodology would not be earning its complexity.*

**Read this result carefully.** The two series are nearly indistinguishable here, which on its face suggests the CPI-aligned machinery is not earning its complexity. That conclusion does not follow from this run, because the underlying data is synthetic and the generator applies one surge multiplier across every route and carrier at once. When everything moves together, weighting and the choice of elementary formula have almost nothing to distinguish, so any reasonable aggregation returns the same answer.

The comparison becomes informative only on real data, where routes move independently. Until then this test establishes that the engine is not producing something wild, and nothing stronger. Treating it as evidence either for or against the methodology would be overreading it.

---

## 5. Coverage

- Basket coverage on the latest day: 100.0%
- Share of domestic passenger traffic observable: 8.3%

The second figure is the binding constraint on this project. It is not improved by better engineering; it needs a licensed aggregator feed or a data-sharing arrangement with DGCA.

