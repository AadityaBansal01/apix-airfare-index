"""Response models for the public API.

DESIGN RULE: no index value travels without its provenance.

Every response carrying a number also carries the base period it is measured
against, the weight period, the formula, how much of the basket was observed,
what share of passenger traffic that represents, and whether the underlying data
is seeded. A consumer at the NSO or RBI must never receive a bare figure they
could mistake for a complete, live market measurement.

That is why `Provenance` is embedded in the payload rather than offered on a
separate metadata endpoint. Metadata that must be fetched separately is metadata
that gets dropped somewhere downstream.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Frequency = Literal["daily", "weekly", "monthly"]


class Provenance(BaseModel):
    """Attached to every payload containing an index value."""

    formula_elementary: str = Field(
        "jevons",
        description="Elementary aggregate formula. Matches MoSPI CPI 2024.")
    formula_upper: str = Field(
        "young_modified_laspeyres",
        description="Upper-level formula. Matches MoSPI CPI 2024.")
    index_reference: str = Field(
        ...,
        description="Price reference period. The index equals 100 here.")
    weight_reference: str = Field(
        ...,
        description="Period whose passenger traffic supplies the route weights.")
    weight_source: str = Field(
        "DGCA monthly domestic city-pair statistics (ODbL-1.0)",
        description="Provenance of the route weights.")
    is_seeded: bool = Field(
        ...,
        description=(
            "True when the underlying fares are synthetic demo data rather than "
            "collected observations. Synthetic values must never be published or "
            "cited as measurements."))
    coverage_note: str = Field(
        ...,
        description="What fraction of the basket and of the market this reflects.")


class IndexPoint(BaseModel):
    date: dt.date
    frequency: Frequency
    index_value: Decimal = Field(..., description="Index, reference period = 100")
    n_cells: int = Field(..., description="Route x window cells in the aggregate")
    coverage_ratio: Decimal = Field(
        ...,
        description=(
            "Share of basket weight actually observed. Below 1 means the value "
            "rests on a partial basket and was renormalised."))
    observed_pax_share: Decimal | None = Field(
        None,
        description=(
            "Share of domestic passenger traffic flown by carriers this index "
            "can legally observe. Upper bound: national carrier shares stand in "
            "for route-level shares, which DGCA does not publish."))


class IndexResponse(BaseModel):
    index: IndexPoint
    change_1d: Decimal | None = Field(None, description="Percent vs previous period")
    change_30d: Decimal | None = Field(None, description="Percent vs 30 periods back")
    provenance: Provenance


class SeriesResponse(BaseModel):
    frequency: Frequency
    start: dt.date | None
    end: dt.date | None
    count: int
    points: list[IndexPoint]
    provenance: Provenance


class RoutePoint(BaseModel):
    route_id: int
    route_code: str
    origin: str
    destination: str
    index_value: Decimal
    weight: Decimal = Field(..., description="Basket weight observed that period")
    contribution: Decimal = Field(
        ..., description="Weighted contribution to the headline index")


class RouteBreakdownResponse(BaseModel):
    date: dt.date
    frequency: Frequency
    headline: Decimal
    routes: list[RoutePoint]
    provenance: Provenance


class RouteDefinition(BaseModel):
    route_id: int
    route_code: str
    origin: str
    destination: str
    origin_city: str
    destination_city: str
    passengers: int | None = Field(
        None, description="Passengers over the weight reference period")
    weight: Decimal | None = None
    in_basket: bool


class BasketResponse(BaseModel):
    routes: list[RouteDefinition]
    advance_windows: list[dict]
    weight_reference: str
    total_weight: Decimal


class LeadTimePoint(BaseModel):
    window_days: int
    n_observations: int
    mean_fare: Decimal
    median_fare: Decimal
    min_fare: Decimal
    ratio_to_longest_window: Decimal | None = Field(
        None,
        description=(
            "Mean fare relative to the longest advance window in the basket. "
            "The lead-time premium a traveller pays for booking later."))


class ElasticityEstimate(BaseModel):
    value: float
    std_error: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    significant: bool = False


class LeadTimeFit(BaseModel):
    """Fitted lead-time model.

    Two models are reported deliberately. The exponential is the model; the
    log-linear slope is a single interpretable number that necessarily
    misstates a curved relationship. Reporting only the second would imply a
    constant percentage effect, when the point is that the effect concentrates
    in the final fortnight.
    """
    model: str = Field("exponential_decay", description="Primary model")
    form: str = Field("fare(w) = L * (1 + a * exp(-w / tau))")
    floor: ElasticityEstimate = Field(
        ..., description="L: the far-advance price level the curve decays towards")
    amplitude: ElasticityEstimate = Field(
        ..., description="a: last-minute premium as a multiple of the floor")
    decay_days: ElasticityEstimate = Field(
        ..., description="tau: days for the premium to fall by a factor of e")
    r_squared: float
    rmse: float
    n: int
    converged: bool
    percent_per_day: float | None = Field(
        None,
        description=("Log-linear slope as a percentage: the average fare change "
                     "per extra day of lead time. A misspecified summary of a "
                     "curved relationship; use the exponential model instead."))
    warnings: list[str] = Field(default_factory=list)


class LeadTimeResponse(BaseModel):
    route_code: str
    start: dt.date | None
    end: dt.date | None
    points: list[LeadTimePoint]
    fit: LeadTimeFit | None = Field(
        None, description="Null when too few observations to identify a curve.")
    note: str


class ComplianceRow(BaseModel):
    source: str
    tier: int
    audit_verdict: str
    requests: int
    requests_sent: int
    robots_blocked: int
    intent_blocked: int
    ok: int
    errors: int
    min_delay_s: Decimal | None
    avg_delay_s: Decimal | None
    max_delay_s: Decimal | None
    first_request: dt.datetime | None
    last_request: dt.datetime | None


class ComplianceResponse(BaseModel):
    sources: list[ComplianceRow] = Field(
        ..., description="Request log per source, from the live audit trail.")
    excluded_on_compliance_grounds: list[dict] = Field(
        ...,
        description=(
            "Sources we can technically scrape but deliberately do not. Tier 3 "
            "permits it in robots.txt but enforces an anti-automation control; "
            "tier 4 disallows the fare-search path outright. This list is the "
            "substance of the compliance posture."))
    pending_implementation: list[dict] = Field(
        ...,
        description=(
            "Sources cleared by the audit whose adapter is not written yet. "
            "Absent for engineering reasons, not compliance ones, and listed "
            "separately so the two are never conflated."))
    note: str


class MethodologyResponse(BaseModel):
    index_name: str
    version: str
    elementary_formula: str
    elementary_formula_latex: str
    upper_formula: str
    upper_formula_latex: str
    elementary_aggregate: str
    price_concept: str
    index_reference_period: str
    weight_reference_period: str
    frequencies: list[str]
    cpi_alignment: dict
    missing_data_policy: dict
    limitations: list[str]
    documentation: dict


class HealthResponse(BaseModel):
    status: str
    database: str
    seeded_mode: bool
    latest_index_date: dt.date | None
    version: str


class ScrapeRunSummary(BaseModel):
    run_id: int | None = None
    scrape_date: dt.date
    mode: str = Field(..., description="'scheduled', 'manual', or 'backfill'")
    planned_cells: int = Field(..., description="Cells that were intended to be collected")
    ok_cells: int | None = Field(
        None, description="Cells that returned at least one fare")
    failed_cells: int | None = Field(None, description="Cells that errored or timed out")
    quotes_written: int | None = Field(None, description="Fare rows persisted")
    finished_at: dt.datetime | None = None
    is_real: bool = Field(
        ...,
        description=(
            "True when this run collected live fares from an airline website. "
            "False for seeded synthetic runs. A live dashboard must show only "
            "runs where this is true."))


class ScrapeHealthResponse(BaseModel):
    """Scrape pipeline health — designed for judges and monitoring dashboards.

    The key field is `is_real`: if it is false across all recent runs the index
    rests entirely on synthetic data and must not be cited as a measurement.
    """
    status: str = Field(
        ...,
        description="'live' if recent real fares exist, 'synthetic' if only seeded, "
                    "'no_data' if the database is empty.")
    seeded_mode: bool = Field(
        ...,
        description="True when APIX_USE_SEED_DATA is set or all stored fares are synthetic.")
    days_of_real_data: int = Field(
        0,
        description="How many distinct scrape_date values have at least one real (non-seeded) fare.")
    last_real_scrape: dt.date | None = Field(
        None,
        description="Most recent scrape_date that produced real fares. None if no real data.")
    last_run: ScrapeRunSummary | None = Field(
        None,
        description="The most recent collection_run row, real or seeded.")
    enabled_sources: list[str] = Field(
        default_factory=list,
        description="Source codes currently enabled in config/sources.yaml (tier 1/2).")
    observed_pax_share: float | None = Field(
        None,
        description=(
            "Share of Indian domestic passenger traffic observable by enabled sources. "
            "This is capped at ~8.3% because IndiGo, Air India and Air India Express "
            "are excluded on robots.txt / bot-management grounds."))
    note: str = Field(
        "SpiceJet adapter (SG) is written and compliant; first live run will confirm "
        "or name the field aliases to add. Akasa (QP) adapter is the primary source.",
        description="Plain-language status note.")


class AnomalyPoint(BaseModel):
    index_date: dt.date
    index_value: Decimal
    change_pct: float
    score: float = Field(..., description="Modified z-score of the day's change")
    direction: Literal["spike", "drop"]
    hypotheses: list[str] = Field(
        default_factory=list,
        description=("Candidate explanations, phrased as things to check. This "
                     "system has no evidence about causation and asserts none."))


class AnomalyEpisode(BaseModel):
    start: dt.date
    end: dt.date
    direction: Literal["spike", "drop"]
    peak_date: dt.date
    peak_change_pct: float
    days: list[AnomalyPoint]


class AnomalyResponse(BaseModel):
    window: int
    threshold: float
    n_flagged: int
    episodes: list[AnomalyEpisode]
    note: str
    provenance: Provenance


class NowcastScore(BaseModel):
    model: str
    horizon: int = Field(..., description="Days ahead")
    n_forecasts: int = Field(..., description="Rolling-origin forecasts scored")
    mae: float
    rmse: float
    mase: float = Field(
        ...,
        description=("Mean absolute scaled error, scaled by the in-sample "
                     "one-step seasonal-naive MAE. Comparable across horizons; "
                     "NOT a verdict on its own — see `verdict`."))
    is_baseline: bool
    verdict: str = Field(
        ...,
        description=("How this model fared against the best BASELINE at the "
                     "same horizon, which is the bar it actually has to clear."))


class NowcastResponse(BaseModel):
    n_observations: int
    season: int = Field(..., description="Seasonal period in days")
    min_train: int
    scores: list[NowcastScore]
    recommendation: str = Field(
        ...,
        description=("Plain-language verdict. Says 'no model is recommended' "
                     "when that is the honest answer, which it currently is."))
    warnings: list[str] = Field(default_factory=list)
    provenance: Provenance
