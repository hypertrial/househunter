from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

CoverageStatus = Literal["complete", "zero_housing", "missing_fema", "unmatched_geography"]


class PlaceSummary(BaseModel):
    place_id: str
    name: str
    state: str
    place_type: str
    population_2020: int
    housing_units_2020: int
    risk_score: float | None
    coverage_status: CoverageStatus
    fema_vintage: str
    census_vintage: str
    county_fips: str
    county_name: str


class TractContribution(BaseModel):
    tract_id: str | None
    housing_units: int
    housing_weight: float
    fema_percentile: float | None
    weighted_contribution: float | None


class HazardPercentile(BaseModel):
    code: str
    label: str
    percentile: float | None


class PlaceDetail(BaseModel):
    summary: PlaceSummary
    total_weighted_housing: int
    coverage_ratio: float
    methodology_notice: str
    tract_contributions: list[TractContribution]
    hazard_percentiles: list[HazardPercentile]
    member_tract_count: int | None = None


class AddressLookupRequest(BaseModel):
    address: str = Field(max_length=200)
    candidate_id: str | None = Field(default=None, max_length=64)


class AddressLookup(BaseModel):
    status: Literal["resolved"] = "resolved"
    query: str
    matched_address: str
    tract_id: str
    detail: PlaceDetail
    provider: Literal["census", "nominatim"] = "census"
    precision: Literal["house", "street"] = "house"
    approximate: bool = False
    attribution: str | None = None


class FallbackCandidate(BaseModel):
    candidate_id: str
    matched_address: str
    precision: Literal["street"]


class AddressConfirmation(BaseModel):
    status: Literal["confirmation_required"]
    query: str
    message: str
    attribution: str
    candidates: list[FallbackCandidate]


class PlacePage(BaseModel):
    items: list[PlaceSummary]
    total: int
    offset: int
    limit: int


class MapScore(BaseModel):
    place_id: str
    risk_score: float | None
    coverage_status: CoverageStatus


class MapScores(BaseModel):
    schema_version: Literal[1] = 1
    build_id: str
    level: Literal["tract", "county"]
    scope: dict[str, str | None]
    rows: list[MapScore]


class SourceStatus(BaseModel):
    source: str
    version: str
    cached: bool
    sha256: str | None = None
    row_count: int | None = None
    retrieved_at: datetime | None = None
    error: str | None = None


class JobStatus(BaseModel):
    job_id: str
    kind: Literal["download", "build", "prepare"]
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    progress: int = Field(ge=0, le=100)
    message: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


METHODOLOGY_NOTICE = (
    "HouseHunter ranks FEMA National Risk Index tracts by their published ALR_NPCTL "
    "percentile. Per-hazard bars are FEMA's published {CODE}_ALR_NPCTL values at the "
    "tract grain; they are not a HouseHunter blend. This is not FEMA's broader Risk "
    "Index, a property assessment, loss probability, insurance quote, or prediction."
)

COUNTY_METHODOLOGY_NOTICE = (
    "HouseHunter ranks FEMA National Risk Index counties by their published county-level "
    "ALR_NPCTL percentile, ranked among counties. Per-hazard bars are FEMA's published "
    "county {CODE}_ALR_NPCTL values, not an average of tract percentiles. This is not "
    "FEMA's broader Risk Index, a property assessment, loss probability, insurance "
    "quote, or prediction."
)
