import { describe, expect, it } from "vitest";
import {
  COUNTY_FIT_PRESETS, countyFitFiltersValid, countyFitParams, countyFitWeightsValid,
  decodeCountyFitSummary, EMPTY_COUNTY_FIT_FILTERS, readCountyFitHash,
} from "./countyFit";

const summary = {
  schema_version: 1,
  build_id: "build-1",
  methodology_id: "top-counties-v2",
  calibration_id: "calibration-1",
  view: "safety",
  preset: null,
  weights: { safety: 1, health: 0, affordability: 0, opportunity: 0, lifestyle: 0, family: 0 },
  gates: {},
  reference_count: 2,
  national_count: 2,
  cohort_count: 1,
  exclusions: { missing_active_data: 1 },
  notices: [],
  counties: {
    county_fips: ["01001", "02013"],
    name: ["Autauga", "Aleutians East"],
    state: ["AL", "AK"],
    active_value: [0.8, null],
    eligible: [true, false],
    exclusion_reason: [null, "missing_active_data"],
    national_rank: [1, null],
    filtered_rank: [1, null],
    pareto_optimal: [null, null],
    u_safety: [0.8, null],
    u_health: [0.5, 0.4],
    u_affordability: [0.4, 0.3],
    u_opportunity: [0.7, 0.2],
    u_lifestyle: [0.6, 0.1],
    u_family: [0.9, 0.8],
  },
} as const;

describe("County Fit public contract", () => {
  it("validates a sorted schema-1 county vector", () => {
    expect(decodeCountyFitSummary(summary, "build-1").counties.county_fips).toEqual([
      "01001", "02013",
    ]);
  });

  it("rejects stale, unsorted, and out-of-range vectors", () => {
    expect(() => decodeCountyFitSummary(summary, "build-2")).toThrow("stale");
    expect(() => decodeCountyFitSummary({
      ...summary,
      counties: { ...summary.counties, county_fips: ["02013", "01001"] },
    }, "build-1")).toThrow("IDs");
    expect(() => decodeCountyFitSummary({
      ...summary,
      counties: { ...summary.counties, active_value: [1.01, null] },
    }, "build-1")).toThrow("0–1");
  });

  it("uses exact preset/custom weights and query units", () => {
    const preset = countyFitParams("build-1", "custom", "balanced", COUNTY_FIT_PRESETS.balanced, {});
    expect(preset.has("weight_safety")).toBe(false);
    const custom = countyFitParams("build-1", "custom", "custom", COUNTY_FIT_PRESETS.balanced, {
      min_safety: 0.75,
      exclude_appalachia: true,
    });
    expect(custom.get("weight_safety")).toBe("0.20");
    expect(custom.get("min_safety")).toBe("0.75");
    expect(custom.get("exclude_appalachia")).toBe("true");
    expect(countyFitWeightsValid({
      ...COUNTY_FIT_PRESETS.balanced, safety: 20.5, health: 14.5,
    })).toBe(false);
    expect(() => countyFitParams("build-1", "custom", "custom", {
      ...COUNTY_FIT_PRESETS.balanced, safety: 20.5, health: 14.5,
    }, {})).toThrow("integer percentages");
  });

  it("round-trips valid custom hash state and rejects malformed state", () => {
    const weights = "safety=20&health=15&affordability=25&opportunity=15&lifestyle=15&family=10"
      .split("&").map((item) => item.split("=")).map(([key, value]) => `fit_weight_${key}=${value}`).join("&");
    const parsed = readCountyFitHash(`#workspace=county-fit&fit_view=custom&fit_preset=custom&${weights}&fit_state=CO&fit_place=08013&fit_cx=.4&fit_cy=.6&fit_z=3`);
    expect(parsed?.weights).toEqual(COUNTY_FIT_PRESETS.balanced);
    expect(parsed?.selected).toBe("08013");
    expect(parsed?.camera.z).toBe(3);
    expect(readCountyFitHash("#workspace=county-fit&fit_view=nope")).toBeNull();
    expect(readCountyFitHash("#workspace=county-fit&fit_view=custom&fit_preset=custom")).toBeNull();
  });

  it("rejects invalid filter state instead of issuing a bad API request", () => {
    expect(readCountyFitHash("#workspace=county-fit&fit_state=ZZ")).toBeNull();
    expect(readCountyFitHash("#workspace=county-fit&fit_min_population=-1")).toBeNull();
    expect(readCountyFitHash("#workspace=county-fit&fit_min_population=25000.5")).toBeNull();
    expect(readCountyFitHash("#workspace=county-fit&fit_min_active_listings=100.5")).toBeNull();
    expect(readCountyFitHash("#workspace=county-fit&fit_min_valid_months=13")).toBeNull();
    expect(readCountyFitHash("#workspace=county-fit&fit_min_safety=101")).toBeNull();
    expect(readCountyFitHash("#workspace=county-fit&fit_min_safety=20.5")).toBeNull();
    expect(readCountyFitHash("#workspace=county-fit&fit_min_jan_temp_f=50&fit_max_jan_temp_f=40")).toBeNull();
    expect(countyFitFiltersValid({
      ...EMPTY_COUNTY_FIT_FILTERS, min_population: "25000.5",
    })).toBe(false);
    expect(countyFitFiltersValid({
      ...EMPTY_COUNTY_FIT_FILTERS, min_population: "25000", min_safety: "20",
    })).toBe(true);
  });
});
