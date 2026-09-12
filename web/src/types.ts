export type CoverageStatus = "complete" | "zero_housing" | "missing_fema" | "unmatched_geography";

export interface PlaceSummary {
  place_id: string;
  name: string;
  state: string;
  place_type: string;
  population_2020: number;
  housing_units_2020: number;
  risk_score: number | null;
  coverage_status: CoverageStatus;
  fema_vintage: string;
  census_vintage: string;
  county_fips: string;
  county_name: string;
  community_conditions_group: number | null;
  community_conditions_geography: "county";
  chrr_release_year: number;
}

export interface TractContribution {
  tract_id: string | null;
  housing_units: number;
  housing_weight: number;
  fema_percentile: number | null;
  weighted_contribution: number | null;
}

export interface HazardPercentile {
  code: string;
  label: string;
  percentile: number | null;
}

export interface PlaceDetail {
  summary: PlaceSummary;
  total_weighted_housing: number;
  coverage_ratio: number;
  methodology_notice: string;
  tract_contributions: TractContribution[];
  hazard_percentiles: HazardPercentile[];
  member_tract_count: number | null;
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
export type Metric = "fema" | "community-conditions";

export interface BuildMeta {
  build_id: string;
  place_count: number;
  ranked_place_count: number;
  county_count?: number;
  ranked_county_count?: number;
  source_vintages: Record<string, string | number>;
  scope: { kind: string; state: string | null };
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
}

export interface MapScore {
  place_id: string;
  risk_score: number | null;
  coverage_status: CoverageStatus;
  community_conditions_group: number | null;
}

export interface MapScores {
  schema_version: 1;
  build_id: string;
  level: Geography;
  scope: { kind: string; state: string | null };
  rows: MapScore[];
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
