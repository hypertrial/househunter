import type {
  CountyFitColumns, CountyFitPillar, CountyFitSummary, CountyFitView,
} from "./types";

export const COUNTY_FIT_PILLARS: CountyFitPillar[] = [
  "safety", "health", "affordability", "opportunity", "lifestyle", "family",
];

export const COUNTY_FIT_VIEWS: Array<{ key: CountyFitView; label: string }> = [
  { key: "safety", label: "Safety" },
  { key: "health", label: "Health" },
  { key: "affordability", label: "Affordability" },
  { key: "opportunity", label: "Opportunity" },
  { key: "lifestyle", label: "Lifestyle" },
  { key: "family", label: "Family Autonomy" },
  { key: "custom", label: "Custom Fit" },
];

export const COUNTY_FIT_PRESETS: Record<string, Record<CountyFitPillar, number>> = {
  balanced: { safety: 20, health: 15, affordability: 25, opportunity: 15, lifestyle: 15, family: 10 },
  "safety-health": { safety: 35, health: 25, affordability: 15, opportunity: 10, lifestyle: 5, family: 10 },
  affordability: { safety: 15, health: 10, affordability: 45, opportunity: 15, lifestyle: 5, family: 10 },
  "mountain-lifestyle": { safety: 10, health: 10, affordability: 15, opportunity: 10, lifestyle: 45, family: 10 },
};

const COUNTY_FIT_STATES = new Set([
  "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
  "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
  "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
  "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
]);

export const EMPTY_COUNTY_FIT_FILTERS = {
  state: "",
  exclude_appalachia: false,
  min_population: "",
  min_active_listings: "",
  min_valid_months: "",
  min_jan_temp_f: "",
  max_jan_temp_f: "",
  min_jul_temp_f: "",
  max_jul_temp_f: "",
  max_extreme_heat_days: "",
  max_extreme_cold_days: "",
  min_safety: "",
  min_health: "",
  min_affordability: "",
  min_opportunity: "",
  min_lifestyle: "",
  min_family: "",
};

export type CountyFitFilters = typeof EMPTY_COUNTY_FIT_FILTERS;

export function countyFitWeightsValid(weights: Record<CountyFitPillar, number>): boolean {
  return COUNTY_FIT_PILLARS.every((pillar) => Number.isInteger(weights[pillar])
    && weights[pillar] >= 0 && weights[pillar] <= 100)
    && Object.values(weights).reduce((total, value) => total + value, 0) === 100;
}

export function countyFitFiltersValid(filters: CountyFitFilters): boolean {
  if (filters.state && !COUNTY_FIT_STATES.has(filters.state)) return false;
  const value = (key: keyof CountyFitFilters) => {
    const raw = filters[key];
    return typeof raw === "string" && raw !== "" ? Number(raw) : null;
  };
  const integerRanges = [
    ["min_population", 0, Number.MAX_SAFE_INTEGER],
    ["min_active_listings", 0, Number.MAX_SAFE_INTEGER],
    ["min_valid_months", 1, 12],
  ] as const;
  if (integerRanges.some(([key, minimum, maximum]) => {
    const number = value(key);
    return number !== null && (!Number.isInteger(number) || number < minimum || number > maximum);
  })) return false;
  for (const pillar of COUNTY_FIT_PILLARS) {
    const number = value(`min_${pillar}` as keyof CountyFitFilters);
    if (number !== null && (!Number.isInteger(number) || number < 0 || number > 100)) return false;
  }
  for (const key of [
    "min_jan_temp_f", "max_jan_temp_f", "min_jul_temp_f", "max_jul_temp_f",
    "max_extreme_heat_days", "max_extreme_cold_days",
  ] as const) {
    const number = value(key);
    if (number !== null && (!Number.isFinite(number)
      || (key.startsWith("max_extreme") && number < 0))) return false;
  }
  for (const [minimum, maximum] of [
    ["min_jan_temp_f", "max_jan_temp_f"],
    ["min_jul_temp_f", "max_jul_temp_f"],
  ] as const) {
    const low = value(minimum);
    const high = value(maximum);
    if (low !== null && high !== null && low > high) return false;
  }
  return true;
}

export interface CountyFitHashState {
  view: CountyFitView;
  preset: string;
  weights: Record<CountyFitPillar, number>;
  filters: CountyFitFilters;
  selected: string;
  camera: { cx: number; cy: number; z: number };
}

export function readCountyFitHash(hash: string): CountyFitHashState | null {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  if (params.get("workspace") !== "county-fit") return null;
  const requestedView = params.get("fit_view") || "custom";
  const view = COUNTY_FIT_VIEWS.find(({ key }) => key === requestedView)?.key;
  if (!view) return null;
  const requestedPreset = params.get("fit_preset") || "balanced";
  if (!(requestedPreset in COUNTY_FIT_PRESETS) && requestedPreset !== "custom") return null;
  const weights = { ...COUNTY_FIT_PRESETS[requestedPreset === "custom" ? "balanced" : requestedPreset] };
  if (requestedPreset === "custom") {
    for (const pillar of COUNTY_FIT_PILLARS) {
      const raw = params.get(`fit_weight_${pillar}`);
      const value = raw === null ? Number.NaN : Number(raw);
      weights[pillar] = value;
    }
    if (!countyFitWeightsValid(weights)) return null;
  }
  const filters = { ...EMPTY_COUNTY_FIT_FILTERS };
  filters.state = params.get("fit_state") || "";
  filters.exclude_appalachia = params.get("fit_exclude_appalachia") === "1";
  for (const key of Object.keys(filters) as Array<keyof CountyFitFilters>) {
    if (key === "state" || key === "exclude_appalachia") continue;
    const raw = params.get(`fit_${key}`) || "";
    (filters[key] as string) = raw;
  }
  if (!countyFitFiltersValid(filters)) return null;
  const selected = params.get("fit_place") || "";
  if (selected && !/^\d{5}$/.test(selected)) return null;
  const bounded = (key: string, fallback: number, min: number, max: number) => {
    const raw = params.get(key);
    if (raw === null) return fallback;
    const value = Number(raw);
    return Number.isFinite(value) && value >= min && value <= max ? value : fallback;
  };
  return {
    view,
    preset: requestedPreset,
    weights,
    filters,
    selected,
    camera: {
      cx: bounded("fit_cx", 0.5, 0, 1),
      cy: bounded("fit_cy", 0.5, 0, 1),
      z: bounded("fit_z", 1, 1, 12),
    },
  };
}

const numberOrNull = (value: unknown): value is number | null =>
  value === null || (typeof value === "number" && Number.isFinite(value));

const integerOrNull = (value: unknown): value is number | null =>
  value === null || (typeof value === "number" && Number.isInteger(value) && value > 0);

export function decodeCountyFitSummary(value: unknown, expectedBuildId: string): CountyFitSummary {
  if (!value || typeof value !== "object") throw new Error("County Fit schema is invalid");
  const payload = value as Partial<CountyFitSummary>;
  if (payload.schema_version !== 1 || !payload.counties) {
    throw new Error("County Fit schema is unsupported");
  }
  if (payload.build_id !== expectedBuildId) throw new Error("County Fit build is stale");
  const columns = payload.counties as Partial<CountyFitColumns>;
  const values = Object.values(columns);
  if (!values.length || !values.every(Array.isArray)
    || new Set(values.map((column) => column.length)).size !== 1) {
    throw new Error("County Fit column length mismatch");
  }
  const required = [
    "county_fips", "name", "state", "active_value", "eligible", "exclusion_reason",
    "national_rank", "filtered_rank", "pareto_optimal",
    ...COUNTY_FIT_PILLARS.map((pillar) => `u_${pillar}`),
  ] as Array<keyof CountyFitColumns>;
  if (!required.every((key) => Array.isArray(columns[key]))) {
    throw new Error("County Fit columns are invalid");
  }
  const count = columns.county_fips!.length;
  for (let index = 0; index < count; index += 1) {
    const fips = columns.county_fips![index];
    if (!/^\d{5}$/.test(fips) || (index > 0 && columns.county_fips![index - 1] >= fips)) {
      throw new Error("County Fit county IDs are invalid");
    }
    if (typeof columns.name![index] !== "string" || !/^[A-Z]{2}$/.test(columns.state![index])) {
      throw new Error("County Fit identity columns are invalid");
    }
    if (typeof columns.eligible![index] !== "boolean"
      || !(columns.exclusion_reason![index] === null || typeof columns.exclusion_reason![index] === "string")
      || !integerOrNull(columns.national_rank![index])
      || !integerOrNull(columns.filtered_rank![index])
      || !(columns.pareto_optimal![index] === null || typeof columns.pareto_optimal![index] === "boolean")) {
      throw new Error("County Fit status columns are invalid");
    }
    for (const key of ["active_value", ...COUNTY_FIT_PILLARS.map((pillar) => `u_${pillar}`)] as Array<keyof CountyFitColumns>) {
      const item = (columns[key] as Array<unknown>)[index];
      if (!numberOrNull(item) || (item !== null && (item < 0 || item > 1))) {
        throw new Error("County Fit utility is outside 0–1");
      }
    }
  }
  if (!COUNTY_FIT_VIEWS.some(({ key }) => key === payload.view)) {
    throw new Error("County Fit view is invalid");
  }
  return payload as CountyFitSummary;
}

export function countyFitParams(
  buildId: string,
  view: CountyFitView,
  preset: string,
  weights: Record<CountyFitPillar, number>,
  filters: Record<string, string | number | boolean | null>,
): URLSearchParams {
  const params = new URLSearchParams({ build_id: buildId, view, preset });
  if (view === "custom" && preset === "custom") {
    if (!countyFitWeightsValid(weights)) {
      throw new Error("Custom Fit weights must be integer percentages totaling 100");
    }
    for (const pillar of COUNTY_FIT_PILLARS) {
      params.set(`weight_${pillar}`, (weights[pillar] * 0.01).toFixed(2));
    }
  }
  for (const [key, value] of Object.entries(filters)) {
    if (value === null || value === "" || value === false) continue;
    params.set(key, String(value));
  }
  return params;
}

export function countyFitRows(summary: CountyFitSummary) {
  return summary.counties.county_fips.map((fips, index) => ({
    fips,
    name: summary.counties.name[index],
    state: summary.counties.state[index],
    activeValue: summary.counties.active_value[index],
    eligible: summary.counties.eligible[index],
    exclusionReason: summary.counties.exclusion_reason[index],
    nationalRank: summary.counties.national_rank[index],
    filteredRank: summary.counties.filtered_rank[index],
    paretoOptimal: summary.counties.pareto_optimal[index],
  }));
}
