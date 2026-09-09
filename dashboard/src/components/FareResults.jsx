import React, { useMemo, useState } from "react";
import { fmt, pct, rupees } from "../data";
import { cityName } from "./SearchPanel";

/* Sectors rendered as flight-search results.
 *
 * A booking result row reads left to right: who flies it, where it goes, how
 * long it takes, what it costs, and a button. That layout is doing real work —
 * it puts the identity on the left and the money on the right, so a column of
 * them can be scanned by price alone. An index breakdown wants exactly the same
 * scan, so the row is reused rather than reinvented as a table.
 *
 * The substitutions, each deliberate:
 *   airline logo      -> sector code, because the sector is the unit here
 *   depart -> arrive  -> origin -> destination with the observed carriers
 *   duration          -> basket weight, the row's share of the headline
 *   price             -> the observed mean fare at the selected booking window
 *   "Book"            -> "Details", which expands. Nothing is for sale.
 *
 * The expanded panel carries the index value, the contribution and the coverage,
 * so the statistical content is one click away rather than removed.
 */

const CARRIERS = { QP: "Akasa Air", SG: "SpiceJet" };

function Row({ r, fare, fit, onPick, selected }) {
  const [open, setOpen] = useState(false);
  const idx = Number(r.index_value);
  const tone = idx > 100.05 ? "up" : idx < 99.95 ? "down" : "flat";

  return (
    <div className={`fr-row ${selected ? "sel" : ""} ${open ? "open" : ""}`}>
      <div className="fr-main">
        <div className="fr-id">
          <div className="fr-sector num">{r.route_code}</div>
          <div className="fr-carriers">
            {Object.keys(CARRIERS).map((c) => (
              <span className="fr-chip" key={c} title={CARRIERS[c]}>{c}</span>
            ))}
          </div>
        </div>

        <div className="fr-path">
          <div className="fr-end">
            <div className="fr-code num">{r.origin}</div>
            <div className="fr-city">{cityName(r.origin)}</div>
          </div>
          <div className="fr-line" aria-hidden="true">
            <span className="fr-dot" />
            <span className="fr-rule" />
            <svg viewBox="0 0 24 24" width="15" height="15" className="fr-plane">
              <path d="M21 16v-2l-8-5V3.5a1.5 1.5 0 0 0-3 0V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5z"
                    fill="currentColor" />
            </svg>
            <span className="fr-rule" />
            <span className="fr-dot" />
          </div>
          <div className="fr-end">
            <div className="fr-code num">{r.destination}</div>
            <div className="fr-city">{cityName(r.destination)}</div>
          </div>
        </div>

        <div className="fr-weight">
          <div className="fr-weight-val num">{pct(r.weight, 1)}</div>
          <div className="fr-weight-lbl">of the basket</div>
          <div className="fr-weight-bar">
            <span style={{ width: `${Math.min(100, r.weight * 100 * 3.6)}%` }} />
          </div>
        </div>

        <div className="fr-price">
          {fare != null && <div className="fr-fare num">{rupees(fare)}</div>}
          <div className={`fr-idx num tone-${tone}`}>
            {fmt(idx)} <small>index</small>
          </div>
          <div className={`fr-move num tone-${tone}`}>
            {idx > 100 ? "▲ +" : idx < 100 ? "▼ " : "■ "}{fmt(idx - 100, 2)}% vs base
          </div>
        </div>

        <div className="fr-cta">
          <button className="fr-btn" onClick={() => setOpen((v) => !v)}
                  aria-expanded={open}>
            {open ? "Hide" : "Details"}
          </button>
          <button className="fr-link" onClick={() => onPick(r.origin, r.destination)}>
            {selected ? "Selected" : "Focus"}
          </button>
        </div>
      </div>

      {open && (
        <div className="fr-detail">
          <dl>
            <div><dt>Index value</dt><dd className="num">{fmt(idx)}</dd></div>
            <div><dt>Basket weight</dt><dd className="num">{pct(r.weight, 4)}</dd></div>
            <div>
              <dt>Contribution to headline</dt>
              <dd className="num">{fmt(r.contribution)}</dd>
            </div>
            {fare != null && (
              <div><dt>Mean fare at this window</dt>
                   <dd className="num">{rupees(fare)}</dd></div>
            )}
            {fit?.fitted && (
              <>
                <div>
                  <dt>Last-minute premium</dt>
                  <dd className="num">+{fmt(fit.amplitude * 100, 0)}%</dd>
                </div>
                <div>
                  <dt>Premium half-life</dt>
                  <dd className="num">{fmt(fit.decay_days, 1)} days</dd>
                </div>
              </>
            )}
          </dl>
          <p className="fr-detail-note">
            The index value is a Jevons geometric mean of this sector's fare
            relatives against its own July base, across every carrier and booking
            window observed. The contribution is that value times the weight —
            add the eight of them and you get the headline.
          </p>
        </div>
      )}
    </div>
  );
}

export default function FareResults({
  rows, fares, fits, window: win, onPick, selectedRoute, headline,
}) {
  const [sort, setSort] = useState("weight");

  const fareFor = useMemo(() => {
    const m = new Map();
    for (const f of fares || []) {
      if (f.window_days === win) m.set(f.route_code, Number(f.mean_fare));
    }
    return (code) => (m.has(code) ? m.get(code) : null);
  }, [fares, win]);

  const sorted = useMemo(() => {
    const out = [...rows];
    const fare = (r) => fareFor(r.route_code) ?? Infinity;
    ({
      weight: () => out.sort((a, b) => b.weight - a.weight),
      cheapest: () => out.sort((a, b) => fare(a) - fare(b)),
      dearest: () => out.sort((a, b) => fare(b) - fare(a)),
      rising: () => out.sort((a, b) => b.index_value - a.index_value),
      falling: () => out.sort((a, b) => a.index_value - b.index_value),
    }[sort] || (() => {}))();
    return out;
  }, [rows, sort, fareFor]);

  if (!rows.length) return null;

  return (
    <div className="fr">
      <div className="fr-head">
        <div>
          <h2 className="fr-title">
            {rows.length} sectors priced
            {headline != null && (
              <span className="fr-headline num"> · headline {fmt(headline)}</span>
            )}
          </h2>
          <p className="fr-sub">
            Fares shown are the mean observed total fare booked{" "}
            <strong>T+{win}</strong>, inclusive of taxes and statutory charges.
          </p>
        </div>
        <div className="fr-sort">
          <span>Sort by</span>
          {[["weight", "Basket weight"], ["cheapest", "Cheapest"],
            ["dearest", "Dearest"], ["rising", "Most above base"],
            ["falling", "Most below base"]].map(([k, label]) => (
            <button key={k} className={sort === k ? "on" : ""}
                    onClick={() => setSort(k)}>{label}</button>
          ))}
        </div>
      </div>

      <div className="fr-list">
        {sorted.map((r) => (
          <Row key={r.route_code} r={r}
               fare={fareFor(r.route_code)}
               fit={(fits || []).find((f) => f.route_code === r.route_code)}
               onPick={onPick}
               selected={selectedRoute === r.route_code} />
        ))}
      </div>
    </div>
  );
}
