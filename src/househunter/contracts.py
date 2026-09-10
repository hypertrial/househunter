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


class TractContribution(BaseModel):
    tract_id: str | None
    housing_units: int
    housing_weight: float
    fema_percentile: float | None
    weighted_contribution: float | None


class PlaceDetail(BaseModel):
    summary: PlaceSummary
    total_weighted_housing: int
    coverage_ratio: float
    methodology_notice: str
    tract_contributions: list[TractContribution]


class PlacePage(BaseModel):
    items: list[PlaceSummary]
    total: int
    offset: int
    limit: int


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
    "HouseHunter aggregates FEMA tract ALR_NPCTL percentiles using 2020 Census housing "
    "weights. It is not FEMA's broader Risk Index, a FEMA-published Place percentile, "
    "property assessment, loss probability, insurance quote, or prediction."
)
