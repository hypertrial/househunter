from __future__ import annotations

import math
from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

ResidentialHazardDataQuality = Literal["complete", "partial", "unavailable"]
MountainCoverageStatus = Literal[
    "complete",
    "partial",
    "insufficient_coverage",
    "zero_population",
    "outside_scope",
    "unavailable",
]
CostOfLivingCoverageStatus = Literal[
    "complete",
    "outside_scope",
    "unmatched_geography",
    "source_unavailable",
]
HomeCostsCoverageStatus = Literal[
    "complete",
    "source_quality_flag",
    "missing_market",
    "invalid_price_per_square_foot",
    "outside_scope",
    "source_unavailable",
]
HousingStockCoverageStatus = Literal[
    "complete",
    "zero_housing",
    "missing_acs",
    "outside_scope",
    "asset_unavailable",
]

COST_OF_LIVING_COVERAGE_STATUSES = frozenset(
    get_args(CostOfLivingCoverageStatus)
)
HOME_COSTS_COVERAGE_STATUSES = frozenset(get_args(HomeCostsCoverageStatus))
HOUSING_STOCK_COVERAGE_STATUSES = frozenset(
    get_args(HousingStockCoverageStatus)
)


class PlaceSummary(BaseModel):
    place_id: str
    name: str
    state: str
    place_type: str
    population_2020: int
    housing_units_2020: int
    res_hazard_npctl: float | None
    res_hazard_spread: float | None
    res_hazard_spectral: float | None
    res_hazard_tail: float | None
    res_hazard_power4: float | None
    property_loss_npctl: float | None
    res_hazard_data_quality: ResidentialHazardDataQuality
    res_hazard_available_count: int
    res_hazard_coverage_ratio: float
    alr_npctl: float
    alr_valb: float | None
    fema_vintage: str
    census_vintage: str
    county_fips: str
    county_name: str
    community_conditions_group: int | None
    community_conditions_geography: Literal["county"] = "county"
    chrr_release_year: int
    mountain_magnitude: float | None
    mountain_magnitude_version: str | None
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
    cost_of_living_index: float | None
    cost_of_living_goods_index: float | None
    cost_of_living_housing_rents_index: float | None
    cost_of_living_utilities_index: float | None
    cost_of_living_other_services_index: float | None
    cost_of_living_geography_type: Literal["metropolitan", "nonmetropolitan"] | None
    cost_of_living_geography_id: str | None
    cost_of_living_geography_name: str | None
    cost_of_living_release_year: int | None
    cost_of_living_coverage_status: CostOfLivingCoverageStatus
    cost_of_living_attribution: str
    home_sqft_for_1m: int | None
    home_buying_power_percentile: float | None
    home_median_listing_price: float | None
    home_median_listing_price_per_square_foot: float | None
    home_median_square_feet: float | None
    home_active_listing_count: float | None
    home_market_month: str | None
    home_costs_coverage_status: HomeCostsCoverageStatus
    home_market_attribution: str
    home_market_usage_notice: str
    housing_stock_total_units_estimate: int | None
    housing_built_2000_plus_pct: float | None
    housing_built_2010_plus_pct: float | None
    housing_built_2020_plus_pct: float | None
    housing_median_year_built: int | None
    housing_stock_release_year: int | None
    housing_stock_coverage_status: HousingStockCoverageStatus
    housing_stock_attribution: str


class HazardPercentile(BaseModel):
    code: str
    label: str
    percentile: float | None
    raw_alrb: float | None
    availability: Literal["valid", "not_applicable", "missing", "invalid"]
    fema_eal_rating: str | None


class PlaceDetail(BaseModel):
    summary: PlaceSummary
    methodology_notice: str
    hazard_percentiles: list[HazardPercentile]
    member_tract_count: int | None = None
    source_notices: list[str] = Field(default_factory=list)


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
    res_hazard_npctl: list[float | None]
    community_conditions_group: list[int | None]
    mountain_magnitude: list[float | None]
    cost_of_living_index: list[float | None]
    home_buying_power_percentile: list[float | None]
    home_sqft_for_1m: list[float | None]
    housing_built_2000_plus_pct: list[float | None]

    @model_validator(mode="after")
    def validate_alignment(self) -> MapScoreColumns:
        lengths = {
            len(self.place_id),
            len(self.res_hazard_npctl),
            len(self.community_conditions_group),
            len(self.mountain_magnitude),
            len(self.cost_of_living_index),
            len(self.home_buying_power_percentile),
            len(self.home_sqft_for_1m),
            len(self.housing_built_2000_plus_pct),
        }
        if len(lengths) != 1:
            raise ValueError("Map score columns must have equal lengths")
        adjacent_ids = zip(self.place_id, self.place_id[1:], strict=False)
        if any(left >= right for left, right in adjacent_ids):
            raise ValueError("Map score place IDs must be ordered and unique")
        for index in range(len(self.place_id)):
            finite_values = (
                self.res_hazard_npctl[index],
                self.mountain_magnitude[index],
                self.cost_of_living_index[index],
                self.home_buying_power_percentile[index],
                self.home_sqft_for_1m[index],
                self.housing_built_2000_plus_pct[index],
            )
            if any(value is not None and not math.isfinite(value) for value in finite_values):
                raise ValueError("Map score values must be finite or null")
            exposure = self.res_hazard_npctl[index]
            group = self.community_conditions_group[index]
            mountain = self.mountain_magnitude[index]
            cost = self.cost_of_living_index[index]
            percentile = self.home_buying_power_percentile[index]
            square_feet = self.home_sqft_for_1m[index]
            housing = self.housing_built_2000_plus_pct[index]
            if exposure is not None and not 0 <= exposure <= 100:
                raise ValueError("Map Residential Hazard Exposure must be between 0 and 100")
            if group is not None and not 1 <= group <= 10:
                raise ValueError("Map Community Conditions group must be between 1 and 10")
            if mountain is not None and mountain < 0:
                raise ValueError("Map Mountain Magnitude must be nonnegative")
            if cost is not None and cost <= 0:
                raise ValueError("Map Cost of Living index must be positive")
            if percentile is not None and not 0 <= percentile <= 100:
                raise ValueError("Map home buying-power percentile must be between 0 and 100")
            if square_feet is not None and square_feet <= 0:
                raise ValueError("Map home square feet must be positive")
            if housing is not None and not 0 <= housing <= 100:
                raise ValueError("Map built-2000+ share must be between 0 and 100")
        return self


class MapScoreCoreColumns(BaseModel):
    model_config = ConfigDict(extra="forbid")

    place_id: list[str]
    res_hazard_npctl: list[float | None]
    community_conditions_group: list[int | None]
    mountain_magnitude: list[float | None]

    @model_validator(mode="after")
    def validate_alignment(self) -> MapScoreCoreColumns:
        columns = (
            self.place_id,
            self.res_hazard_npctl,
            self.community_conditions_group,
            self.mountain_magnitude,
        )
        if len({len(column) for column in columns}) != 1:
            raise ValueError("Core map score columns must have equal lengths")
        if any(
            left >= right
            for left, right in zip(self.place_id, self.place_id[1:], strict=False)
        ):
            raise ValueError("Core map score place IDs must be ordered and unique")
        for exposure, group, mountain in zip(
            self.res_hazard_npctl,
            self.community_conditions_group,
            self.mountain_magnitude,
            strict=True,
        ):
            if any(
                value is not None and not math.isfinite(value)
                for value in (exposure, mountain)
            ):
                raise ValueError("Core map score values must be finite or null")
            if exposure is not None and not 0 <= exposure <= 100:
                raise ValueError("Map Residential Hazard Exposure must be between 0 and 100")
            if group is not None and not 1 <= group <= 10:
                raise ValueError("Map Community Conditions group must be between 1 and 10")
            if mountain is not None and mountain < 0:
                raise ValueError("Map Mountain Magnitude must be nonnegative")
        return self


class CostOfLivingMapColumns(BaseModel):
    model_config = ConfigDict(extra="forbid")

    place_id: list[str]
    cost_of_living_index: list[float | None]

    @model_validator(mode="after")
    def validate_alignment(self) -> CostOfLivingMapColumns:
        if len(self.place_id) != len(self.cost_of_living_index):
            raise ValueError("Cost of Living map columns must have equal lengths")
        if any(
            left >= right
            for left, right in zip(self.place_id, self.place_id[1:], strict=False)
        ):
            raise ValueError("Cost of Living map place IDs must be ordered and unique")
        if any(
            value is not None and (not math.isfinite(value) or value <= 0)
            for value in self.cost_of_living_index
        ):
            raise ValueError("Map Cost of Living index must be positive and finite")
        return self


class HomeCostsMapColumns(BaseModel):
    model_config = ConfigDict(extra="forbid")

    place_id: list[str]
    home_buying_power_percentile: list[float | None]
    home_sqft_for_1m: list[float | None]
    housing_built_2000_plus_pct: list[float | None]

    @model_validator(mode="after")
    def validate_alignment(self) -> HomeCostsMapColumns:
        columns = (
            self.place_id,
            self.home_buying_power_percentile,
            self.home_sqft_for_1m,
            self.housing_built_2000_plus_pct,
        )
        if len({len(column) for column in columns}) != 1:
            raise ValueError("Home Costs map columns must have equal lengths")
        if any(
            left >= right
            for left, right in zip(self.place_id, self.place_id[1:], strict=False)
        ):
            raise ValueError("Home Costs map place IDs must be ordered and unique")
        for percentile, square_feet, built_2000 in zip(
            self.home_buying_power_percentile,
            self.home_sqft_for_1m,
            self.housing_built_2000_plus_pct,
            strict=True,
        ):
            if any(
                value is not None and not math.isfinite(value)
                for value in (percentile, square_feet, built_2000)
            ):
                raise ValueError("Home Costs map values must be finite or null")
            if percentile is not None and not 0 <= percentile <= 100:
                raise ValueError("Map home buying-power percentile must be between 0 and 100")
            if square_feet is not None and square_feet <= 0:
                raise ValueError("Map home square feet must be positive")
            if built_2000 is not None and not 0 <= built_2000 <= 100:
                raise ValueError("Map built-2000+ share must be between 0 and 100")
        return self


class MapScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[5] = 5
    build_id: str
    level: Literal["tract", "county"]
    scope: MapScoreScope
    columns: MapScoreColumns


class MapScoreAddons(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cost_of_living: str
    home_costs: str


class MapScoresCore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[5] = 5
    build_id: str
    level: Literal["tract", "county"]
    scope: MapScoreScope
    columns: MapScoreCoreColumns
    add_ons: MapScoreAddons


class CostOfLivingMapScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["cost-of-living"] = "cost-of-living"
    build_id: str
    level: Literal["tract", "county"]
    scope: MapScoreScope
    columns: CostOfLivingMapColumns


class HomeCostsMapScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["home-costs"] = "home-costs"
    build_id: str
    level: Literal["tract", "county"]
    scope: MapScoreScope
    columns: HomeCostsMapColumns


class SourceStatus(BaseModel):
    source: str
    version: str
    cached: bool
    sha256: str | None = None
    row_count: int | None = None
    retrieved_at: datetime | None = None
    release: str | int | None = None
    stale: bool | None = None
    attribution: str | None = None
    usage_notice: str | None = None
    coverage_status: str | None = None
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
    "HouseHunter derives tract Residential Hazard Exposure from FEMA building-specific "
    "Expected Annual Loss Rates across 17 hazards. It emphasizes elevated hazards and "
    "ranks tracts nationally. It is not a property assessment, loss probability, "
    "insurance quote, or prediction."
)

COUNTY_METHODOLOGY_NOTICE = (
    "HouseHunter derives county Residential Hazard Exposure from FEMA building-specific "
    "Expected Annual Loss Rates across 17 hazards and ranks counties nationally, never "
    "by averaging tract scores. It is not a property assessment, loss probability, "
    "insurance quote, or prediction."
)
