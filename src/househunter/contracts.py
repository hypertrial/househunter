from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CoverageStatus = Literal["complete", "zero_housing", "missing_fema", "unmatched_geography"]
MountainCoverageStatus = Literal[
    "complete",
    "partial",
    "insufficient_coverage",
    "zero_population",
    "outside_scope",
    "unavailable",
]


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
    community_conditions_group: int | None
    community_conditions_geography: Literal["county"] = "county"
    chrr_release_year: int
    mountain_score: float | None
    mountain_score_version: str | None
    mountain_pipeline_version: str | None
    relief_5km_m: float | None
    relief_10km_m: float | None
    relief_20km_m: float | None
    relief_40km_m: float | None
    relief_20km_pct: float | None
    rugged_fraction_20km: float | None
    rugged_pct: float | None
    public_mountain_access_raw: float | None
    public_mountain_access_pct: float | None
    open_mountain_km2_5: float | None
    open_mountain_km2_15: float | None
    open_mountain_km2_30: float | None
    restricted_mountain_km2_30: float | None
    closed_mountain_km2_30: float | None
    unknown_mountain_km2_30: float | None
    nearest_mountain_trail_km: float | None
    mountain_trail_km_10: float | None
    mountain_trail_km_25: float | None
    trail_access_raw: float | None
    trail_access_pct: float | None
    mountain_population_coverage: float
    mountain_coverage_status: MountainCoverageStatus


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


class MapScoreScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["national", "state"]
    state: str | None

    @model_validator(mode="after")
    def validate_scope(self) -> MapScoreScope:
        if self.kind == "national" and self.state is not None:
            raise ValueError("National map score scope cannot include a state")
        if self.kind == "state" and (
            self.state is None or len(self.state) != 2 or not self.state.isupper()
        ):
            raise ValueError("State map score scope requires a two-letter state code")
        return self


class MapScoreColumns(BaseModel):
    model_config = ConfigDict(extra="forbid")

    place_id: list[str]
    risk_score: list[float | None]
    community_conditions_group: list[int | None]
    mountain_score: list[float | None]

    @model_validator(mode="after")
    def validate_alignment(self) -> MapScoreColumns:
        lengths = {
            len(self.place_id),
            len(self.risk_score),
            len(self.community_conditions_group),
            len(self.mountain_score),
        }
        if len(lengths) != 1:
            raise ValueError("Map score columns must have equal lengths")
        adjacent_ids = zip(self.place_id, self.place_id[1:], strict=False)
        if any(left >= right for left, right in adjacent_ids):
            raise ValueError("Map score place IDs must be ordered and unique")
        return self


class MapScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    build_id: str
    level: Literal["tract", "county"]
    scope: MapScoreScope
    columns: MapScoreColumns


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
