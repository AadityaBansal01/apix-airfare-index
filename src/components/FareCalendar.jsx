import React, { useEffect, useMemo, useRef } from "react";
import { fmt, rupees } from "../data";

/* The fare-calendar strip: a horizontal row of dates, each with its number,
 * cheapest highlighted.
 *
 * This is the one borrowed element that is a genuinely better fit here than it
 * is on the site it comes from. A booking site's fare calendar shows the price
 * of a FUTURE departure by date, so the traveller can move their trip to a
 * cheaper day. Ours shows the price level on each PAST observation day, which is
 * literally what a daily index is. The control and the data want the same shape.
 *
 * Colour follows the same rule as everywhere else in this project: green is
 * cheaper, red is dearer, and the midpoint is the reference period. A booking
 * site would colour the cheapest day green and everything else grey, which
 * throws away the sign of the movement.
 */
export default function FareCalendar({ rows, selected, onSelect, fareFor,
                                       indexFor, surgeDates, caption }) {
  const ref = useRef(null);
  const active = useRef(null);

  /* The "cheapest" tag has to mark the cheapest thing the tile is SHOWING. With
     rupees on screen that is the lowest observed fare for this sector and
     window, which is not necessarily the lowest index day for the whole basket.
     Labelling one while displaying the other would be a quiet lie. */
  const stats = useMemo(() => {
    if (!rows.length) return null;
    const read = (r) => {
      const f = fareFor ? fareFor(r.index_date) : null;
      return f != null ? f : Number(r.index_value);
    };
    const vals = rows.map(read).filter((v) => Number.isFinite(v));
    return vals.length
      ? { min: Math.min(...vals), max: Math.max(...vals), read }
      : null;
  }, [rows, fareFor]);

  /* Keep the selected day in view when it changes from elsewhere — clicking a
     point on a chart, or the date field in the panel. */
  useEffect(() => {
    if (active.current && ref.current) {
      const el = active.current;
      const box = ref.current;
      const left = el.offsetLeft - box.clientWidth / 2 + el.clientWidth / 2;
      box.scrollTo({ left: Math.max(0, left), behavior: "smooth" });
    }
  }, [selected]);

  if (!rows.length || !stats) return null;

  const scrollBy = (dx) =>
    ref.current?.scrollBy({ left: dx, behavior: "smooth" });

  return (
    <div className="fc">
      <div className="fc-head">
        <div>
          <h2 className="fc-title">
            {fareFor ? "Fare by day" : "Price level by day"}
            {caption && <span className="fc-caption">{caption}</span>}
          </h2>
          <p className="fc-sub">
            {fareFor
              ? "The mean fare actually observed for this sector and booking window on each day, and that same cell's movement against its own July level. Green is cheaper than July, red dearer."
              : "Each day the basket was priced. Green is cheaper than the July reference period, red dearer."}{" "}
            Pick a day to move the whole page to it.
          </p>
        </div>
        <div className="fc-nav">
          <button onClick={() => scrollBy(-360)} aria-label="Earlier">‹</button>
          <button onClick={() => scrollBy(360)} aria-label="Later">›</button>
        </div>
      </div>

      <div className="fc-strip" ref={ref}>
        {rows.map((r) => {
          /* The cell's own index where we have one, so the rupees and the
             percentage on a tile describe the same thing. Falls back to the
             basket headline when this sector/window was not observed that day. */
          const cell = indexFor ? indexFor(r.index_date) : null;
          const v = cell != null ? cell : Number(r.index_value);
          const isSel = r.index_date === selected;
          const isMin = stats.read(r) === stats.min;
          const surge = surgeDates?.has(r.index_date);
          const d = new Date(r.index_date + "T00:00:00Z");
          const tone = v > 100.05 ? "up" : v < 99.95 ? "down" : "flat";
          const fare = fareFor ? fareFor(r.index_date) : null;
          return (
            <button
              key={r.index_date}
              ref={isSel ? active : null}
              className={`fc-day tone-${tone} ${isSel ? "sel" : ""} ${isMin ? "min" : ""}`}
              onClick={() => onSelect(r.index_date)}
              title={`${r.index_date} · index ${fmt(v)}`}
            >
              <span className="fc-dow">
                {d.toLocaleDateString("en-IN", { weekday: "short", timeZone: "UTC" })}
              </span>
              <span className="fc-date num">
                {d.getUTCDate()}{" "}
                {d.toLocaleDateString("en-IN", { month: "short", timeZone: "UTC" })}
              </span>
              <span className="fc-val num">
                {fare != null ? rupees(fare) : fmt(v, 1)}
              </span>
              <span className="fc-delta num">
                {v > 100 ? "+" : ""}{fmt(v - 100, 1)}%
              </span>
              {isMin && <span className="fc-tag">cheapest</span>}
              {surge && !isMin && <span className="fc-tag fc-tag-surge">flagged</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}
