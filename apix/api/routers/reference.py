"""Reference endpoints: the basket, and the methodology metadata endpoint.

The methodology endpoint is what makes this API consumable by a statistical
office rather than merely readable. It states the formulas, the reference
periods, the missing-data policy and the limitations in machine-readable form, so
a consumer can check that what they are ingesting is what they think it is,
without reading a PDF.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter

from apix.api.deps import API_VERSION, basket_config, build_provenance, read_all
from apix.api.schemas import BasketResponse, MethodologyResponse, RouteDefinition

router = APIRouter(prefix="/v1", tags=["reference"])


@router.get("/basket", response_model=BasketResponse,
            summary="Routes, weights and advance-purchase windows")
def basket() -> BasketResponse:
    cfg = basket_config()
    wref = cfg["weight_reference"]

    rows = read_all(
        """
        SELECT r.route_id, r.route_code, r.origin, r.destination, r.in_basket,
               ao.city_name AS origin_city, ad.city_name AS destination_city,
               rw.passengers, rw.weight
        FROM apix.route r
        JOIN apix.airport ao ON ao.iata = r.origin
        JOIN apix.airport ad ON ad.iata = r.destination
        LEFT JOIN apix.route_weight rw
               ON rw.route_id = r.route_id AND rw.weight_ref_start = %s
        ORDER BY rw.weight DESC NULLS LAST
        """,
        (wref["start"],))

    routes = [RouteDefinition(**r) for r in rows]
    total = sum((r.weight or Decimal(0)) for r in routes)
    return BasketResponse(
        routes=routes,
        advance_windows=cfg["advance_windows"],
        weight_reference=f"{wref['start']} to {wref['end']}",
        total_weight=total,
    )


@router.get("/methodology", response_model=MethodologyResponse,
            summary="Machine-readable methodology and limitations")
def methodology() -> MethodologyResponse:
    cfg = basket_config()
    ref = cfg["index_reference"]
    wref = cfg["weight_reference"]
    prov = build_provenance()

    return MethodologyResponse(
        index_name="APIx, Real-time Airfare Price Index for India",
        version=API_VERSION,
        elementary_formula="Jevons, geometric mean of price relatives",
        elementary_formula_latex=(
            r"I^{t}_{r,w} = 100 \prod_{c \in C_{r,w}} "
            r"\left( \frac{P^{t}_{r,w,c}}{\bar{P}^{0}_{r,w,c}} "
            r"\right)^{1/|C_{r,w}|}"),
        upper_formula="Young, modified Laspeyres, fixed weights",
        upper_formula_latex=(
            r"\mathrm{APIx}_t = \frac{\sum_{r}\sum_{w} \omega_{r,w} "
            r"I^{t}_{r,w}}{\sum_{r}\sum_{w} \omega_{r,w}}"),
        elementary_aggregate=(
            "One (route, advance-purchase window) pair. Carriers within a cell "
            "are the analogue of CPI markets, which is the level at which MoSPI "
            "applies Jevons."),
        price_concept=(
            "Lowest available total economy fare on a non-stop service, "
            "inclusive of taxes, User Development Fee and statutory charges. "
            "This is what a traveller faces, not average realised revenue."),
        index_reference_period=(
            f"{ref['price_ref_start']} to {ref['price_ref_end']} = 100"),
        weight_reference_period=f"{wref['start']} to {wref['end']}",
        frequencies=["daily", "weekly", "monthly"],
        cpi_alignment={
            "series": "MoSPI CPI 2024 series, base 2024=100",
            "classification": "COICOP 2018, Division 07 Transport",
            "division_07_weight_combined": 8.796,
            "elementary_formula_matches": True,
            "upper_formula_matches": True,
            "note": (
                "MoSPI already collects airfares from online platforms monthly. "
                "APIx adds frequency and coverage of the advance-purchase "
                "surface, not the shift from manual to online collection."),
        },
        missing_data_policy={
            "carrier_missing_from_cell": (
                "Jevons runs over observed carriers only; no imputation needed."),
            "cell_missing_under_3_days": (
                "Carry forward the last elementary value, flagged is_imputed."),
            "cell_missing_3_days_or_more": (
                "Drop the cell and renormalise; coverage_ratio falls below 1."),
            "sold_out": (
                "Excluded from the index and recorded separately. A sold-out "
                "fare is not a price."),
        },
        limitations=[
            "Carrier-restricted sample. IndiGo, Air India and Air India Express "
            "are excluded on robots.txt and bot-management grounds, so the index "
            "observes a minority of domestic passenger traffic. Every response "
            "carries observed_pax_share.",
            "Advance-purchase window weights are an equal-weight assumption. No "
            "public booking-lead-time distribution for India exists.",
            "Lowest-available fare is not average realised revenue, so APIx "
            "should correlate with DGCA average fares in direction but must not "
            "be expected to match them in level.",
            "Non-stop economy only. Connecting itineraries and premium cabins "
            "are excluded to hold the product definition constant.",
            "observed_pax_share uses national carrier shares as a proxy for "
            "route-level shares, which DGCA does not publish. Treat it as an "
            "upper bound on coverage.",
            "APIx is a price index and asserts no causal claim. Anomaly flags "
            "mark statistical outliers against a rolling baseline; attributing "
            "one to a festival or fuel move is a labelled hypothesis.",
        ]
        + (["Underlying fares are SYNTHETIC seeded data, not observations. "
            "These values must not be published or cited as measurements."]
           if prov.is_seeded else []),
        documentation={
            "methodology": "docs/METHODOLOGY.md",
            "source_audit": "docs/SOURCE_AUDIT.md",
            "deployment": "docs/DEPLOYMENT.md",
            "openapi": "/openapi.json",
            "interactive_docs": "/docs",
        },
    )
