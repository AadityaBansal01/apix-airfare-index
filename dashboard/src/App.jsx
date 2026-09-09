import React, { useEffect, useMemo, useRef, useState } from "react";
import PlainEnglish from "./components/PlainEnglish";
import SearchPanel from "./components/SearchPanel";
import FareCalendar from "./components/FareCalendar";
import FareResults from "./components/FareResults";
import {
  Compliance, CpiOverlay, LeadTime, Methodology, Nowcast, Overview, Routes,
} from "./components/Views";
import {
  cellIndexLookup, dailyFareLookup, fmt, loadAll, longDate, pct, routeDates,
  routeMatrix,
  seriesFor, surgeDates,
} from "./data";

/* The shell.
 *
 * Two things changed from the first version and both are about who is reading.
 *
 * The chrome is a booking site's, not a dashboard's: a dark top bar, a search
 * panel that overlaps it, then results. That grammar is already installed in
 * every visitor's head, so the page needs no orientation.
 *
 * And the selection is REAL. Route, observation date and booking window live
 * here and are threaded into every view, so changing the route in the panel
 * moves the fare calendar, the results list, the lead-time curve and the
 * charts together. The first version's controls only reported state; these
 * drive it, which is the difference between a screenshot and a tool.
 */

const TABS = [
  { id: "plain", label: "Start here" },
  { id: "fares", label: "Fares" },
  { id: "overview", label: "Index" },
  { id: "routes", label: "Sectors" },
  { id: "leadtime", label: "Book early?" },
  { id: "cpi", label: "vs CPI" },
  { id: "nowcast", label: "Forecast" },
  { id: "compliance", label: "How we collect" },
  { id: "methodology", label: "Method" },
];

/* Views whose question is answered at one aggregation level only. Offering a
   frequency control that silently does nothing is worse than not offering it. */
const FREQUENCY_TABS = new Set(["overview", "routes", "fares"]);

export default function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState("plain");
  const [frequency, setFrequency] = useState("daily");
  const [origin, setOrigin] = useState("BLR");
  const [destination, setDestination] = useState("DEL");
  const [win, setWin] = useState(7);
  const [date, setDate] = useState(null);
  const resultsRef = useRef(null);

  useEffect(() => { loadAll().then(setData).catch((e) => setError(e.message)); }, []);

  const rows = useMemo(
    () => (data ? seriesFor(data.series, frequency) : []), [data, frequency]);
  const dates = useMemo(
    () => (data ? routeDates(data.routes, frequency) : []), [data, frequency]);

  /* Default the observation date to the most recent one, and keep it valid when
     the frequency changes underneath it — a weekly date is not a daily date. */
  useEffect(() => {
    if (!dates.length) return;
    setDate((d) => (d && dates.includes(d) ? d : dates[dates.length - 1]));
  }, [dates]);

  const routeCode = `${origin}-${destination}`;
  const canonical = useMemo(() => {
    if (!data) return routeCode;
    const [a, b] = [origin, destination].sort();
    return data.basket.some((r) => r.route_code === `${a}-${b}`)
      ? `${a}-${b}` : routeCode;
  }, [data, origin, destination, routeCode]);

  const matrix = useMemo(
    () => (data && date ? routeMatrix(data.routes, frequency, date) : []),
    [data, frequency, date]);

  const fareFor = useMemo(() => {
    if (!data) return null;
    const row = (data.leadtime_fares || []).find(
      (f) => f.route_code === canonical && f.window_days === win);
    return row ? Number(row.mean_fare) : null;
  }, [data, canonical, win]);

  const flagged = useMemo(
    () => (data ? surgeDates(data.anomalies) : new Set()), [data]);

  /* Real observed fares for the selected sector and window, keyed by day, so
     the calendar can show rupees. Recomputed when the selection moves. */
  const dailyFare = useMemo(
    () => (data ? dailyFareLookup(data.fares_daily, canonical, win) : null),
    [data, canonical, win]);
  const cellIndex = useMemo(
    () => (data ? cellIndexLookup(data.leadtime, canonical, win) : null),
    [data, canonical, win]);

  if (error) {
    return (
      <div className="boot">
        <div className="boot-card">
          <div className="boot-icon">⚠️</div>
          <h1>Could not load index data</h1>
          <p className="boot-err">{error}</p>
          <p>Run <code>make export</code> to regenerate the static payload.</p>
        </div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="boot">
        <div className="boot-card">
          <div className="boot-spin" />
          <h1>Loading the index…</h1>
        </div>
      </div>
    );
  }

  const latest = rows[rows.length - 1];
  const atDate = rows.find((r) => r.index_date === date) || latest;
  const supportsFrequency = FREQUENCY_TABS.has(tab);
  const headline = atDate ? Number(atDate.index_value) : null;

  const setRoute = (o, d) => { setOrigin(o); setDestination(d); };
  const showFares = () => {
    setTab("fares");
    requestAnimationFrame(() =>
      resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  return (
    <div className="app">
      {/* ------------------------------------------------------ top bar */}
      <header className="topbar">
        <div className="topbar-in">
          <a className="brand" href="#top">
            <span className="brand-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" width="19" height="19">
                <path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"
                      fill="currentColor" />
              </svg>
            </span>
            <span className="brand-text">
              APIx<small>Airfare Price Index · India</small>
            </span>
          </a>

          <nav className="topnav">
            {TABS.map((t) => (
              <button key={t.id}
                      className={`topnav-item ${tab === t.id ? "active" : ""}`}
                      onClick={() => setTab(t.id)}>
                {t.label}
              </button>
            ))}
          </nav>

          <div className="topbar-right">
            {/* The seeded state is the single most misreadable thing on the
                page, so it is pinned to the chrome and visible on every view —
                not left to whichever view happens to show a chart. */}
            {data.provenance.seeded && (
              <span className="chip-seeded" title="Synthetic demo data">
                <span className="dot" /> Seeded data
              </span>
            )}
            <a className="topbar-api" href="/docs">API</a>
          </div>
        </div>
      </header>

      {/* --------------------------------------------------- hero + panel */}
      {tab !== "plain" && (
        <section className="hero" id="top">
          <div className="hero-in">
            <div className="hero-copy">
              <h1>What did flying India cost today?</h1>
              <p>
                A daily price index for {data.basket.length} trunk sectors,
                built the way the Consumer Price Index is built — so a fare
                spike that starts and ends inside one month still gets counted.
              </p>
            </div>
            <div className="hero-stats">
              <div>
                <span className="hero-stat num">{latest ? fmt(latest.index_value) : "–"}</span>
                <small>index today (base 100 = July)</small>
              </div>
              <div>
                <span className="hero-stat num">{rows.length}</span>
                <small>days observed</small>
              </div>
              <div>
                <span className="hero-stat num">
                  {latest ? pct(latest.observed_pax_share, 1) : "–"}
                </span>
                <small>of passengers we may legally watch</small>
              </div>
            </div>
          </div>

          <div className="panel-wrap">
            <SearchPanel
              routes={data.basket}
              origin={origin} destination={destination} onRoute={setRoute}
              windows={data.windows} window={win} onWindow={setWin}
              dates={dates} date={date} onDate={setDate}
              frequency={frequency} onFrequency={setFrequency}
              frequencyEnabled={supportsFrequency}
              fareForSelection={fareFor}
              onShow={showFares}
            />
          </div>
        </section>
      )}

      <main className="shell" ref={resultsRef}>
        {tab === "plain" && <PlainEnglish data={data} onStart={() => setTab("fares")} />}

        {tab === "fares" && (
          <>
            <FareCalendar rows={rows} selected={date} onSelect={setDate}
                          surgeDates={flagged}
                          fareFor={frequency === "daily" ? dailyFare : null}
                          indexFor={frequency === "daily" ? cellIndex : null}
                          caption={`${canonical} booked T+${win}`} />
            <FareResults rows={matrix} fares={data.leadtime_fares}
                         fits={data.leadtime_fit} window={win}
                         onPick={setRoute} selectedRoute={canonical}
                         headline={headline} />
          </>
        )}

        {tab === "overview" && (
          <Overview data={data} frequency={frequency} date={date} onDate={setDate} />
        )}
        {tab === "routes" && (
          <Routes data={data} frequency={frequency} date={date}
                  onPick={setRoute} selectedRoute={canonical} />
        )}
        {tab === "leadtime" && (
          <LeadTime data={data} route={canonical} window={win}
                    onWindow={setWin} onPick={setRoute} />
        )}
        {tab === "cpi" && <CpiOverlay data={data} />}
        {tab === "nowcast" && <Nowcast data={data} />}
        {tab === "compliance" && <Compliance data={data} />}
        {tab === "methodology" && <Methodology data={data} />}
      </main>

      <footer className="footer">
        <div className="footer-in">
          <div>
            <strong>APIx</strong> — a daily airfare price index for India, built
            to mirror CPI methodology so it could augment the MoSPI Consumer
            Price Index Transport division.
            <br />
            An independent prototype for Smart India Hackathon 2026 (SIH26056).
            Not affiliated with, endorsed by, or published by MoSPI, DGCA, or any
            airline named on this site.
          </div>
          <div className="footer-meta">
            Snapshot generated{" "}
            {new Date(data.provenance.generated_at).toLocaleString("en-IN",
              { dateStyle: "medium", timeStyle: "short" })}.
            <br />
            The figures are a static export; the index engine, cleaning pipeline
            and compliance log run against PostgreSQL locally.
            {data.provenance.seeded && (
              <>
                <br />
                <strong className="footer-seeded">
                  This deployment shows seeded data, not observed prices.
                </strong>
              </>
            )}
          </div>
        </div>
      </footer>
    </div>
  );
}
