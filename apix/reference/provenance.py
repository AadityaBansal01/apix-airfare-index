"""Where the numbers came from.

Both the static export and the API report provenance, and they must never
disagree. Two implementations of "is this seeded?" would eventually drift, and
the failure mode is the worst one available to this project: a dashboard
labelled seeded while the API serves the same figures as though observed.

So the computation lives here, once, and is derived from the data rather than
asserted by a caller. If any fare behind the index came from the synthetic
generator, `seeded` is true.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CARRIER_SHARE_PATH = REPO_ROOT / "data/reference/carrier_share.json"

#: Source code used by the synthetic generator. A fare attributed to it is not
#: an observation.
SEED_SOURCE = "seed"


@dataclass(slots=True)
class Provenance:
    generated_at: str
    seeded: bool
    seeded_fare_rows: int
    live_fare_rows: int
    first_index_date: dt.date | None
    last_index_date: dt.date | None
    observable_carriers: list[str] = field(default_factory=list)
    observable_share: float | None = None
    carrier_share: dict[str, float] = field(default_factory=dict)
    weight_reference: dict[str, Any] = field(default_factory=dict)

    @property
    def basis(self) -> str:
        """One line a consumer can print next to a figure."""
        if self.seeded:
            return (
                "SEEDED — figures are computed from synthetic quotes and are "
                "not observations of the market"
            )
        return "observed — figures are computed from collected fare quotes"

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("first_index_date", "last_index_date"):
            if isinstance(d[key], (dt.date, dt.datetime)):
                d[key] = d[key].isoformat()
        return d


def carrier_share_reference() -> dict[str, Any]:
    if not CARRIER_SHARE_PATH.exists():
        return {}
    return json.loads(CARRIER_SHARE_PATH.read_text())


def compute(fetch_one=None) -> Provenance:
    """Read provenance from the database.

    `fetch_one` is injectable so this can be exercised without a database.
    """
    if fetch_one is None:
        from apix import db

        fetch_one = db.fetch_one

    seeded = fetch_one(
        "SELECT count(*) AS n FROM apix.fare f JOIN apix.source s USING (source_id) "
        "WHERE s.code = %s", (SEED_SOURCE,))
    live = fetch_one(
        "SELECT count(*) AS n FROM apix.fare f JOIN apix.source s USING (source_id) "
        "WHERE s.code <> %s", (SEED_SOURCE,))
    span = fetch_one(
        "SELECT min(index_date) AS first, max(index_date) AS last "
        "FROM apix.apix_index WHERE frequency = 'daily'")

    ref = carrier_share_reference()
    return Provenance(
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        seeded=bool(seeded and seeded["n"] > 0),
        seeded_fare_rows=(seeded or {}).get("n", 0) or 0,
        live_fare_rows=(live or {}).get("n", 0) or 0,
        first_index_date=(span or {}).get("first"),
        last_index_date=(span or {}).get("last"),
        observable_carriers=ref.get("observable_carriers", []),
        observable_share=ref.get("observable_share"),
        carrier_share=ref.get("carrier_share", {}),
        weight_reference={
            "start": ref.get("weight_ref_start"),
            "end": ref.get("weight_ref_end"),
            "source": ref.get("source"),
        },
    )
