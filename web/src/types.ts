export interface PlaceSummary {
  place_id: string;
  name: string;
  state: string;
  place_type: string;
  population_2020: number;
  housing_units_2020: number;
  res_hazard_npctl: number | null;
  res_hazard_spread: number | null;
  res_hazard_spectral: number | null;
  res_hazard_tail: number | null;
  res_hazard_power4: number | null;
  property_loss_npctl: number | null;
  res_hazard_data_quality: "complete" | "partial" | "unavailable";
  res_hazard_available_count: number;
  res_hazard_coverage_ratio: number;
  alr_npctl: number;
  alr_valb: number | null;
  fema_vintage: string;
  census_vintage: string;
  county_fips: string;
  county_name: string;
  community_conditions_group: number | null;
  community_conditions_geography: "county";
  chrr_release_year: number;
  mountain_magnitude: number | null;
  mountain_magnitude_version: string | null;
  mountain_pipeline_version: string | null;
  relief_5km_m: number | null;
  relief_10km_m: number | null;
  relief_20km_m: number | null;
  relief_40km_m: number | null;
  relief_20km_pct: number | null;
  rugged_fraction_20km: number | null;
  rugged_pct: number | null;
  public_mountain_access_raw: number | null;
  public_mountain_access_pct: number | null;
  open_mountain_km2_5: number | null;
  open_mountain_km2_15: number | null;
  open_mountain_km2_30: number | null;
  restricted_mountain_km2_30: number | null;
  closed_mountain_km2_30: number | null;
  unknown_mountain_km2_30: number | null;
  nearest_mountain_trail_km: number | null;
  mountain_trail_km_10: number | null;
  mountain_trail_km_25: number | null;
  trail_access_raw: number | null;
  trail_access_pct: number | null;
  mountain_population_coverage: number;
  mountain_coverage_status: "complete" | "partial" | "insufficient_coverage" | "zero_population" | "outside_scope" | "unavailable";
  cost_of_living_index: number | null;
  cost_of_living_goods_index: number | null;
  cost_of_living_housing_rents_index: number | null;
  cost_of_living_utilities_index: number | null;
  cost_of_living_other_services_index: number | null;
  cost_of_living_geography_type: "metropolitan" | "nonmetropolitan" | null;
  cost_of_living_geography_id: string | null;
  cost_of_living_geography_name: string | null;
  cost_of_living_release_year: number | null;
  cost_of_living_coverage_status: "complete" | "outside_scope" | "unmatched_geography" | "source_unavailable";
  cost_of_living_attribution: string;
  home_sqft_for_1m: number | null;
  home_buying_power_percentile: number | null;
  home_median_listing_price: number | null;
  home_median_listing_price_per_square_foot: number | null;
  home_median_square_feet: number | null;
  home_active_listing_count: number | null;
  home_market_month: string | null;
  home_costs_coverage_status: "complete" | "source_quality_flag" | "missing_market" | "invalid_price_per_square_foot" | "outside_scope" | "source_unavailable";
  home_market_attribution: string;
  home_market_usage_notice: string;
  housing_stock_total_units_estimate: number | null;
  housing_built_2000_plus_pct: number | null;
  housing_built_2010_plus_pct: number | null;
  housing_built_2020_plus_pct: number | null;
  housing_median_year_built: number | null;
  housing_stock_release_year: number | null;
  housing_stock_coverage_status: "complete" | "zero_housing" | "missing_acs" | "outside_scope" | "asset_unavailable";
  housing_stock_attribution: string;
}

export interface HazardPercentile {
  code: string;
  label: string;
  percentile: number | null;
  raw_alrb: number | null;
  availability: "valid" | "not_applicable" | "missing" | "invalid";
  fema_eal_rating: string | null;
}

export interface PlaceDetail {
  summary: PlaceSummary;
  methodology_notice: string;
  hazard_percentiles: HazardPercentile[];
  member_tract_count: number | null;
  source_notices: string[];
}

export interface AddressLookup {
  status: "resolved";
  query: string;
  matched_address: string;
  tract_id: string;
  detail: PlaceDetail;
  provider: "census" | "nominatim";
  precision: "house" | "street";
  approximate: boolean;
  attribution: string | null;
}

export interface FallbackCandidate {
  candidate_id: string;
  matched_address: string;
  precision: "street";
}

export interface AddressConfirmation {
  status: "confirmation_required";
  query: string;
  message: string;
  attribution: string;
  candidates: FallbackCandidate[];
}

export type LookupResult = AddressLookup | AddressConfirmation;

export interface JobStatus {
  job_id: string;
  state: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  progress: number;
  message: string;
  error: string | null;
}

export type Geography = "tract" | "county";
export type Metric = "residential-hazard" | "community-conditions" | "mountain" | "cost-of-living" | "home-costs";
export type MapMetric = Metric | "county-fit";
export type CountyFitView = "safety" | "health" | "affordability" | "opportunity" | "lifestyle" | "family" | "custom";
export type CountyFitPillar = Exclude<CountyFitView, "custom">;
export type MapScope = { kind: "national"; state: null } | { kind: "state"; state: string };

export interface MapFilters {
  state: string;
  county: string;
  showUnavailable: boolean;
  mountainMagnitudeMin: number | null;
  communityConditionsGroupMax: number | null;
  costOfLivingIndexMax: number | null;
  homeSqftFor1mMin: number | null;
  housingBuilt2000PlusPctMin: number | null;
}

export interface LayerDescriptor {
  key: "residential-hazard" | "community-conditions" | "mountain" | "cost-of-living" | "home-costs";
  display_name: string;
  source: string;
  direction: "lower" | "higher";
  availability: "available" | "unavailable";
  vintage: string;
  geography: string;
  attribution: string;
  notice: string;
}

export interface SourceDescriptor {
  source: string;
  version: string;
  release: string | number | null;
  cached: boolean;
  sha256: string | null;
  row_count: number | null;
  stale: boolean | null;
  attribution: string;
  usage_notice: string | null;
  coverage_status: string;
  error: string | null;
}

export interface BuildMeta {
  build_id: string;
  place_count: number;
  ranked_place_count: number;
  county_count?: number;
  ranked_county_count?: number;
  source_vintages: Record<string, string | number>;
  scope: MapScope;
  sources?: SourceDescriptor[];
}

export interface MapAssetStatus {
  ready: boolean;
  error: string | null;
  schema_version: number | null;
  release: string | null;
  manifest_url: string;
}

export interface Meta {
  app_version: string;
  mutation_token: string;
  reference_assets_ready: boolean;
  reference_assets_error: string | null;
  map_assets: MapAssetStatus;
  build: BuildMeta | null;
  layers: LayerDescriptor[];
  county_fit: CountyFitReadiness;
}

export interface CountyFitReadiness {
  readiness: "unavailable" | "partial" | "ready";
  reason_code: string | null;
  methodology_id: string;
  calibration_id?: string | null;
  bundle_schema_version?: number | null;
  bundle_release?: string | null;
  vintages?: Record<string, string | number>;
  row_count?: number;
  population_floor?: number;
  rank_policy?: "competition";
  available_pillars: CountyFitPillar[];
  local_history?: {
    status?: string;
    valid_months?: number;
    required_months?: number;
    commands?: string[];
  };
  notices?: string[];
}

export interface CountyFitColumns {
  county_fips: string[];
  name: string[];
  state: string[];
  active_value: Array<number | null>;
  eligible: boolean[];
  exclusion_reason: Array<string | null>;
  national_rank: Array<number | null>;
  filtered_rank: Array<number | null>;
  pareto_optimal: Array<boolean | null>;
  u_safety: Array<number | null>;
  u_health: Array<number | null>;
  u_affordability: Array<number | null>;
  u_opportunity: Array<number | null>;
  u_lifestyle: Array<number | null>;
  u_family: Array<number | null>;
}

export interface CountyFitSummary {
  schema_version: 1;
  build_id: string;
  methodology_id: string;
  calibration_id: string;
  view: CountyFitView;
  preset: string | null;
  weights: Record<CountyFitPillar, number>;
  gates: Record<string, unknown>;
  reference_count: number;
  national_count: number;
  cohort_count: number;
  exclusions: Record<string, number>;
  notices: string[];
  counties: CountyFitColumns;
}

export interface CountyFitDetail {
  schema_version: 1;
  build_id: string;
  county: { fips: string; name: string; state: string };
  view: CountyFitView;
  population: number | null;
  active_value: number | null;
  eligible: boolean;
  exclusion_reason: string | null;
  reference_only: boolean;
  national_rank: number | null;
  filtered_rank: number | null;
  pareto_optimal: boolean | null;
  weights: Record<CountyFitPillar, number>;
  gates: Record<string, unknown>;
  pillars: Record<CountyFitPillar, {
    utility: number | null;
    weight: number;
    contribution: number | null;
    measures: Record<string, string | number | null>;
  }>;
  subutilities: Record<string, number | null>;
  coverage: Record<string, string | number | null>;
  vintages: Record<string, unknown>;
  sources: Record<string, string | null>;
  citations: Record<string, unknown>;
  rubric_components: Record<string, unknown> | null;
  limitations: string[];
}

export interface MapScore {
  place_id: string;
  res_hazard_npctl: number | null;
  community_conditions_group: number | null;
  mountain_magnitude: number | null;
  cost_of_living_index: number | null;
  home_buying_power_percentile: number | null;
  home_sqft_for_1m: number | null;
  housing_built_2000_plus_pct: number | null;
}

export interface MapScoreColumns {
  place_id: string[];
  res_hazard_npctl: Array<number | null>;
  community_conditions_group: Array<number | null>;
  mountain_magnitude: Array<number | null>;
  cost_of_living_index: Array<number | null>;
  home_buying_power_percentile: Array<number | null>;
  home_sqft_for_1m: Array<number | null>;
  housing_built_2000_plus_pct: Array<number | null>;
}

export type MapScoreAddonKind = "cost-of-living" | "home-costs";

export interface MapScores {
  schema_version: 5;
  build_id: string;
  level: Geography;
  scope: MapScope;
  columns: MapScoreColumns;
  add_ons?: {
    cost_of_living: string;
    home_costs: string;
  };
}

export type MapValueDataset = MapScores | CountyFitSummary;

export interface MapScoreAddon {
  schema_version: 1;
  kind: MapScoreAddonKind;
  build_id: string;
  level: Geography;
  scope: MapScope;
  columns: {
    place_id: string[];
    cost_of_living_index?: Array<number | null>;
    home_buying_power_percentile?: Array<number | null>;
    home_sqft_for_1m?: Array<number | null>;
    housing_built_2000_plus_pct?: Array<number | null>;
  };
}

export interface MapAssetEntry {
  key: string;
  filename: string;
  level: Geography | "state";
  lod: "national" | "detail";
  jurisdiction: string | null;
  feature_count: number;
  bounds: [number, number, number, number];
  compressed_size: number;
  sha256: string;
}

export interface MapManifest {
  schema_version: 1;
  release: string;
  files: MapAssetEntry[];
  initial_compressed_size: number;
}
