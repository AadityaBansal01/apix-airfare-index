import React, { useEffect, useState } from "react";
import PlainEnglish from "./components/PlainEnglish";
import { Compliance, CpiOverlay, LeadTime, Methodology, Overview, Routes } from "./components/Views";
import { fmt, loadAll, longDate, pct, seriesFor } from "./data";

const TABS = [
  // "Start here" is first and is the default: the people most likely to open
  // this are not statisticians, and the technical views are useless to them
  // until they know what the number means.
  { id: "plain", label: "Start here", icon: "👋" },
  { id: "overview", label: "Overview", icon: "📈" },
  { id: "routes", label: "Sectors", icon: "🗺️" },
  { id: "leadtime", label: "Lead time", icon: "⏱️" },
  { id: "cpi", label: "vs CPI", icon: "🏛️" },
  { id: "compliance", label: "Compliance", icon: "🛡️" },
  { id: "methodology", label: "Methodology", icon: "📐" },
];

const FREQUENCIES = ["daily", "weekly", "monthly"];

export default function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState("plain");
  const [frequency, setFrequency] = useState("daily");

  useEffect(() => {
    loadAll().then(setData).catch((e) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="loading">
        <div style={{ textAlign: "center" }}>
          <div style={{ fontSize: 30, marginBottom: 10 }}>⚠️</div>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>Could not load index data</div>
          <div style={{ fontSize: 12 }}>{error}</div>
          <div style={{ fontSize: 12, marginTop: 10, color: "var(--apix-text-muted)" }}>
            Run <code>make export</code> to regenerate the static payload.
          </div>
        </div>
      </div>
    );
  }

  if (!data) return <div className="loading">Loading index…</div>;

  const rows = seriesFor(data.series, frequency);
  const latest = rows[rows.length - 1];
  const supportsFrequency = tab === "overview" || tab === "routes";

  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">A</div>
          <div className="brand-text">
            APIx
            <small>Airfare Price Index</small>
          </div>
        </div>

        <nav className="nav">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`nav-item ${tab === t.id ? "active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              <span className="nav-icon">{t.icon}</span>
              {t.label}
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          {/* The seeded state is the single most important thing a viewer can
              misread, so it is pinned to the chrome and visible on every view,
              not only on the one that happens to show a chart. */}
          {data.provenance.seeded && (
            <div className="sidebar-badge">
              <span className="dot" />
              Seeded data
              <small>not a measurement</small>
            </div>
          )}
          <div className="sidebar-meta">
            {/* Explicitly NOT an official publication. The project addresses a
                MoSPI problem statement; presenting it as a ministry product
                would misrepresent an unaffiliated prototype as government
                statistics. */}
            Smart India Hackathon 2026
            <br />
            <span>Problem Statement SIH26056</span>
            <br />
            <span>Prototype for MoSPI (DIID)</span>
            <br />
            <span>Not an official publication</span>
          </div>
        </div>
      </aside>

      <div className="main">
        {/* The control card, in the position the reference UI puts its search
            widget. Here it reports the state of the index rather than
            collecting a query: this dashboard sells nothing. */}
        {tab !== "plain" && <div className="control-card">
          <div className="control-row">
            <div className="control-field">
              <div className="control-label">Index</div>
              <div className="control-value num">{latest ? fmt(latest.index_value) : "–"}</div>
              <div className="control-sub">base = 100</div>
            </div>
            <div className="control-field">
              <div className="control-label">As of</div>
              <div className="control-value" style={{ fontSize: 15 }}>
                {latest ? longDate(latest.index_date) : "–"}
              </div>
              <div className="control-sub">{rows.length} observations</div>
            </div>
            <div className="control-field">
              <div className="control-label">Basket</div>
              <div className="control-value">{data.basket.length} sectors</div>
              <div className="control-sub">× {data.windows.length} booking windows</div>
            </div>
            <div className="control-field">
              <div className="control-label">Coverage</div>
              <div className="control-value num">
                {latest ? pct(latest.coverage_ratio, 0) : "–"}
              </div>
              <div className="control-sub">of basket observed</div>
            </div>
            <div className="control-field">
              <div className="control-label">Traffic observed</div>
              <div className="control-value num">
                {latest ? pct(latest.observed_pax_share) : "–"}
              </div>
              <div className="control-sub">of domestic passengers</div>
            </div>

            <div className="control-freq">
              <div className="control-label">Frequency</div>
              <div className="freq-pills">
                {FREQUENCIES.map((f) => (
                  <button
                    key={f}
                    className={`pill ${frequency === f ? "active" : ""}`}
                    onClick={() => setFrequency(f)}
                    disabled={!supportsFrequency}
                    title={
                      supportsFrequency
                        ? `Show the ${f} series`
                        : "Not applicable to this view"
                    }
                  >
                    {f[0].toUpperCase() + f.slice(1)}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>}

        <main className="shell">
          {tab === "plain" && <PlainEnglish data={data} />}
          {tab === "overview" && <Overview data={data} frequency={frequency} />}
          {tab === "routes" && <Routes data={data} frequency={frequency} />}
          {tab === "leadtime" && <LeadTime data={data} />}
          {tab === "cpi" && <CpiOverlay data={data} />}
          {tab === "compliance" && <Compliance data={data} />}
          {tab === "methodology" && <Methodology data={data} />}
        </main>

        <footer className="footer">
          <strong>APIx</strong> — a daily airfare price index for India, built to
          mirror CPI methodology so it could augment the MoSPI Consumer Price
          Index Transport division. An independent prototype built for Smart
          India Hackathon 2026; it is not affiliated with, endorsed by, or
          published by MoSPI, DGCA, or any airline named on this site.
          <br />
          Index snapshot generated{" "}
          {new Date(data.provenance.generated_at).toLocaleString("en-IN", {
            dateStyle: "medium",
            timeStyle: "short",
          })}
          . The figures shown are a static export; the index engine, cleaning
          pipeline and compliance log run against PostgreSQL locally.
          {data.provenance.seeded && (
            <>
              {" "}
              <strong className="footer-seeded">
                This deployment shows seeded data, not observed prices.
              </strong>
            </>
          )}
        </footer>
      </div>
    </div>
  );
}
