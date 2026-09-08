"""The compliance endpoint: ethical-scraping evidence, served live.

A judge or a regulator should be able to check the scraping posture themselves
rather than take a slide's word for it. This endpoint returns, per source, how
many requests were made, how many were refused before being sent, and the
minimum interval ever applied, plus every source excluded from collection with
the written reason.

The exclusion list is the important half. It shows the sources we chose not to
scrape and why, which is the part a compliance review actually cares about.
"""
from __future__ import annotations

from fastapi import APIRouter

from apix.api.deps import read_all
from apix.api.schemas import ComplianceResponse, ComplianceRow

router = APIRouter(prefix="/v1", tags=["compliance"])


@router.get("/compliance", response_model=ComplianceResponse,
            summary="Live scraping-compliance evidence")
def compliance() -> ComplianceResponse:
    rows = read_all("SELECT * FROM apix.v_compliance_summary ORDER BY source")

    # Two distinct reasons a source is not being collected, kept apart on
    # purpose. Mixing "we chose not to" with "we have not built it yet" would
    # overstate the compliance posture in one direction and understate the
    # engineering progress in the other. `__robots__` is an internal
    # bookkeeping row for robots.txt retrieval and belongs in neither list.
    excluded = read_all(
        "SELECT code, display_name, kind, tier, audit_verdict, exclusion_reason "
        "FROM apix.source WHERE NOT enabled AND tier >= 3 "
        "AND code NOT IN ('seed', '__robots__') ORDER BY tier, code")

    pending = read_all(
        "SELECT code, display_name, kind, tier, audit_verdict, exclusion_reason "
        "FROM apix.source WHERE NOT enabled AND tier < 3 "
        "AND code NOT IN ('seed', '__robots__') ORDER BY code")

    return ComplianceResponse(
        sources=[ComplianceRow(**r) for r in rows],
        excluded_on_compliance_grounds=[dict(r) for r in excluded],
        pending_implementation=[dict(r) for r in pending],
        note=(
            "Delay statistics cover requests actually sent. A refused request "
            "correctly waits zero seconds, and the first request to an origin "
            "has no predecessor to be spaced from, so both are excluded from "
            "min_delay_s. The pseudo-source '__robots__' records robots.txt "
            "retrievals, which are exempt from robots checking by RFC 9309 but "
            "are still rate limited and logged. The 'seed' source is synthetic "
            "demo data and made no network requests."),
    )
