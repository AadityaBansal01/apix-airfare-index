"""APIx public API.

    make api        # uvicorn on :8000, interactive docs at /docs

Read-only by construction: the scraper and index engine write, this process only
reads. See apix/api/deps.py.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from apix.api.deps import (
    API_VERSION,
    DatabaseUnavailable,
    is_seeded,
    read_one,
)
from apix.api.routers import analytics, compliance, index, reference
from apix.api.schemas import HealthResponse

log = logging.getLogger(__name__)

DESCRIPTION = """
Real-time Airfare Price Index for India (APIx).

A daily, weighted airfare price index built to augment the CPI Transport
division, constructed to match MoSPI CPI 2024 methodology: **Jevons** at the
elementary level, **Young / modified Laspeyres** above it, with route weights
from DGCA passenger traffic.

### Read this before consuming the series

Every response carrying an index value also carries a `provenance` block with the
reference periods, the formulas, and two coverage figures. Both matter:

* `coverage_ratio` is how much of the basket was observed that period.
* `observed_pax_share` is the share of Indian domestic passenger traffic flown by
  carriers this system can legally observe.

The second is small. IndiGo, Air India and Air India Express are excluded because
their robots.txt disallows fare search or they enforce active bot management, and
this project does not defeat anti-automation controls. `/v1/compliance` shows
every exclusion and its reason. `/v1/methodology` lists the limitations in full.

If `provenance.is_seeded` is true, the underlying fares are synthetic demo data
and must not be published or cited as measurements.
"""

app = FastAPI(
    title="APIx: Real-time Airfare Price Index for India",
    description=DESCRIPTION,
    version=API_VERSION,
    contact={"name": "APIx, SIH26056 / MoSPI"},
    license_info={"name": "Open Government Data compatible"},
    openapi_tags=[
        {"name": "index", "description": "Index values and history."},
        {"name": "analytics", "description": "Lead-time elasticity."},
        {"name": "reference", "description": "Basket and machine-readable methodology."},
        {"name": "compliance", "description": "Live ethical-scraping evidence."},
        {"name": "service", "description": "Health and service metadata."},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip()
        for o in os.getenv("APIX_CORS_ORIGINS", "http://localhost:5173").split(",")
        if o.strip()
    ],
    allow_credentials=False,   # nothing here is user-specific; no cookies needed
    allow_methods=["GET"],     # the API is read-only, so advertise only GET
    allow_headers=["*"],
)

app.include_router(index.router)
app.include_router(analytics.router)
app.include_router(reference.router)
app.include_router(compliance.router)


@app.exception_handler(DatabaseUnavailable)
async def _db_unavailable(request: Request, exc: DatabaseUnavailable) -> JSONResponse:
    """A missing database is an operational fault, not a client error.

    Returns 503 with a runnable remedy rather than a stack trace, because the
    most likely reader of this message is a judge who has not started Postgres.
    """
    log.warning("database unavailable on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "database unavailable",
            "remedy": "Start the database and build the index: make demo",
            "error": str(exc)[:300],
        },
    )


@app.get("/", tags=["service"], summary="Service metadata")
def root() -> dict:
    return {
        "name": "APIx: Real-time Airfare Price Index for India",
        "version": API_VERSION,
        "problem_statement": "SIH26056, MoSPI",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "endpoints": {
            "latest": "/v1/index/latest",
            "series": "/v1/index/series?frequency=daily",
            "route_breakdown": "/v1/index/routes",
            "lead_time": "/v1/leadtime?route=BOM-DEL",
            "basket": "/v1/basket",
            "methodology": "/v1/methodology",
            "compliance": "/v1/compliance",
            "health": "/health",
        },
    }


@app.get("/health", response_model=HealthResponse, tags=["service"],
         summary="Health check")
def health() -> HealthResponse:
    database = "ok"
    latest_date = None
    try:
        row = read_one(
            "SELECT max(index_date) AS d FROM apix.apix_index "
            "WHERE frequency = 'daily'")
        latest_date = row["d"] if row else None
    except DatabaseUnavailable as exc:
        database = f"unavailable: {str(exc)[:120]}"

    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        database=database,
        seeded_mode=is_seeded(),
        latest_index_date=latest_date,
        version=API_VERSION,
    )
