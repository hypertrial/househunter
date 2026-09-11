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
  query: string;
  matched_address: string;
  tract_id: string;
  detail: PlaceDetail;
}

export interface JobStatus {
  job_id: string;
  state: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  progress: number;
  message: string;
  error: string | null;
}
