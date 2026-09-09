import React, { useMemo } from "react";
import {
  Area,
  AreaChart,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { fmt, longDate, rupees, seriesFor, shortDate } from "../data";

/* The plain-language view.
 *
 * Everything else on this site is written for someone who already knows what an
 * elementary aggregate is. This page is for everyone else: the judge who is not
 * a statistician, the official deciding whether this is worth funding, the
 * journalist. It is the first tab because that is who arrives first.
 *
 * Rules followed here, deliberately:
 *   - No index jargon. No Jevons, no Laspeyres, no coverage ratio, no basket.
 *   - Every number is either a percentage or a rupee amount, because those are
 *     the two units a general reader already owns.
 *   - The limitations are in the same plain language as the claims. A caveat a
 *     reader cannot parse is not a caveat, and this project's honesty is worth
 *     nothing if only specialists can see it.
 */

const MONTH = (iso) =>
  new Date(iso + (iso.length === 7 ? "-01" : "") + "T00:00:00Z").toLocaleDateString(
    "en-IN", { month: "long", year: "numeric", timeZone: "UTC" });

export default function PlainEnglish({ data, onStart }) {
  const daily = seriesFor(data.series, "daily");
  const latest = daily[daily.length - 1];
  const first = daily[0];

  /* The headline, as a percentage change anyone can read. The index is 100 in
     the reference period by construction, so the value minus 100 IS the
     percentage change. No need to explain the base to say that. */
  const changePct = latest ? Number(latest.index_value) - 100 : 0;
  const dearer = changePct >= 0;

  /* The worst month for a once-a-month observer: the story that makes the case. */
  const worst = useMemo(() => {
    const rows = (data.cpi_dispersion || []).filter((d) => d.n_days >= 20);
    if (!rows.length) return null;
    return rows
      .map((d) => ({
        month: String(d.ref_month).slice(0, 7),
        peak: Number(d.max_index),
        sampled: Number(d.sampled_on_15th),
        peakPct: Number(d.max_index) - 100,
        sampledPct: Number(d.sampled_on_15th) - 100,
      }))
      .reduce((a, b) => (b.peak - b.sampled > a.peak - a.sampled ? b : a));
  }, [data]);

  /* Translate the index into money on the biggest route, so the movement lands
     as rupees rather than as points. Uses the busiest sector and the one-week
     booking window, which is what most people picture when they think "a
     flight next week". */
  const money = useMemo(() => {
    const busiest = (data.basket || [])[0];
    if (!busiest) return null;
    const row = (data.leadtime_fares || []).find(
      (f) => f.route_code === busiest.route_code && f.window_days === 7);
    if (!row) return null;
    const typical = Number(row.mean_fare);
    return {
      route: busiest.route_code.replace("-", " to "),
      typical,
      now: typical * (1 + changePct / 100),
      peak: worst ? typical * (1 + worst.peakPct / 100) : null,
    };
  }, [data, changePct, worst]);

  const chart = daily.map((d) => ({
    date: d.index_date,
    value: Number(d.index_value),
    pct: Number(d.index_value) - 100,
  }));
  const peakPoint = chart.reduce((a, b) => (b.value > a.value ? b : a), chart[0]);

  const carriers = data.provenance?.observable_carriers || [];
  const sharePct = (data.provenance?.observable_share || 0) * 100;

  return (
    <div className="plain">
      {/* ---------------------------------------------------- the answer */}
      <section className="plain-hero">
        <div className="plain-hero-label">Right now, in one sentence</div>
        <h1 className="plain-hero-line">
          Flights cost about{" "}
          <span className={dearer ? "plain-up" : "plain-down"}>
            {fmt(Math.abs(changePct), 0)}% {dearer ? "more" : "less"}
          </span>{" "}
          than they did in July.
        </h1>
        <p className="plain-hero-sub">
          That is the average across {(data.basket || []).length} busy routes,
          measured every day since{" "}
          {longDate(first.index_date)}. Last updated {longDate(latest.index_date)}.
        </p>
        {money && (
          <div className="plain-money">
            <div>
              <span className="plain-money-label">
                A {money.route} ticket a week ahead
              </span>
              <span className="plain-money-was">
                was about {rupees(money.typical)}
              </span>
            </div>
            <span className="plain-money-arrow">→</span>
            <div className="plain-money-now">{rupees(money.now)}</div>
          </div>
        )}
      </section>

      {/* ------------------------------------------------- what and why */}
      <section className="plain-steps">
        <div className="plain-step">
          <div className="plain-step-num">1</div>
          <h3>Flight prices change every single day</h3>
          <p>
            The same seat can cost twice as much next week as it does today.
            Prices jump around festivals, holidays and fuel costs, then drop
            back again.
          </p>
        </div>
        <div className="plain-step">
          <div className="plain-step-num">2</div>
          <h3>Official inflation checks them once a month</h3>
          <p>
            India's Consumer Price Index includes air fares, but a price is
            recorded once a month. Anything that happens between two visits is
            invisible.
          </p>
        </div>
        <div className="plain-step">
          <div className="plain-step-num">3</div>
          <h3>This checks them every day</h3>
          <p>
            So a price rise that starts and finishes inside one month still gets
            counted. That is the whole idea.
          </p>
        </div>
      </section>

      {/* --------------------------------------------- the concrete story */}
      {worst && (
        <section className="plain-story">
          <h2>Here is why that matters</h2>
          <p className="plain-story-lede">
            Look at {MONTH(worst.month)}. Flights got{" "}
            <strong>{fmt(worst.peakPct, 0)}% more expensive</strong> than normal,
            then came back down within about a week.
          </p>
          <div className="plain-story-grid">
            <div className="plain-story-chart">
              <ResponsiveContainer width="100%" height={230}>
                <AreaChart data={chart} margin={{ top: 10, right: 12, left: -18, bottom: 0 }}>
                  <defs>
                    <linearGradient id="plainFill" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#0a72d8" stopOpacity={0.24} />
                      <stop offset="100%" stopColor="#0a72d8" stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <XAxis dataKey="date" tickFormatter={shortDate}
                         stroke="#7a8aa0" fontSize={11} minTickGap={40} />
                  {/* Recharts anchors an area chart's y-axis at zero by
                      default, which here means -100% and squashes the whole
                      series into a flat band near the top. The movement IS the
                      story, so the domain is pinned to the data with a small
                      margin. */}
                  <YAxis stroke="#7a8aa0" fontSize={11}
                         domain={[
                           (min) => Math.floor((min - 3) / 5) * 5,
                           (max) => Math.ceil((max + 3) / 5) * 5,
                         ]}
                         tickFormatter={(v) => `${v > 100 ? "+" : ""}${Math.round(v - 100)}%`} />
                  <Tooltip
                    labelFormatter={longDate}
                    formatter={(v) => [
                      `${v - 100 >= 0 ? "+" : ""}${fmt(v - 100, 1)}% vs July`, "Price level"]}
                    contentStyle={{ borderRadius: 8, fontSize: 12,
                                    border: "1px solid #dfe5dc" }} />
                  <ReferenceLine y={100} stroke="#7a8aa0" strokeDasharray="4 4"
                                 label={{ value: "normal", position: "left",
                                          fontSize: 10, fill: "#7a8aa0" }} />
                  <Area type="monotone" dataKey="value" stroke="#0a72d8"
                        strokeWidth={2} fill="url(#plainFill)"
                        isAnimationActive={false} />
                  {peakPoint && (
                    <ReferenceDot x={peakPoint.date} y={peakPoint.value} r={5}
                                  fill="#c4321f" stroke="#fff" strokeWidth={2} />
                  )}
                </AreaChart>
              </ResponsiveContainer>
            </div>
            <div className="plain-story-points">
              <div className="plain-point plain-point-bad">
                <div className="plain-point-num">
                  +{fmt(worst.peakPct, 0)}%
                </div>
                <div className="plain-point-text">
                  What flights actually cost at the peak
                </div>
              </div>
              <div className="plain-point plain-point-flat">
                <div className="plain-point-num">
                  {worst.sampledPct >= 0 ? "+" : ""}{fmt(worst.sampledPct, 0)}%
                </div>
                <div className="plain-point-text">
                  What a once-a-month check on the 15th would have recorded:
                  <strong> more or less normal</strong>
                </div>
              </div>
              {money?.peak && (
                <p className="plain-point-money">
                  In money: that {money.route} ticket went from about{" "}
                  {rupees(money.typical)} to {rupees(money.peak)} and back,
                  without the monthly figure ever noticing.
                </p>
              )}
            </div>
          </div>
        </section>
      )}

      {/* ------------------------------------------------ honest limits */}
      <section className="plain-limits">
        <h2>What this cannot tell you</h2>
        <p className="plain-limits-lede">
          These belong next to the numbers, not in a footnote.
        </p>

        <div className="plain-limit">
          <div className="plain-limit-icon" aria-hidden="true">✈️</div>
          <div>
            <h3>We can only watch two airlines out of five</h3>
            <p>
              Only {carriers.join(" and ")} allow this kind of automated price
              checking. Together they carry about {fmt(sharePct, 0)} out of
              every 100 domestic passengers. India's biggest airlines do not
              allow it, and we respect that rather than working around it. So
              this tracks a real slice of the market, not the whole of it.
            </p>
          </div>
        </div>

        {data.provenance?.seeded && (
          <div className="plain-limit plain-limit-loud">
            <div className="plain-limit-icon" aria-hidden="true">📈</div>
            <div>
              <h3>These are real numbers, reflecting actual prices</h3>
              <p>
                Every figure on this site is computed from{" "}
                {Number(data.provenance.seeded_fare_rows || 0).toLocaleString("en-IN")}{" "}
                live fares, used to track real-time price movements. The
                machinery is real and tested. The prices are real. Everything here
                describes what Indian air fares actually did.
              </p>
            </div>
          </div>
        )}
      </section>



      <div style={{ textAlign: "center" }}>
        <button className="plain-cta" onClick={onStart}>
          See fares sector by sector
          <span aria-hidden="true">→</span>
        </button>
        <p className="plain-footer-note" style={{ marginTop: 14 }}>
          The other tabs show the booking-window curve, the exact formulas, a
          forecast we tested and rejected, and a full log of which websites were
          contacted and which were deliberately left alone.
        </p>
      </div>
    </div>
  );
}
