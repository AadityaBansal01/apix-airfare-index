"""Methodology and compliance endpoints.

A statistical series without published methodology is not usable by a statistical
office, so this is a deliverable rather than documentation. The compliance
endpoints serve the same purpose for the collection side: they let a reviewer
verify how the data was gathered without taking anyone's word for it.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from apix.api.deps import get_provenance, get_queries
from apix.api.queries import Queries
from apix.api.schemas import (
    AuditRecord,
    AuditResponse,
    BasketRoute,
    BasketWindow,
    ComplianceResponse,
    ComplianceRow,
    FormulaBlock,
    MethodologyResponse,
    ProvenanceModel,
    RobotsSnapshotRecord,
    SourceRecord,
)

router = APIRouter(prefix="/v1", tags=["methodology"])


ELEMENTARY = FormulaBlock(
    name="Jevons — geometric mean of price relatives",
    latex=r"I_{r,w,t} = 100 \times \prod_{c \in C_{r,w,t}} "
          r"\left( \frac{p_{c,t}}{p_{c,0}} \right)^{1/N_{r,w,t}}",
    plain="index = 100 * geometric_mean(current_price / base_price) over carriers",
    rationale=(
        "The ILO and IMF CPI manuals recommend a geometric mean at the "
        "elementary level for closely substitutable items, and seats on one "
        "route on one day are close substitutes. Jevons is also transitive, so "
        "the index does not depend on which day is chosen as the base, and it "
        "renormalises naturally over whichever carriers were observed, so a "
        "missing carrier needs no imputation."
    ),
)

UPPER = FormulaBlock(
    name="Young — weighted arithmetic mean, a modified Laspeyres",
    latex=r"\mathrm{APIx}_t = \frac{\sum_{r,w} \omega_{r,w} \, I_{r,w,t}}"
          r"{\sum_{r,w} \omega_{r,w}}",
    plain="APIx = sum(weight * cell_index) / sum(weight) over observed cells",
    rationale=(
        "Weights come from a period earlier than the price reference period, "
        "which makes this a Young index rather than a strict Laspeyres. This is "
        "what national CPIs actually do, since expenditure weights are never "
        "available for the current period. The denominator runs over observed "
        "cells only, so a missing cell lowers the coverage ratio instead of "
        "silently biasing the level."
    ),
)

LIMITATIONS = [
    "Coverage is roughly 8% of Indian domestic passenger traffic. The carriers "
    "collectable within this project's ethical limits are the two smallest. "
    "IndiGo disallows its booking paths in robots.txt, Air India sits behind bot "
    "management, and Air India Express also disallows. This is the binding "
    "constraint on the index and closing it requires a licensed feed or a "
    "data-sharing arrangement, not better scraping.",

    "Carrier traffic shares are national, not route-level. DGCA publishes "
    "carrier traffic and city-pair traffic separately and never crossed, so a "
    "carrier's national share stands in for its share on each route. On trunk "
    "routes this flatters the coverage figure, so treat observable_share as an "
    "upper bound.",

    "The price concept is the lowest available total fare per carrier, which is "
    "what a traveller faces. It is not an average over booking classes, and it "
    "is not revenue-weighted, so it will not match an airline's realised yield.",

    "No revision policy is defined. A published statistic needs a stated rule "
    "for when and how figures are revised; this has none yet.",

    "The index measures posted fares, not transacted prices. Discounts applied "
    "at payment, corporate fares and loyalty redemptions are invisible to it.",
]


@router.get("/methodology", response_model=MethodologyResponse,
            summary="How the index is constructed")
def methodology(
    q: Queries = Depends(get_queries),
    prov: ProvenanceModel = Depends(get_provenance),
) -> MethodologyResponse:
    basket = q.basket()
    windows = q.windows()
    ref = basket[0] if basket else {}
    return MethodologyResponse(
        price_concept=(
            "Lowest available total fare per route, advance-purchase window and "
            "carrier, on a one-way non-stop economy itinerary. Total fare means "
            "base fare plus taxes plus User Development Fee plus convenience "
            "charge, which is the amount a traveller actually pays."
        ),
        elementary_formula=ELEMENTARY,
        upper_level_formula=UPPER,
        temporal_aggregation=(
            "Weekly and monthly values are the arithmetic mean of the daily "
            "values in the period. With fixed weights this is identical to "
            "aggregating period-mean elementary indices, so the three "
            "frequencies are mutually consistent by construction rather than by "
            "convention."
        ),
        missing_data_policy={
            "carrier_absent":
                "Jevons runs over the carriers observed. No imputation needed, "
                "because the geometric mean of relatives renormalises itself.",
            "cell_absent_1_to_2_days":
                "The last known elementary value is carried forward and flagged "
                "as imputed.",
            "cell_absent_3_or_more_days":
                "The cell is dropped and remaining weights renormalised. "
                "coverage_ratio falls below 1 to record that this happened.",
            "sold_out":
                "Recorded but excluded. A sold-out flight has no purchasable "
                "price, so treating it as an observation would be wrong.",
        },
        outlier_policy=(
            "Modified z-score on log fares, using the median and median absolute "
            "deviation, flagged above 3.5. Comparison groups never cross a scrape "
            "date, so a genuine price surge cannot be flagged as an outlier. The "
            "detector targets parse errors, not expensive flights. Flagged rows "
            "are retained in the database with their flag and excluded at query "
            "time, so every exclusion is auditable and reversible."
        ),
        reference_periods={
            "price_reference": (
                "Base = 100. Base prices are the geometric mean of daily prices "
                "over the price reference period."
            ),
            "weight_reference_start": ref.get("weight_ref_start"),
            "weight_reference_end": ref.get("weight_ref_end"),
            "weight_source": ref.get("source_url"),
        },
        basket_routes=[BasketRoute(**r) for r in basket],
        basket_windows=[BasketWindow(**w) for w in windows],
        limitations=LIMITATIONS,
        provenance=prov,
    )


COMPLIANCE_STATEMENT = (
    "Every candidate source was checked against its live robots.txt, its terms "
    "of use and its bot-management stack before any request was made. Sources "
    "that could only be collected by evading access controls are excluded, and "
    "the reason is recorded rather than the source quietly dropped. Collection "
    "runs at a deliberate rate limit well below what the sites permit. Every "
    "outbound request writes an audit row, including requests that were refused, "
    "and a database constraint rejects any row claiming a successful fetch of a "
    "URL that robots.txt disallowed, so compliance is enforced by the schema "
    "rather than only by well-behaved code."
)


@router.get("/compliance", response_model=ComplianceResponse,
            summary="Collection ethics evidence")
def compliance(q: Queries = Depends(get_queries)) -> ComplianceResponse:
    return ComplianceResponse(
        statement=COMPLIANCE_STATEMENT,
        sources=[SourceRecord(**s) for s in q.sources()],
        request_summary=[ComplianceRow(**r) for r in q.compliance()],
        robots_snapshots=[RobotsSnapshotRecord(**r) for r in q.robots_snapshots()],
    )


@router.get("/compliance/requests", response_model=AuditResponse,
            summary="Raw request audit log")
def audit(
    limit: int = Query(200, ge=1, le=2000),
    source: str | None = Query(None, description="Filter to one source code."),
    q: Queries = Depends(get_queries),
) -> AuditResponse:
    """The request log itself, newest first.

    A refused request is recorded with `robots_allowed=false` and an outcome of
    `blocked_by_robots` or `blocked_by_intent`. Those rows are the evidence that
    refusals were enforced rather than merely intended, so they are served
    alongside successful fetches rather than filtered out.
    """
    rows = q.audit(limit=limit, source=source)
    return AuditResponse(
        count=len(rows),
        note=("delay_applied_s is the interval since the previous request to the "
              "same origin. A refused request correctly waits zero seconds and a "
              "first request has no predecessor, so both record 0."),
        requests=[AuditRecord(**r) for r in rows],
    )
