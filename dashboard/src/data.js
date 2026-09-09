/* Data access and derived series.
 *
 * Everything here reads the static JSON exported by scripts/export_static.py.
 * No index arithmetic happens in the browser: the methodology lives in Python,
 * is covered by tests, and would drift if it existed twice. What this file does
 * is slice, join and format what the engine already computed.
 */

const BASE = `${import.meta.env.BASE_URL || "/"}data`;

export async function loadAll() {
  const names = [
    "series", "routes", "leadtime", "leadtime_fares",
    "compliance", "sources", "basket", "windows", "provenance",
    "cpi", "cpi_dispersion", "leadtime_fit", "anomalies", "nowcast",
    "fares_daily",
  ];
  const parts = await Promise.all(
    names.map(async (n) => {
      const res = await fetch(`${BASE}/${n}.json`);
      if (!res.ok) throw new Error(`could not load ${n}.json (${res.status})`);
      return [n, await res.json()];
    })
  );
  return Object.fromEntries(parts);
}

/* ------------------------------------------------------------- helpers */

export const fmt = (n, d = 2) =>
  n === null || n === undefined || Number.isNaN(n)
    ? "–"
    : Number(n).toLocaleString("en-IN", {
        minimumFractionDigits: d,
        maximumFractionDigits: d,
      });

export const pct = (n, d = 1) =>
  n === null || n === undefined ? "–" : `${(Number(n) * 100).toFixed(d)}%`;

export const rupees = (n) =>
  n === null || n === undefined
    ? "–"
    : `₹${Number(n).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

export const shortDate = (iso) =>
  new Date(iso + "T00:00:00Z").toLocaleDateString("en-IN", {
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });

export const longDate = (iso) =>
  new Date(iso + "T00:00:00Z").toLocaleDateString("en-IN", {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });

/* --------------------------------------------------------------- series */

export const seriesFor = (series, frequency) =>
  series
    .filter((r) => r.frequency === frequency)
    .sort((a, b) => a.index_date.localeCompare(b.index_date));

/** Change between the last two points, as a fraction. */
export function lastChange(rows) {
  if (rows.length < 2) return null;
  const a = rows[rows.length - 2].index_value;
  const b = rows[rows.length - 1].index_value;
  return (b - a) / a;
}

/** Change from the first point of the series. */
export function periodChange(rows) {
  if (rows.length < 2) return null;
  const a = rows[0].index_value;
  const b = rows[rows.length - 1].index_value;
  return (b - a) / a;
}

/* ---------------------------------------------------------------- routes */

export function routeMatrix(routes, frequency, date) {
  return routes
    .filter((r) => r.frequency === frequency && r.index_date === date)
    .sort((a, b) => b.weight - a.weight);
}

export const routeDates = (routes, frequency) =>
  [...new Set(routes.filter((r) => r.frequency === frequency).map((r) => r.index_date))].sort();

/* -------------------------------------------------------------- lead time
 *
 * The elasticity artifact: mean observed fare against advance-purchase window,
 * per route. Reads observed fare LEVELS rather than index values, because the
 * question "what does booking later cost me" is about rupees, not about an
 * index normalised to its own base.
 */
export function leadtimeCurves(leadtimeFares) {
  const byRoute = {};
  for (const row of leadtimeFares) {
    (byRoute[row.route_code] ||= []).push(row);
  }
  for (const code of Object.keys(byRoute)) {
    byRoute[code].sort((a, b) => a.window_days - b.window_days);
  }
  return byRoute;
}

/** Merge per-route curves into rows keyed by window, for a multi-line chart. */
export function leadtimeChartData(leadtimeFares) {
  const windows = [...new Set(leadtimeFares.map((r) => r.window_days))].sort((a, b) => a - b);
  return windows.map((w) => {
    const row = { window: w, label: `T+${w}` };
    for (const r of leadtimeFares.filter((x) => x.window_days === w)) {
      row[r.route_code] = Number(r.mean_fare);
    }
    return row;
  });
}

/**
 * Premium paid for booking at T+1 rather than T+45, per route.
 *
 * Reported as a plain ratio rather than a fitted elasticity. A two-point ratio
 * is what the data supports directly; calling it an elasticity would imply a
 * model that has not been estimated here.
 */
export function leadtimePremium(leadtimeFares) {
  const curves = leadtimeCurves(leadtimeFares);
  return Object.entries(curves)
    .map(([code, rows]) => {
      const near = rows[0];
      const far = rows[rows.length - 1];
      if (!near || !far || near === far) return null;
      return {
        route_code: code,
        near_window: near.window_days,
        far_window: far.window_days,
        near_fare: Number(near.mean_fare),
        far_fare: Number(far.mean_fare),
        ratio: Number(near.mean_fare) / Number(far.mean_fare),
      };
    })
    .filter(Boolean)
    .sort((a, b) => b.ratio - a.ratio);
}

/* -------------------------------------------------------------- anomalies
 *
 * Flags days whose move is large relative to the series' own recent volatility.
 * Uses a rolling median and MAD rather than a mean and standard deviation, for
 * the same reason the cleaner does: one big move should not inflate the yardstick
 * enough to hide itself.
 */
/* Anomaly detection now lives in apix/analytics/anomaly.py.
 *
 * It was implemented here in JavaScript, which put it beyond the reach of the
 * Python test suite and would have required a second implementation for the
 * API. The exported anomalies.json carries episodes the Python detector found;
 * this file only reads them.
 */
export function surgeEpisodes(anomalies) {
  return (anomalies || []).map((e) => ({
    ...e,
    peak_change_pct: Number(e.peak_change_pct),
    score: Number(e.score),
  }));
}

/* Dates flagged anywhere in any episode, for marking points on a chart. */
export function surgeDates(anomalies) {
  return new Set((anomalies || []).flatMap((e) => e.days || []));
}

/* ------------------------------------------------------------------ heat */

/**
 * Diverging colour around 100.
 *
 * Diverging rather than sequential because a price index has a meaningful
 * midpoint: 100 is the reference period, below it is cheaper than base and above
 * it dearer. A sequential ramp would hide that the scale has a natural zero.
 * Saturates at 40 index points either side, which covers the observed range.
 */
/* Diverging scale, because a price index has a meaningful midpoint at 100.
 * Neutral is the warm off-white of the design ground so an at-base cell reads
 * as "nothing to see"; green is cheaper than base, red-brown dearer. Both ends
 * are taken from the design tokens rather than picked ad hoc. */
const HEAT_NEUTRAL = [238, 241, 245];  // --viz-neutral
const HEAT_BELOW = [20, 135, 90];      // --viz-below, cheaper than base
const HEAT_ABOVE = [196, 50, 31];      // --viz-above, dearer than base

export function heatColour(value) {
  const t = Math.max(-1, Math.min(1, (value - 100) / 40));
  const target = t < 0 ? HEAT_BELOW : HEAT_ABOVE;
  const k = Math.abs(t);
  const mix = HEAT_NEUTRAL.map((c, i) => Math.round(c + (target[i] - c) * k));
  return `rgb(${mix.join(", ")})`;
}

export function heatTextColour(value) {
  return Math.abs(value - 100) > 14 ? "#fff" : "#0f1e35";
}

/* Categorical series colours, taken from the design tokens (--series-1..8).
 * Ordered so adjacent lines differ in lightness as well as hue: eight sectors
 * overlap heavily at the long-window end, and hue alone would not separate them
 * for a viewer who cannot distinguish red from green. */
export const ROUTE_SERIES_COLOURS = [
  "#0a72d8", // blue
  "#f26a2e", // orange
  "#14875a", // green
  "#7c3aed", // violet
  "#0891b2", // cyan
  "#c4321f", // red
  "#b45309", // amber
  "#64748b", // slate
];


/* ------------------------------------------------------- CPI overlay */

/* Rebase a series so it equals 100 at `anchor`.
 *
 * APIx and the CPI are on different bases: APIx is 100 over its own price
 * reference period, the CPI 2024 series is 100 in calendar 2024. Plotting them
 * on one axis without rebasing would compare two different questions, and
 * putting them on two independent axes is worse, because the scaling can be
 * chosen to manufacture any apparent agreement. Rebasing both to a common month
 * is the standard, checkable treatment, and the chart says so on its face.
 */
export function rebase(rows, valueKey, anchorValue) {
  if (!anchorValue) return [];
  return rows.map((r) => ({ ...r, rebased: (Number(r[valueKey]) / anchorValue) * 100 }));
}

/* Join the monthly APIx against the official CPI, both rebased to their first
 * common month. Returns [] when the CPI series has not been loaded. */
export function cpiOverlay(series, cpi, { sector = "combined" } = {}) {
  const cpiRows = (cpi || []).filter((c) => c.sector === sector);
  const monthly = seriesFor(series, "monthly");
  if (!cpiRows.length || !monthly.length) return [];

  const month = (iso) => iso.slice(0, 7);
  const cpiByMonth = new Map(cpiRows.map((c) => [month(c.ref_month), Number(c.index_value)]));
  const shared = monthly.filter((m) => cpiByMonth.has(month(m.index_date)));
  if (!shared.length) return [];

  const anchor = month(shared[0].index_date);
  const apixAnchor = Number(shared[0].index_value);
  const cpiAnchor = cpiByMonth.get(anchor);

  return shared.map((m) => {
    const k = month(m.index_date);
    return {
      month: k,
      apix: (Number(m.index_value) / apixAnchor) * 100,
      cpi: (cpiByMonth.get(k) / cpiAnchor) * 100,
    };
  });
}

/* The argument the overlay exists to make, and it needs no CPI data.
 *
 * For each month, contrast the spread of the daily index against the single
 * value a once-a-month collector would have recorded. `missed` is how far that
 * one observation sat from the month's peak: the movement monthly collection
 * cannot see, in index points. */
export function monthlyBlindSpot(dispersion) {
  return (dispersion || []).map((d) => {
    const sampled = Number(d.sampled_on_15th);
    const max = Number(d.max_index);
    const min = Number(d.min_index);
    return {
      month: String(d.ref_month).slice(0, 7),
      n_days: d.n_days,
      min,
      max,
      mean: Number(d.mean_index),
      sd: d.sd_index === null ? null : Number(d.sd_index),
      range: Number(d.range_index),
      sampled,
      missed: max - sampled,
      missedPct: sampled ? ((max - sampled) / sampled) * 100 : null,
    };
  });
}

/* The single most quotable number on the page: the worst month for a monthly
 * observer, and by how much it would have understated the peak. */
export function worstBlindSpot(dispersion) {
  const rows = monthlyBlindSpot(dispersion).filter((r) => r.n_days >= 20);
  if (!rows.length) return null;
  return rows.reduce((a, b) => (b.missed > a.missed ? b : a));
}

/* ---------------------------------------------- fitted lead-time curve */

/* Evaluate the curve the Python model fitted: fare(w) = L * (1 + a e^(-w/tau)).
 *
 * The PARAMETERS are computed in Python, tested, and shipped in
 * leadtime_fit.json. This only evaluates them for plotting. Refitting in the
 * browser would put the methodology in two places and guarantee the two drift.
 */
export function fittedCurve(fit, windows) {
  if (!fit || !fit.fitted) return [];
  const maxW = Math.max(...windows, 45);
  const out = [];
  for (let w = 1; w <= maxW; w += 0.5) {
    out.push({
      window_days: w,
      fitted: fit.floor * (1 + fit.amplitude * Math.exp(-w / fit.decay_days)),
    });
  }
  return out;
}

/* Observed window means for one route, joined onto the fitted curve so a single
 * chart can show the model against what it was fitted to. */
export function leadtimeFitChart(leadtimeFares, fit, routeCode) {
  const observed = (leadtimeFares || [])
    .filter((r) => r.route_code === routeCode)
    .sort((a, b) => a.window_days - b.window_days);
  if (!observed.length) return { rows: [], observed: [] };

  const windows = observed.map((o) => o.window_days);
  const curve = fittedCurve(fit, windows);
  const byWindow = new Map(observed.map((o) => [o.window_days, Number(o.mean_fare)]));

  const rows = curve.map((c) => ({
    window_days: c.window_days,
    fitted: c.fitted,
    observed: byWindow.has(c.window_days) ? byWindow.get(c.window_days) : null,
  }));
  return { rows, observed };
}

export const fitFor = (fits, routeCode) =>
  (fits || []).find((f) => f.route_code === routeCode) || null;


/* ------------------------------------------------------------- nowcast
 *
 * The scoreboard from apix/analytics/nowcast.py. Shipped even though its verdict
 * is negative: a rejected model is a result, and showing the test that rejected
 * it is the point. Nothing is recomputed here.
 */
export function nowcastByHorizon(nowcast) {
  const scores = nowcast?.scores || [];
  const horizons = [...new Set(scores.map((s) => s.horizon))].sort((a, b) => a - b);
  return horizons.map((h) => {
    const at = scores.filter((s) => s.horizon === h).sort((a, b) => a.mase - b.mase);
    const bestBaseline = at.filter((s) => s.is_baseline)[0] || null;
    return {
      horizon: h,
      rows: at.map((s) => ({
        ...s,
        /* The bar a model has to clear is the best BASELINE at the same
         * horizon, never MASE 1.0 — the MASE denominator is a fixed one-step
         * scale, so at h=1 nearly everything scores under 1 including models
         * that lose outright. */
        margin: bestBaseline && !s.is_baseline
          ? (bestBaseline.mase - s.mase) / bestBaseline.mase : null,
        isBestBaseline: bestBaseline && s.model === bestBaseline.model,
      })),
      bestBaseline,
    };
  });
}


/* Observed mean fare for one sector and booking window, by observation day.
 *
 * Returns a lookup, not a series, because the fare calendar asks the question
 * one day at a time. Missing days return null and the calendar falls back to the
 * index value: a day with no observation for THIS cell still has a headline.
 */
export function dailyFareLookup(faresDaily, routeCode, windowDays) {
  const m = new Map();
  for (const r of faresDaily || []) {
    if (r.route_code === routeCode && r.window_days === windowDays) {
      m.set(String(r.scrape_date).slice(0, 10), Number(r.mean_fare));
    }
  }
  return (iso) => (m.has(iso) ? m.get(iso) : null);
}


/* The elementary index for one sector and booking window, by day.
 *
 * Used so a fare-calendar tile is internally consistent: when it shows rupees
 * for BLR-DEL at T+7, the percentage beside them must describe BLR-DEL at T+7
 * too, not the whole basket. Putting a sector's fare next to the basket's
 * movement is two different populations on one tile, and a reader has no way to
 * see that from the tile.
 *
 * This reads `leadtime.json`, which Python already computed: the elementary
 * index is that cell against its own July base, so `index_value - 100` IS the
 * percentage change. No arithmetic happens here.
 */
export function cellIndexLookup(leadtime, routeCode, windowDays) {
  const m = new Map();
  for (const r of leadtime || []) {
    if (r.route_code === routeCode && r.window_days === windowDays) {
      m.set(String(r.index_date).slice(0, 10), Number(r.index_value));
    }
  }
  return (iso) => (m.has(iso) ? m.get(iso) : null);
}
