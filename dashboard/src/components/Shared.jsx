import React from "react";
import { fmt, pct } from "../data";

export const Card = ({ title, note, right, children }) => (
  <div className="card">
    {(title || right) && (
      <div className="card-head">
        {title && <h2 className="card-title">{title}</h2>}
        {right && <div style={{ marginLeft: "auto" }}>{right}</div>}
      </div>
    )}
    {note && <p className="card-note">{note}</p>}
    {children}
  </div>
);

export const Badge = ({ kind = "grey", children }) => (
  <span className={`badge badge-${kind}`}>{children}</span>
);

export const Stat = ({ label, value, sub }) => (
  <div>
    <div className="stat-label">{label}</div>
    <div className="stat-value num">{value}</div>
    {sub && <div className="stat-sub">{sub}</div>}
  </div>
);

export const Delta = ({ value, digits = 2 }) => {
  if (value === null || value === undefined) return <span className="flat">–</span>;
  const cls = value > 0.0001 ? "up" : value < -0.0001 ? "down" : "flat";
  const arrow = value > 0.0001 ? "▲" : value < -0.0001 ? "▼" : "■";
  return (
    <span className={`${cls} num`}>
      {arrow} {value > 0 ? "+" : ""}
      {(value * 100).toFixed(digits)}%
    </span>
  );
};

/**
 * Coverage is shown wherever an index value is shown.
 *
 * A number computed on part of the basket must never appear as though it rested
 * on all of it, so this renders next to the value rather than in a footnote.
 */
export const Coverage = ({ ratio }) => {
  if (ratio === null || ratio === undefined) return null;
  const full = ratio >= 0.9999;
  return (
    <Badge kind={full ? "ok" : "warn"}>
      {full ? "full basket" : `${pct(ratio, 0)} of basket`}
    </Badge>
  );
};

/** Persistent, non-dismissible provenance notice. */
export const ProvenanceBanner = ({ provenance }) => {
  if (!provenance?.seeded) return null;
  return (
    <div className="provenance-banner">
      <span style={{ fontSize: 18, lineHeight: 1 }}>⚗️</span>
      <div className="provenance-text">
        <strong>SEEDED DATA — NOT A MEASUREMENT.</strong> Every figure on this
        page is computed from {fmt(provenance.seeded_fare_rows, 0)} synthetic
        fare quotes, not from observed prices. The generator, the cleaning
        pipeline and the index engine are the same code paths a live collection
        would use, so this demonstrates that the system works; it does not
        demonstrate what Indian airfares did. Live collection at the rate limits
        this project commits to yields roughly 200 quotes a day, so a comparable
        real series takes about four weeks to accumulate.
      </div>
    </div>
  );
};

/** The coverage limitation, stated wherever the index is presented. */
export const CoverageBanner = ({ provenance }) => {
  const share = provenance?.observable_share;
  if (share === null || share === undefined) return null;
  return (
    <div className="coverage-banner">
      <strong>Coverage: {pct(share)} of domestic passenger traffic.</strong>{" "}
      Only {(provenance.observable_carriers || []).join(" and ")} can be
      collected within the ethical limits this project sets. IndiGo (63.9% of
      traffic) disallows its booking paths in robots.txt; Air India (15.2%) sits
      behind bot management that could only be passed by evasion; Air India
      Express (11.5%) also disallows. Excluding them is the correct call and it
      is the single biggest limitation of this index. Closing the gap needs a
      licensed GDS feed or a data-sharing arrangement with DGCA, not better
      scraping.
    </div>
  );
};

export const Formula = ({ children }) => <div className="formula">{children}</div>;
