import React, { useEffect, useRef, useState } from "react";
import { longDate, rupees, shortDate } from "../data";

/* The search panel, in the position and shape a traveller expects it.
 *
 * It is a deliberate borrowing: from/to boxes with a circular swap between
 * them, a date field, a passenger-style selector, and a large primary button on
 * the right. Anyone who has bought a flight can drive it without instructions.
 *
 * WHAT IT DOES NOT DO
 * -------------------
 * It sells nothing. There is no cart, no fare lock, no "3 seats left". Every
 * control changes what the page is SHOWING; none of them starts a transaction.
 * The primary button reads "Show fares", not "Search", because there is nothing
 * to search — the whole series is already loaded and the button scrolls to it.
 *
 * The one place the borrowing is corrected rather than followed: a booking site
 * asks for a departure date in the future. This asks for an OBSERVATION date in
 * the past, because an index is a record of what prices were, not an offer of
 * what they will be. The field is labelled accordingly and cannot be set past
 * the last collection day.
 */

const CITY = {
  DEL: "New Delhi", BOM: "Mumbai", BLR: "Bengaluru",
  CCU: "Kolkata", HYD: "Hyderabad", MAA: "Chennai",
};
export const cityName = (code) => CITY[code] || code;

const AIRPORT = {
  DEL: "Indira Gandhi Intl", BOM: "Chhatrapati Shivaji Intl",
  BLR: "Kempegowda Intl", CCU: "Netaji Subhas Chandra Bose Intl",
  HYD: "Rajiv Gandhi Intl", MAA: "Chennai Intl",
};

/** A from/to picker. Lists only airports that form a basket route with the
 *  other end, so the panel can never be put into a state the index has no data
 *  for — the booking-site equivalent of not offering a route nobody flies. */
function Picker({ label, code, options, onPick, align = "left" }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const close = (e) => { if (!ref.current?.contains(e.target)) setOpen(false); };
    const esc = (e) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  return (
    <div className={`sp-field sp-field-picker ${open ? "open" : ""}`} ref={ref}>
      <button className="sp-field-btn" onClick={() => setOpen((v) => !v)}
              aria-expanded={open} aria-haspopup="listbox">
        <span className="sp-label">{label}</span>
        <span className="sp-code num">{code}</span>
        <span className="sp-city">{cityName(code)}</span>
        <span className="sp-airport">{AIRPORT[code] || ""}</span>
      </button>
      {open && (
        <ul className={`sp-menu sp-menu-${align}`} role="listbox">
          {options.map((o) => (
            <li key={o}>
              <button className={o === code ? "active" : ""} role="option"
                      aria-selected={o === code}
                      onClick={() => { onPick(o); setOpen(false); }}>
                <span className="sp-menu-code num">{o}</span>
                <span>
                  <strong>{cityName(o)}</strong>
                  <small>{AIRPORT[o]}</small>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function SearchPanel({
  routes, origin, destination, onRoute,
  windows, window: win, onWindow,
  dates, date, onDate,
  frequency, onFrequency, frequencyEnabled,
  fareForSelection, onShow,
}) {
  /* Which airports can sit at each end, given the basket. */
  const originsFor = (dest) => [...new Set(
    routes.filter((r) => r.origin === dest || r.destination === dest)
          .map((r) => (r.origin === dest ? r.destination : r.origin)))].sort();
  const allEnds = [...new Set(routes.flatMap((r) => [r.origin, r.destination]))].sort();

  const swap = () => onRoute(destination, origin);

  return (
    <div className="sp">
      {/* Frequency, in the position a booking site puts one-way / round-trip.
          Same control grammar, different question. */}
      <div className="sp-top">
        <div className="sp-trip">
          {["daily", "weekly", "monthly"].map((f) => (
            <label key={f} className={`sp-radio ${frequency === f ? "on" : ""} ${
              frequencyEnabled ? "" : "disabled"}`}>
              <input type="radio" name="freq" checked={frequency === f}
                     disabled={!frequencyEnabled}
                     onChange={() => onFrequency(f)} />
              <span />
              {f[0].toUpperCase() + f.slice(1)}
            </label>
          ))}
        </div>
        <div className="sp-top-note">
          {frequencyEnabled
            ? "How often the index is aggregated"
            : "This view is daily only"}
        </div>
      </div>

      <div className="sp-row">
        {/* From, To and the swap live in their own relatively-positioned pair so
            the swap can sit exactly on the seam between them. Positioning it
            against the whole row needs a calc() over `fr` units, which is not
            valid CSS and silently collapsed it onto the left edge. */}
        <div className="sp-pair">
          <Picker label="From" code={origin}
                  options={allEnds.filter((a) => a !== destination)}
                  onPick={(o) => onRoute(o, destination)} />

          <button className="sp-swap" onClick={swap} title="Swap"
                  aria-label={`Swap ${origin} and ${destination}`}>
            <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
              <path d="M7 7h11l-3-3M17 17H6l3 3" fill="none" stroke="currentColor"
                    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>

          <Picker label="To" code={destination} align="right"
                  options={originsFor(origin).length ? originsFor(origin)
                                                     : allEnds.filter((a) => a !== origin)}
                  onPick={(d) => onRoute(origin, d)} />
        </div>

        {/* An OBSERVATION date, not a departure date. See the module note. */}
        <div className="sp-field">
          <label className="sp-field-btn" htmlFor="sp-date">
            <span className="sp-label">Priced on</span>
            <span className="sp-date-day num">
              {date ? new Date(date + "T00:00:00Z").getUTCDate() : "–"}
              <em>{date ? new Date(date + "T00:00:00Z").toLocaleDateString("en-IN",
                    { month: "short", timeZone: "UTC" }) : ""}</em>
            </span>
            <span className="sp-city">
              {date ? new Date(date + "T00:00:00Z").toLocaleDateString("en-IN",
                    { weekday: "long", timeZone: "UTC" }) : ""}
            </span>
            <select id="sp-date" className="sp-date-select" value={date || ""}
                    onChange={(e) => onDate(e.target.value)}>
              {dates.map((d) => <option key={d} value={d}>{longDate(d)}</option>)}
            </select>
          </label>
        </div>

        {/* Booking window, where a booking site puts travellers and cabin
            class. It is the closest true analogue: both say "what kind of
            purchase are we pricing". */}
        <div className="sp-field">
          <label className="sp-field-btn" htmlFor="sp-window">
            <span className="sp-label">Booked ahead</span>
            <span className="sp-win num">T+{win}</span>
            <span className="sp-city">
              {win === 1 ? "1 day before" : `${win} days before`} departure
            </span>
            <select id="sp-window" className="sp-date-select" value={win}
                    onChange={(e) => onWindow(Number(e.target.value))}>
              {windows.map((w) => (
                <option key={w.window_days} value={w.window_days}>
                  T+{w.window_days} — booked {w.window_days} day
                  {w.window_days === 1 ? "" : "s"} ahead
                </option>
              ))}
            </select>
          </label>
        </div>

        <button className="sp-cta" onClick={onShow}>
          Show fares
        </button>
      </div>

      {fareForSelection != null && (
        <div className="sp-readout">
          <span className="sp-readout-dot" />
          Typical fare for <strong>{origin}–{destination}</strong> booked{" "}
          <strong>T+{win}</strong>, observed {shortDate(date)}:{" "}
          <strong className="num">{rupees(fareForSelection)}</strong>
          {/* Swapping the ends does not change the figure, and a control that
              appears to do nothing reads as broken. It isn't: DGCA publishes
              city-pair traffic with both directions combined, so the index
              weights a city PAIR, not a direction. Saying so turns a confusing
              no-op into a methodology note. See METHODOLOGY.md section 3.4. */}
          <span className="sp-readout-hint">
            both directions are one sector for weighting
          </span>
        </div>
      )}
    </div>
  );
}
