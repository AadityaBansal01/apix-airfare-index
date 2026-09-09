import React, { useMemo } from "react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ComposedChart,
  Cell,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Badge,
  Card,
  Coverage,
  CoverageBanner,
  Delta,
  Formula,
  ProvenanceBanner,
  Stat,
} from "./Shared";
import {
  ROUTE_SERIES_COLOURS,
  cpiOverlay,
  surgeEpisodes,
  surgeDates as surgeDatesOf,
  fmt,
  heatColour,
  heatTextColour,
  lastChange,
  leadtimeChartData,
  leadtimeFitChart,
  fitFor,
  leadtimePremium,
  longDate,
  monthlyBlindSpot,
  nowcastByHorizon,
  periodChange,
  pct,
  routeDates,
  routeMatrix,
  rupees,
  seriesFor,
  shortDate,
  worstBlindSpot,
} from "../data";

const axis = { stroke: "#7a8aa0", fontSize: 11 };
const gridProps = { stroke: "#e3e8ef", vertical: false };

const tooltipStyle = {
  contentStyle: {
    borderRadius: 8,
    border: "1px solid #e7e7e7",
    boxShadow: "0 2px 12px rgba(0,0,0,.1)",
    fontSize: 12,
  },
};

/* ============================================================== OVERVIEW */

export function Overview({ data, frequency, date, onDate }) {
  const rows = seriesFor(data.series, frequency);
  const latest = rows[rows.length - 1];
  // Episodes come from apix/analytics/anomaly.py via anomalies.json. Nothing
  // is detected in the browser: the detector is tested in Python and a second
  // implementation here would drift from it.
  const surges = useMemo(
    () => (frequency === "daily" ? surgeEpisodes(data.anomalies) : []),
    [data, frequency]
  );
  const surgeDates = surgeDatesOf(frequency === "daily" ? data.anomalies : []);

  if (!latest) return <Card title="No data">Nothing built for this frequency.</Card>;

  return (
    <>
      <ProvenanceBanner provenance={data.provenance} />
      <CoverageBanner provenance={data.provenance} />

      <Card>
        <div className="headline">
          <div>
            <div className="stat-label">APIx · {frequency}</div>
            <div className="headline-value num">{fmt(latest.index_value)}</div>
            <div className="stat-sub">
              {longDate(latest.index_date)} · base = 100 over the price
              reference period
            </div>
          </div>
          <div className="headline-delta">
            <div className="stat-label">vs previous</div>
            <Delta value={lastChange(rows)} />
          </div>
          <div className="headline-delta">
            <div className="stat-label">over the period</div>
            <Delta value={periodChange(rows)} />
          </div>
          <div style={{ marginLeft: "auto", paddingBottom: 6 }}>
            <Coverage ratio={latest.coverage_ratio} />
          </div>
        </div>

        <div className="stat-row">
          <Stat
            label="cells in basket"
            value={latest.n_cells}
            sub="routes × advance windows"
          />
          <Stat
            label="traffic observed"
            value={pct(latest.observed_pax_share)}
            sub="share of domestic passengers"
          />
          <Stat
            label="observations"
            value={rows.length}
            sub={`${shortDate(rows[0].index_date)} – ${shortDate(latest.index_date)}`}
          />
          <Stat
            label="surges flagged"
            value={surges.length}
            sub="rolling MAD, |z| > 3.5"
          />
        </div>
      </Card>

      <Card
        title="Index series"
        note="The whole point of a high-frequency index: this moves daily, while the CPI Transport sub-group it would augment is published once a month. Flagged points are days whose move is large relative to the series' own recent volatility."
        right={<Badge kind="info">base = 100</Badge>}
      >
        <ResponsiveContainer width="100%" height={320}>
          <AreaChart data={rows} margin={{ top: 6, right: 12, left: -12, bottom: 0 }}>
            <defs>
              <linearGradient id="apixFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#0a72d8" stopOpacity={0.26} />
                <stop offset="100%" stopColor="#0a72d8" stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid {...gridProps} />
            <XAxis dataKey="index_date" tickFormatter={shortDate} {...axis} minTickGap={28} />
            {/* Integer ticks: recharts' default gives four decimal places here,
                which overflows the gutter and clips the leading digit. */}
            <YAxis
              domain={["dataMin - 4", "dataMax + 4"]}
              tickFormatter={(v) => Math.round(v)}
              {...axis}
              width={44}
            />
            <Tooltip
              {...tooltipStyle}
              labelFormatter={longDate}
              formatter={(v) => [fmt(v), "APIx"]}
            />
            <ReferenceLine
              y={100}
              stroke="#7a8aa0"
              strokeDasharray="4 4"
              label={{
                value: "base 100",
                position: "insideTopRight",
                fontSize: 10,
                fill: "#7a8aa0",
              }}
            />
            <Area
              type="monotone"
              dataKey="index_value"
              stroke="#0a72d8"
              strokeWidth={2.4}
              fill="url(#apixFill)"
              isAnimationActive={false}
              dot={(props) => {
                const flagged = surgeDates.has(props.payload.index_date);
                return flagged ? (
                  <circle
                    key={props.payload.index_date}
                    cx={props.cx}
                    cy={props.cy}
                    r={4.5}
                    fill="#c4321f"
                    stroke="#fff"
                    strokeWidth={1.5}
                  />
                ) : null;
              }}
              activeDot={{ r: 5 }}
            />
          </AreaChart>
        </ResponsiveContainer>
      </Card>

      {surges.length > 0 && (
        <Card
          title="Anomalies"
          note="Detected in Python with a rolling median and median absolute deviation, then grouped into episodes. Robust statistics are used rather than a mean and standard deviation so that one large move cannot inflate the yardstick enough to conceal itself, and days already flagged are excluded from the baseline so an event cannot distort the judgement of the days after it. A multi-day surge is one episode. Every flag is statistical; the reading column names a candidate to check, never a cause."
        >
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Episode</th>
                  <th className="right">Peak APIx</th>
                  <th className="right">Peak change</th>
                  <th className="right">Robust score</th>
                  <th>Candidate</th>
                </tr>
              </thead>
              <tbody>
                {surges.map((s) => (
                  <tr key={s.start}>
                    <td>
                      {longDate(s.peak_date)}
                      {s.start !== s.end && (
                        <span style={{ color: "var(--ink-muted)", fontSize: 11 }}>
                          {" "}({shortDate(s.start)}–{shortDate(s.end)})
                        </span>
                      )}
                    </td>
                    <td className="right num">{fmt(s.peak_index_value)}</td>
                    <td className="right">
                      <Delta value={s.peak_change_pct / 100} />
                    </td>
                    <td className="right num">{fmt(s.score)}</td>
                    <td style={{ fontSize: 12 }}>
                      <Badge kind={s.direction === "spike" ? "bad" : "ok"}>
                        {s.direction === "spike" ? "fare spike" : "fare drop"}
                      </Badge>
                      {(s.hypotheses || []).map((h) => (
                        <div key={h} style={{ color: "var(--ink-muted)", marginTop: 3 }}>
                          {h}
                        </div>
                      ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}

/* ================================================================ ROUTES */

export function Routes({ data, frequency, date, onPick, selectedRoute }) {
  const dates = routeDates(data.routes, frequency);
  // Honour the shared observation date; fall back to the latest built day.
  const latestDate = date && dates.includes(date) ? date : dates[dates.length - 1];
  const rows = routeMatrix(data.routes, frequency, latestDate);

  const heatDates = dates.slice(-14);
  const codes = [...new Set(rows.map((r) => r.route_code))];
  const heat = {};
  for (const r of data.routes.filter((x) => x.frequency === frequency)) {
    (heat[r.route_code] ||= {})[r.index_date] = r.index_value;
  }

  return (
    <>
      <ProvenanceBanner provenance={data.provenance} />

      <Card
        title="Sector heatmap"
        note="Each cell is one route's index on one day. Colour diverges around 100 because a price index has a meaningful midpoint: green is cheaper than the reference period, red is dearer."
        right={<Badge kind="info">{frequency}</Badge>}
      >
        <div
          className="heat-grid"
          style={{
            gridTemplateColumns: `120px repeat(${heatDates.length}, minmax(30px, 1fr))`,
          }}
        >
          <div />
          {heatDates.map((d) => (
            <div className="heat-head" key={d}>
              {shortDate(d)}
            </div>
          ))}
          {codes.map((code) => (
            <React.Fragment key={code}>
              <div className="heat-label">{code}</div>
              {heatDates.map((d) => {
                const v = heat[code]?.[d];
                return (
                  <div
                    key={code + d}
                    className="heat-cell num"
                    style={{
                      background: v ? heatColour(v) : "#f7f9fc",
                      color: v ? heatTextColour(v) : "#a8b4c4",
                    }}
                    title={v ? `${code} · ${longDate(d)} · ${fmt(v)}` : "no data"}
                  >
                    {v ? Math.round(v) : "–"}
                  </div>
                );
              })}
            </React.Fragment>
          ))}
        </div>
        <div className="legend">
          <span>cheaper</span>
          <div className="legend-scale">
            {[60, 70, 80, 90, 100, 110, 120, 130, 140].map((v) => (
              <div key={v} style={{ flex: 1, background: heatColour(v) }} />
            ))}
          </div>
          <span>dearer</span>
          <span style={{ marginLeft: 8 }}>· midpoint 100 = reference period</span>
        </div>
      </Card>

      <Card
        title="Route breakdown"
        note={`Weights come from DGCA city-pair passenger traffic. Contribution is the route's weighted share of the headline, so the column sums to the index itself — the arithmetic is checkable by hand from this table.`}
        right={<Badge kind="grey">{latestDate && longDate(latestDate)}</Badge>}
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Sector</th>
                <th className="right">Index</th>
                <th className="right">Weight</th>
                <th className="right">Contribution</th>
                <th style={{ width: 140 }}>Relative to base</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.route_code}>
                  <td>
                    <div className="route-pair">
                      <span className="route-code">{r.origin}</span>
                      <span className="route-arrow">⇄</span>
                      <span className="route-code">{r.destination}</span>
                    </div>
                  </td>
                  <td className="right num" style={{ fontWeight: 700 }}>
                    {fmt(r.index_value)}
                  </td>
                  <td className="right num">{pct(r.weight, 1)}</td>
                  <td className="right num">{fmt(r.contribution)}</td>
                  <td className="bar-cell">
                    <div className="bar-track">
                      <div
                        className="bar-fill"
                        style={{
                          width: `${Math.min(100, Math.abs(r.index_value - 100) * 2.2)}%`,
                          background: heatColour(r.index_value),
                        }}
                      />
                    </div>
                  </td>
                </tr>
              ))}
              <tr style={{ borderTop: "2px solid #e7e7e7", fontWeight: 700 }}>
                <td>All sectors</td>
                <td className="right num">
                  {fmt(rows.reduce((s, r) => s + Number(r.contribution), 0))}
                </td>
                <td className="right num">
                  {pct(rows.reduce((s, r) => s + Number(r.weight), 0), 1)}
                </td>
                <td className="right num">
                  {fmt(rows.reduce((s, r) => s + Number(r.contribution), 0))}
                </td>
                <td />
              </tr>
            </tbody>
          </table>
        </div>
      </Card>

      <Card
        title="Estimated lead-time model"
        note="Fitted in Python by nonlinear least squares on individual fares, not on the five window means, so the intervals reflect the real dispersion. The curve is fare(w) = L × (1 + a·e^(−w/τ)): L is the far-advance floor, a the last-minute premium as a multiple of it, and τ the number of days over which that premium decays. Points are observed window means; the line is the fitted curve."
      >
        <div className="fit-grid">
          {(data.basket || []).map((b) => {
            const fit = fitFor(data.leadtime_fit, b.route_code);
            const { rows } = leadtimeFitChart(data.leadtime_fares, fit, b.route_code);
            if (!fit || !fit.fitted || !rows.length) return null;
            return (
              <div className="fit-cell" key={b.route_code}>
                <div className="fit-head">
                  <span className="route-code">{b.route_code}</span>
                  <span className="fit-r2">R² {fit.r_squared.toFixed(2)}</span>
                </div>
                <ResponsiveContainer width="100%" height={150}>
                  <ComposedChart data={rows} margin={{ top: 6, right: 8, left: -22, bottom: 0 }}>
                    <CartesianGrid {...gridProps} />
                    <XAxis dataKey="window_days" type="number" domain={[0, 46]}
                           ticks={[1, 7, 15, 30, 45]}
                           tickFormatter={(v) => `T+${v}`} {...axis} />
                    <YAxis {...axis} tickFormatter={(v) => `${Math.round(v / 1000)}k`} />
                    <Tooltip {...tooltipStyle}
                      labelFormatter={(v) => `T+${v}`}
                      formatter={(v, n) => [rupees(v), n === "fitted" ? "Fitted" : "Observed mean"]} />
                    <Line type="monotone" dataKey="fitted" stroke="#0a72d8"
                          strokeWidth={2} dot={false} isAnimationActive={false} />
                    <Scatter dataKey="observed" fill="#f26a2e" shape="circle" r={4}
                              isAnimationActive={false} />
                  </ComposedChart>
                </ResponsiveContainer>
                <div className="fit-params">
                  <span>a <strong>{fit.amplitude.toFixed(2)}</strong>
                    <em>[{fit.amplitude_ci[0].toFixed(2)}–{fit.amplitude_ci[1].toFixed(2)}]</em></span>
                  <span>τ <strong>{fit.decay_days.toFixed(1)}d</strong>
                    <em>[{fit.decay_days_ci[0].toFixed(1)}–{fit.decay_days_ci[1].toFixed(1)}]</em></span>
                  <span>floor <strong>{rupees(fit.floor)}</strong></span>
                </div>
              </div>
            );
          })}
        </div>
        <p className="card-note" style={{ marginTop: 14, marginBottom: 0 }}>
          The log-linear slope, an average of about{" "}
          {(() => {
            const v = (data.leadtime_fit || []).filter((f) => f.fitted)
              .map((f) => f.percent_per_day).filter((x) => x != null);
            return v.length ? (v.reduce((a, b) => a + b, 0) / v.length).toFixed(2) : "–";
          })()}% per extra day of lead time, is reported by the API as a single
          headline number. It is deliberately misspecified: it averages a steep
          final fortnight together with a nearly flat far-advance period, which
          is exactly the structure these curves exist to show.
        </p>
      </Card>
    </>
  );
}

/* ============================================================= LEAD TIME */

export function LeadTime({ data, route, window: win, onWindow, onPick }) {
  const chart = leadtimeChartData(data.leadtime_fares);
  const premium = leadtimePremium(data.leadtime_fares);
  const codes = [...new Set(data.leadtime_fares.map((r) => r.route_code))];

  return (
    <>
      <ProvenanceBanner provenance={data.provenance} />

      <Card
        title="Lead-time curve"
        note="Mean observed fare against advance-purchase window, per sector. This is the analytical artifact a scraper alone cannot produce: manual CPI collection samples a single booking horizon, so it cannot see this curve at all, and cannot tell a genuine price rise from a shift in when travellers book."
      >
        <ResponsiveContainer width="100%" height={340}>
          <LineChart data={chart} margin={{ top: 6, right: 16, left: -8, bottom: 4 }}>
            <CartesianGrid {...gridProps} />
            <XAxis dataKey="label" {...axis} />
            <YAxis {...axis} width={62} tickFormatter={(v) => `₹${(v / 1000).toFixed(1)}k`} />
            <Tooltip
              {...tooltipStyle}
              formatter={(v, n) => [rupees(v), n]}
              labelFormatter={(l) => `Booked ${l.replace("T+", "")} days ahead`}
            />
            <Legend wrapperStyle={{ fontSize: 11, paddingTop: 8 }} />
            {codes.map((code, i) => (
              <Line
                key={code}
                type="monotone"
                dataKey={code}
                stroke={ROUTE_SERIES_COLOURS[i % ROUTE_SERIES_COLOURS.length]}
                strokeWidth={2.2}
                dot={{ r: 3 }}
                isAnimationActive={false}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </Card>

      <Card
        title="Last-minute premium"
        note="The ratio of the mean fare at the nearest window to the furthest — a plain two-point summary. The estimated model, with confidence intervals, is in the card below."
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Sector</th>
                <th className="right">Fare at T+{premium[0]?.near_window ?? 1}</th>
                <th className="right">Fare at T+{premium[0]?.far_window ?? 45}</th>
                <th className="right">Premium</th>
                <th style={{ width: 150 }} />
              </tr>
            </thead>
            <tbody>
              {premium.map((p) => (
                <tr key={p.route_code}>
                  <td className="route-code">{p.route_code}</td>
                  <td className="right num">{rupees(p.near_fare)}</td>
                  <td className="right num">{rupees(p.far_fare)}</td>
                  <td className="right num" style={{ fontWeight: 700 }}>
                    {p.ratio.toFixed(2)}×
                  </td>
                  <td className="bar-cell">
                    <div className="bar-track">
                      <div
                        className="bar-fill"
                        style={{
                          width: `${Math.min(100, (p.ratio - 1) * 90)}%`,
                          background: "var(--apix-accent)",
                        }}
                      />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card
        title="Estimated lead-time model"
        note="Fitted in Python by nonlinear least squares on individual fares, not on the five window means, so the intervals reflect the real dispersion. The curve is fare(w) = L × (1 + a·e^(−w/τ)): L is the far-advance floor, a the last-minute premium as a multiple of it, and τ the number of days over which that premium decays. Points are observed window means; the line is the fitted curve."
      >
        <div className="fit-grid">
          {(data.basket || []).map((b) => {
            const fit = fitFor(data.leadtime_fit, b.route_code);
            const { rows } = leadtimeFitChart(data.leadtime_fares, fit, b.route_code);
            if (!fit || !fit.fitted || !rows.length) return null;
            return (
              <div className="fit-cell" key={b.route_code}>
                <div className="fit-head">
                  <span className="route-code">{b.route_code}</span>
                  <span className="fit-r2">R² {fit.r_squared.toFixed(2)}</span>
                </div>
                <ResponsiveContainer width="100%" height={150}>
                  <ComposedChart data={rows} margin={{ top: 6, right: 8, left: -22, bottom: 0 }}>
                    <CartesianGrid {...gridProps} />
                    <XAxis dataKey="window_days" type="number" domain={[0, 46]}
                           ticks={[1, 7, 15, 30, 45]}
                           tickFormatter={(v) => `T+${v}`} {...axis} />
                    <YAxis {...axis} tickFormatter={(v) => `${Math.round(v / 1000)}k`} />
                    <Tooltip {...tooltipStyle}
                      labelFormatter={(v) => `T+${v}`}
                      formatter={(v, n) => [rupees(v), n === "fitted" ? "Fitted" : "Observed mean"]} />
                    <Line type="monotone" dataKey="fitted" stroke="#0a72d8"
                          strokeWidth={2} dot={false} isAnimationActive={false} />
                    <Scatter dataKey="observed" fill="#f26a2e" shape="circle" r={4}
                              isAnimationActive={false} />
                  </ComposedChart>
                </ResponsiveContainer>
                <div className="fit-params">
                  <span>a <strong>{fit.amplitude.toFixed(2)}</strong>
                    <em>[{fit.amplitude_ci[0].toFixed(2)}–{fit.amplitude_ci[1].toFixed(2)}]</em></span>
                  <span>τ <strong>{fit.decay_days.toFixed(1)}d</strong>
                    <em>[{fit.decay_days_ci[0].toFixed(1)}–{fit.decay_days_ci[1].toFixed(1)}]</em></span>
                  <span>floor <strong>{rupees(fit.floor)}</strong></span>
                </div>
              </div>
            );
          })}
        </div>
        <p className="card-note" style={{ marginTop: 14, marginBottom: 0 }}>
          The log-linear slope, an average of about{" "}
          {(() => {
            const v = (data.leadtime_fit || []).filter((f) => f.fitted)
              .map((f) => f.percent_per_day).filter((x) => x != null);
            return v.length ? (v.reduce((a, b) => a + b, 0) / v.length).toFixed(2) : "–";
          })()}% per extra day of lead time, is reported by the API as a single
          headline number. It is deliberately misspecified: it averages a steep
          final fortnight together with a nearly flat far-advance period, which
          is exactly the structure these curves exist to show.
        </p>
      </Card>
    </>
  );
}

/* ============================================================ COMPLIANCE */

const verdictKind = {
  scrape_ok: "ok",
  scrape_with_caution: "warn",
  do_not_scrape: "bad",
  unknown: "grey",
};

export function Compliance({ data }) {
  const tiers = {
    1: "Collected",
    2: "Licensed feed required",
    3: "Excluded — bot management",
    4: "Excluded — robots.txt",
  };
  const bySource = Object.fromEntries(data.compliance.map((c) => [c.source, c]));

  return (
    <>
      <Card
        title="Source audit"
        note="Every candidate source was checked against its live robots.txt, its terms of use, and its bot-management stack before a single request was made. Where a source could only be collected by evasion, it is excluded and the reason is recorded here rather than quietly dropped."
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th>Type</th>
                <th>Verdict</th>
                <th>Disposition</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {data.sources.map((s) => (
                <tr key={s.code}>
                  <td style={{ fontWeight: 600 }}>{s.display_name}</td>
                  <td>
                    <Badge kind="grey">{s.kind}</Badge>
                  </td>
                  <td>
                    <Badge kind={verdictKind[s.audit_verdict] || "grey"}>
                      {s.audit_verdict.replace(/_/g, " ")}
                    </Badge>
                  </td>
                  <td>{tiers[s.tier] || "—"}</td>
                  <td style={{ fontSize: 12, color: "#7a8aa0", maxWidth: 380 }}>
                    {s.exclusion_reason || (s.enabled ? "Collected within rate limits" : "—")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <Card
        title="Request log"
        note="Written by the fetch layer itself, in a finally block, so a request that crashes still leaves a record. A database CHECK constraint rejects any row claiming a successful fetch of a URL that robots.txt disallowed, which means compliance is enforced by the schema and not merely by well-behaved code."
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Source</th>
                <th className="right">Requests</th>
                <th className="right">Sent</th>
                <th className="right">Blocked</th>
                <th className="right">Min gap</th>
                <th className="right">Avg gap</th>
              </tr>
            </thead>
            <tbody>
              {data.compliance.map((c) => (
                <tr key={c.source}>
                  <td style={{ fontWeight: 600 }}>{c.source}</td>
                  <td className="right num">{c.requests}</td>
                  <td className="right num">{c.requests_sent}</td>
                  <td className="right num">{c.robots_blocked}</td>
                  <td className="right num">
                    {c.min_delay_s === null ? "–" : `${fmt(c.min_delay_s, 1)}s`}
                  </td>
                  <td className="right num">
                    {c.avg_delay_s === null ? "–" : `${fmt(c.avg_delay_s, 1)}s`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="card-note" style={{ marginTop: 14, marginBottom: 0 }}>
          Delay statistics cover requests actually sent. A refused request
          correctly waits zero seconds, and counting those zeros would make a
          well-behaved crawler look like it hammered the origin.
        </p>
      </Card>
    </>
  );
}

/* =========================================================== METHODOLOGY */

export function Methodology({ data }) {
  const p = data.provenance;
  return (
    <>
      <Card
        title="How the index is built"
        note="Mirrors CPI practice so the series can sit alongside official statistics rather than beside them."
      >
        <Formula>
          <div style={{ marginBottom: 10 }}>
            <strong>Elementary aggregate</strong> — one route × advance window,
            over carriers. Jevons, the geometric mean of price relatives:
          </div>
          <div style={{ paddingLeft: 14, color: "#0a72d8" }}>
            I(r,w,t) = 100 × ∏<sub>c</sub> ( p(c,t) / p(c,0) )<sup>1/N</sup>
          </div>
          <div style={{ margin: "14px 0 10px" }}>
            <strong>Upper level</strong> — across cells, weighted by DGCA
            passenger traffic. Young / modified Laspeyres:
          </div>
          <div style={{ paddingLeft: 14, color: "#0a72d8" }}>
            APIx(t) = Σ ω(r,w) × I(r,w,t) ÷ Σ ω(r,w)
          </div>
        </Formula>
        <p className="card-note" style={{ marginTop: 16 }}>
          Jevons at the elementary level matches the ILO and IMF recommendation
          for tightly substitutable items, and airline seats on one route on one
          day are close substitutes. It is also transitive, so the index does not
          depend on which day is chosen as the base. The denominator renormalises
          over observed cells, so a missing cell reduces coverage rather than
          silently biasing the level.
        </p>
      </Card>

      <div className="grid-2">
        <Card title="Reference periods">
          <dl className="kv">
            <dt>Price reference</dt>
            <dd>Base = 100, geometric mean of daily prices</dd>
            <dt>Weight reference</dt>
            <dd>
              {p.weight_reference?.start} – {p.weight_reference?.end}
            </dd>
            <dt>Weight source</dt>
            <dd style={{ fontWeight: 400, fontSize: 12 }}>
              {p.weight_reference?.source}
            </dd>
            <dt>Index span</dt>
            <dd>
              {p.first_index_date} – {p.last_index_date}
            </dd>
            <dt>Engine</dt>
            <dd>index-v1</dd>
          </dl>
        </Card>

        <Card title="Missing data policy">
          <dl className="kv">
            <dt>Carrier absent</dt>
            <dd style={{ fontWeight: 400 }}>
              Jevons runs over observed carriers; no imputation needed
            </dd>
            <dt>Cell absent, 1–2 days</dt>
            <dd style={{ fontWeight: 400 }}>Last value carried forward, flagged</dd>
            <dt>Cell absent, 3+ days</dt>
            <dd style={{ fontWeight: 400 }}>
              Dropped and renormalised; coverage falls below 1
            </dd>
            <dt>Sold out</dt>
            <dd style={{ fontWeight: 400 }}>
              Recorded, excluded from the index — it is not a price
            </dd>
          </dl>
        </Card>
      </div>

      <Card
        title="Basket"
        note="Eight trunk sectors by DGCA passenger traffic, each collected at five advance-purchase windows."
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Sector</th>
                <th className="right">Annual passengers</th>
                <th className="right">Weight</th>
              </tr>
            </thead>
            <tbody>
              {data.basket.map((b) => (
                <tr key={b.route_code}>
                  <td className="route-code">{b.route_code}</td>
                  <td className="right num">
                    {Number(b.passengers).toLocaleString("en-IN")}
                  </td>
                  <td className="right num">{pct(b.weight, 2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ marginTop: 16 }}>
          <div className="stat-label" style={{ marginBottom: 8 }}>
            Advance-purchase windows
          </div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {data.windows.map((w) => (
              <Badge key={w.window_days} kind="info">
                T+{w.window_days} · {pct(w.weight, 0)}
              </Badge>
            ))}
          </div>
        </div>
      </Card>

      <Card title="Known limitations">
        <ul style={{ margin: 0, paddingLeft: 20, lineHeight: 1.9, fontSize: 13 }}>
          <li>
            <strong>Coverage is {pct(p.observable_share)} of traffic.</strong>{" "}
            The carriers that can be collected ethically are the two smallest.
            This is the binding constraint on the whole project.
          </li>
          <li>
            <strong>Weights are national, not route-level.</strong> DGCA
            publishes carrier traffic and city-pair traffic separately, never
            crossed, so a carrier's national share stands in for its share on
            each route. On trunk routes this flatters the coverage figure, so
            treat it as an upper bound.
          </li>
          <li>
            <strong>Lowest-fare price concept.</strong> The index tracks the
            cheapest purchasable seat, which is what a traveller faces, not an
            average over booking classes they cannot buy.
          </li>
          <li>
            <strong>No revision policy yet.</strong> A published statistic needs
            a stated rule for when and how figures are revised. This has none.
          </li>
        </ul>
      </Card>
    </>
  );
}


/* ============================================================ CPI OVERLAY */

/* The argument this project rests on, in two parts.
 *
 * Part one needs no CPI data at all: contrast the daily index against the
 * single value a once-a-month collector would have recorded. That comparison
 * is self-contained and is why this view is useful even before anyone has
 * downloaded the official series.
 *
 * Part two overlays the official CPI Transport series when it has been loaded.
 * It is absent by default and stays absent rather than being approximated:
 * MoSPI's data endpoints are not reachable programmatically from outside
 * India, and inventing a government statistic to complete a chart would be a
 * far worse failure than an empty panel that says why it is empty.
 */
export function CpiOverlay({ data }) {
  const daily = seriesFor(data.series, "daily");
  const monthly = seriesFor(data.series, "monthly");
  const blind = useMemo(() => monthlyBlindSpot(data.cpi_dispersion), [data]);
  const worst = useMemo(() => worstBlindSpot(data.cpi_dispersion), [data]);
  const overlay = useMemo(() => cpiOverlay(data.series, data.cpi), [data]);

  /* Daily line with each month's monthly aggregate carried across it, so the
     step a monthly series would show is visible against the movement it
     smooths away. */
  const monthlyByKey = new Map(monthly.map((m) => [m.index_date.slice(0, 7), Number(m.index_value)]));
  const sampledByKey = new Map(blind.map((b) => [b.month, b.sampled]));
  const chart = daily.map((d) => {
    const k = d.index_date.slice(0, 7);
    return {
      date: d.index_date,
      apix: Number(d.index_value),
      monthly: monthlyByKey.get(k) ?? null,
      sampled: sampledByKey.get(k) ?? null,
    };
  });

  return (
    <>
      <ProvenanceBanner provenance={data.provenance} />

      {worst && (
        <div className="coverage-banner">
          <strong>
            What a monthly observation misses: {fmt(worst.missed, 1)} index points
            in {new Date(worst.month + "-01T00:00:00Z").toLocaleDateString("en-IN",
              { month: "long", year: "numeric", timeZone: "UTC" })}.
          </strong>{" "}
          A collector sampling once mid-month would have recorded{" "}
          <strong>{fmt(worst.sampled)}</strong>. The index peaked at{" "}
          <strong>{fmt(worst.max)}</strong> that month and ranged{" "}
          {fmt(worst.range, 1)} points end to end. The monthly figure is not
          wrong; it is a point estimate of something that moved{" "}
          {fmt(worst.missedPct, 0)}% above it and came back before the next visit.
        </div>
      )}

      <Card
        title="Daily index against monthly sampling"
        note="The blue line is the daily index. The step line is the monthly aggregate, and the dashed marks are the value a single mid-month observation would have recorded. Where the three separate is precisely the information a monthly series cannot carry."
      >
        <ResponsiveContainer width="100%" height={330}>
          <LineChart data={chart} margin={{ top: 6, right: 16, left: -8, bottom: 4 }}>
            <CartesianGrid {...gridProps} />
            <XAxis dataKey="date" tickFormatter={shortDate} {...axis} minTickGap={28} />
            <YAxis {...axis} domain={["auto", "auto"]} />
            <Tooltip
              {...tooltipStyle}
              labelFormatter={longDate}
              formatter={(v, n) => [
                v === null ? "–" : fmt(v),
                n === "apix" ? "APIx (daily)"
                  : n === "monthly" ? "Monthly aggregate"
                  : "Mid-month sample",
              ]}
            />
            <Legend
              wrapperStyle={{ fontSize: 11 }}
              formatter={(n) =>
                n === "apix" ? "APIx (daily)"
                  : n === "monthly" ? "Monthly aggregate"
                  : "Mid-month sample"}
            />
            <ReferenceLine y={100} stroke="#7a8aa0" strokeDasharray="4 4" />
            <Line type="monotone" dataKey="apix" stroke="#0a72d8" strokeWidth={2}
                  dot={false} isAnimationActive={false} />
            <Line type="stepAfter" dataKey="monthly" stroke="#f26a2e" strokeWidth={2}
                  dot={false} isAnimationActive={false} connectNulls />
            <Line type="stepAfter" dataKey="sampled" stroke="#c4321f" strokeWidth={1.5}
                  strokeDasharray="5 4" dot={false} isAnimationActive={false} connectNulls />
          </LineChart>
        </ResponsiveContainer>
      </Card>

      <Card
        title="Monthly blind spot"
        note="For each month: the spread of the daily index, and how far a single mid-month reading sat from the month's peak. Months with fewer than twenty observed days are shown but are not comparable, because a partial month understates its own range."
      >
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Month</th>
                <th className="right">Days</th>
                <th className="right">Low</th>
                <th className="right">High</th>
                <th className="right">Range</th>
                <th className="right">Mid-month reading</th>
                <th className="right">Missed vs peak</th>
              </tr>
            </thead>
            <tbody>
              {blind.map((b) => {
                const partial = b.n_days < 20;
                return (
                  <tr key={b.month}>
                    <td style={{ fontWeight: 700 }}>
                      {new Date(b.month + "-01T00:00:00Z").toLocaleDateString("en-IN",
                        { month: "short", year: "numeric", timeZone: "UTC" })}
                      {partial && <> <Badge kind="grey">partial</Badge></>}
                    </td>
                    <td className="right num">{b.n_days}</td>
                    <td className="right num">{fmt(b.min)}</td>
                    <td className="right num">{fmt(b.max)}</td>
                    <td className="right num">{fmt(b.range, 1)}</td>
                    <td className="right num">{fmt(b.sampled)}</td>
                    <td className="right num" style={{ fontWeight: 700 }}>
                      {partial ? "–" : `${fmt(b.missed, 1)} (${fmt(b.missedPct, 0)}%)`}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>

      <Card
        title="Against the official CPI Transport series"
        note="Both series rebased to 100 at their first common month. Rebasing rather than using two independent axes: APIx is 100 over its own price reference period and the CPI 2024 series is 100 in calendar 2024, and a second axis can be scaled to manufacture any apparent agreement."
        right={overlay.length
          ? <Badge kind="ok">CPI loaded</Badge>
          : <Badge kind="warn">CPI not loaded</Badge>}
      >
        {overlay.length ? (
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={overlay} margin={{ top: 6, right: 16, left: -8, bottom: 4 }}>
              <CartesianGrid {...gridProps} />
              <XAxis dataKey="month" {...axis} />
              <YAxis {...axis} domain={["auto", "auto"]} />
              <Tooltip {...tooltipStyle}
                formatter={(v, n) => [fmt(v), n === "apix" ? "APIx (rebased)" : "CPI Transport (rebased)"]} />
              <Legend wrapperStyle={{ fontSize: 11 }}
                formatter={(n) => (n === "apix" ? "APIx, monthly" : "CPI Division 07 Transport")} />
              <ReferenceLine y={100} stroke="#7a8aa0" strokeDasharray="4 4" />
              <Line type="monotone" dataKey="apix" stroke="#0a72d8" strokeWidth={2}
                    dot={{ r: 3 }} isAnimationActive={false} />
              <Line type="monotone" dataKey="cpi" stroke="#94a3b8" strokeWidth={2}
                    strokeDasharray="6 4" dot={{ r: 3 }} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="empty-state">
            <div className="empty-title">The official CPI series has not been loaded.</div>
            <p>
              This panel is deliberately empty rather than approximated. MoSPI
              publishes CPI through the eSankhyiki portal, whose data endpoints
              return the portal application rather than data when called from
              outside India, so the series could not be retrieved
              programmatically. Estimating a government statistic to fill a chart
              would be a worse failure than an empty panel.
            </p>
            <p>
              To populate it, export <strong>Division 07 Transport</strong>,
              All India, Combined, monthly from{" "}
              <a href="https://esankhyiki.mospi.gov.in/macroindicators?product=cpi"
                 target="_blank" rel="noopener noreferrer">eSankhyiki</a>, then:
            </p>
            <Formula>
              python scripts/load_cpi.py --csv &lt;file&gt;{"\n"}make export
            </Formula>
            <p className="empty-note">
              Everything above on this page already works without it. The
              comparison that matters — daily movement against monthly sampling —
              is made with APIx alone.
            </p>
          </div>
        )}
      </Card>
    </>
  );
}

/* ============================================================== NOWCAST
 *
 * A view whose whole content is a negative result.
 *
 * It exists because a project that only ships the components that worked is not
 * one a statistical office should trust with a price index. The honest finding —
 * that nothing beats "tomorrow will be like today" at this sample size — is more
 * useful to a reader than a forecast line drawn on a chart would have been, and
 * the scoreboard shows the test that established it.
 */
export function Nowcast({ data }) {
  const nc = data.nowcast;
  const byH = nowcastByHorizon(nc);
  const negative = (nc?.recommendation || "").startsWith("NO NOWCAST");

  if (!nc || !nc.scores?.length) {
    return (
      <Card title="Forecast evaluation">
        <p className="card-note">
          Not enough history to evaluate a nowcast. {nc?.recommendation}
        </p>
      </Card>
    );
  }

  return (
    <>
      <ProvenanceBanner provenance={data.provenance} />

      <div className={`verdict ${negative ? "verdict-no" : "verdict-yes"}`}>
        <div className="verdict-tag">{negative ? "Result: no" : "Result: yes"}</div>
        <h2 className="verdict-line">
          {negative
            ? "We tried to forecast tomorrow's index. Nothing beat guessing “the same as today”."
            : "A forecast model earned its place."}
        </h2>
        <p className="verdict-body">{nc.recommendation}</p>
      </div>

      <Card
        title="What was tested"
        note="Three baselines and two models, scored the same way. The baselines are not straw men: a price series is close to a random walk, so “tomorrow equals today” is genuinely hard to beat, and airfares have a real weekly cycle, so “the same weekday last week” is the right bar at a week out."
      >
        <div className="model-grid">
          {[
            ["naive", "baseline", "Tomorrow equals today. The random walk."],
            ["seasonal_naive", "baseline", "Tomorrow equals the same weekday last week."],
            ["drift", "baseline", "Random walk plus the average historical slope."],
            ["damped_trend", "model", "Holt's linear method with damping, so the trend flattens instead of extrapolating forever. Three parameters, grid-fitted."],
            ["seasonal_damped_trend", "model", "The same, on a series with the weekly shape removed and added back."],
          ].map(([name, kind, desc]) => (
            <div className={`model-card model-${kind}`} key={name}>
              <div className="model-head">
                <code>{name}</code>
                <span className={`badge badge-${kind === "model" ? "info" : "grey"}`}>
                  {kind}
                </span>
              </div>
              <p>{desc}</p>
            </div>
          ))}
        </div>
      </Card>

      {byH.map(({ horizon, rows, bestBaseline }) => (
        <Card
          key={horizon}
          title={horizon === 1 ? "One day ahead" : `${horizon} days ahead`}
          right={<Badge kind={rows.some((r) => !r.is_baseline && r.margin > 0.05)
            ? "warn" : "grey"}>
            {rows[0]?.n_forecasts} rolling-origin forecasts
          </Badge>}
          note="Lower MASE is better. The bar every model has to clear is the best baseline in this table, not the value 1.0 — the MASE scale is fixed at one step, so at a one-day horizon almost everything scores below 1 including models that lose outright."
        >
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>Method</th>
                  <th className="right">MAE</th>
                  <th className="right">RMSE</th>
                  <th className="right">MASE</th>
                  <th>Verdict</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const wins = r.margin != null && r.margin > 0.05;
                  const loses = r.margin != null && r.margin <= 0;
                  return (
                    <tr key={r.model}
                        className={r.isBestBaseline ? "row-best" : ""}>
                      <td>
                        <code className="model-name">{r.model}</code>
                        {r.is_baseline && (
                          <span className="badge badge-grey">baseline</span>
                        )}
                      </td>
                      <td className="right num">{fmt(r.mae, 3)}</td>
                      <td className="right num">{fmt(r.rmse, 3)}</td>
                      <td className="right num" style={{ fontWeight: 700 }}>
                        {fmt(r.mase, 3)}
                      </td>
                      <td>
                        {r.isBestBaseline ? (
                          <span className="verdict-pill pill-best">best baseline</span>
                        ) : r.is_baseline ? (
                          <span className="verdict-pill pill-flat">baseline</span>
                        ) : wins ? (
                          <span className="verdict-pill pill-win">
                            beats {bestBaseline?.model} by {pct(r.margin, 0)}
                          </span>
                        ) : loses ? (
                          <span className="verdict-pill pill-lose">
                            loses to {bestBaseline?.model} by {pct(-r.margin, 0)}
                          </span>
                        ) : (
                          <span className="verdict-pill pill-flat">
                            ties {bestBaseline?.model}, inside the noise
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {horizon === 1 && (
            <p className="card-note callout-warn">
              <strong>Why the apparent winner was still rejected.</strong> At one
              day ahead <code>seasonal_damped_trend</code> beats{" "}
              <code>naive</code> on mean absolute error and <em>loses to it on
              RMSE</em>. That combination means it is usually closer and
              occasionally much further out — a heavier error tail — and the tail
              falls at turning points, which is exactly when anyone reads a
              nowcast. Publishing the MAE win alone would be choosing the metric
              that flatters.
            </p>
          )}
        </Card>
      ))}

      <Card title="How it was scored">
        <p className="card-note">
          <strong>Rolling origin, not one split.</strong> A single train/test
          split on a {nc.n_observations}-point series measures luck. Every origin
          from day {nc.min_train} onward produces a forecast and is scored, so
          each number above is a mean over dozens of forecasts. Each origin fits
          on data strictly before it, so no model ever sees the value it is asked
          to predict — <code>tests/test_nowcast.py</code> asserts that, because a
          leak would be invisible in the output and would turn the whole exercise
          into a tautology.
        </p>
        <p className="card-note">
          <strong>MASE</strong> is mean absolute error scaled by the in-sample
          one-step seasonal-naive error, which makes it comparable across
          horizons and across a rebased series in a way raw RMSE is not.
        </p>
        <p className="card-note">
          <strong>Two caveats that matter more than the numbers.</strong> This is
          seeded data, so the test measures whether these methods can track a
          series with the generator's dynamics — not whether they forecast Indian
          airfares. And {nc.n_observations} observations is a short series for a
          forecasting comparison, which is why a margin under 5% was treated as
          no improvement at all.
        </p>
      </Card>
    </>
  );
}
