import { zoomIdentity, type ZoomTransform } from "d3-zoom";
import type { Geography, Metric } from "./types";

export {
  COMMUNITY_COLOR_SCALE,
  COMMUNITY_GROUP_COLORS,
  communityGroupColor,
  COST_OF_LIVING_COLOR_SCALE,
  costOfLivingColor,
  COUNTY_FIT_COLOR_SCALE,
  COUNTY_FIT_GRADIENT,
  COUNTY_MOUNTAIN_COLOR_SCALE,
  countyFitColor,
  HAZARD_COLOR_SCALE,
  HOME_COSTS_COLOR_SCALE,
  homeBuyingPowerColor,
  layerColor,
  MAP_COLORS,
  METRIC_COLOR_SCALES,
  metricColor,
  metricColorScale,
  metricValueColor,
  MOUNTAIN_BAND_COLORS,
  MOUNTAIN_COLOR_SCALE,
  MOUNTAIN_COLORS,
  mountainColor,
  scoreBand,
  scoreColor,
  type ScoreBand,
} from "./palette";

export const METRIC_UI = {
  "residential-hazard": {
    name: "Residential Hazard Exposure",
    source: "HouseHunter / FEMA NRI",
    descriptorKey: "residential-hazard",
  },
  "community-conditions": { name: "Community Conditions", source: "CHR&R", descriptorKey: "community-conditions" },
  mountain: { name: "Mountain Magnitude", source: "HouseHunter", descriptorKey: "mountain" },
  "cost-of-living": { name: "Cost of Living", source: "BEA RPP", descriptorKey: "cost-of-living" },
  "home-costs": { name: "Home Costs", source: "Realtor.com / ACS", descriptorKey: "home-costs" },
} as const satisfies Record<Metric, { name: string; source: string; descriptorKey: string }>;

export const STATE_FIPS = {
  AK: "02", AL: "01", AR: "05", AS: "60", AZ: "04", CA: "06", CO: "08", CT: "09",
  DC: "11", DE: "10", FL: "12", GA: "13", GU: "66", HI: "15", IA: "19", ID: "16",
  IL: "17", IN: "18", KS: "20", KY: "21", LA: "22", MA: "25", MD: "24", ME: "23",
  MI: "26", MN: "27", MO: "29", MP: "69", MS: "28", MT: "30", NC: "37", ND: "38",
  NE: "31", NH: "33", NJ: "34", NM: "35", NV: "32", NY: "36", OH: "39", OK: "40",
  OR: "41", PA: "42", PR: "72", RI: "44", SC: "45", SD: "46", TN: "47", TX: "48",
  UT: "49", VA: "51", VI: "78", VT: "50", WA: "53", WI: "55", WV: "54", WY: "56",
} as const;

export const STATE_ABBREVIATIONS = Object.keys(STATE_FIPS).sort() as Array<keyof typeof STATE_FIPS>;

export interface CameraState {
  cx: number;
  cy: number;
  z: number;
}

interface TransformLike {
  k: number;
  x: number;
  y: number;
}

export function relativeTransform(current: TransformLike, rendered: TransformLike): TransformLike {
  const k = current.k / rendered.k;
  return {
    k,
    x: current.x - k * rendered.x,
    y: current.y - k * rendered.y,
  };
}

export function cameraFromTransform(transform: ZoomTransform, width: number, height: number): CameraState {
  return {
    cx: Math.max(0, Math.min(1, (width / 2 - transform.x) / transform.k / width)),
    cy: Math.max(0, Math.min(1, (height / 2 - transform.y) / transform.k / height)),
    z: Math.max(1, Math.min(12, transform.k)),
  };
}

export function transformFromCamera(camera: CameraState, width: number, height: number): ZoomTransform {
  return zoomIdentity
    .translate(width / 2 - camera.cx * width * camera.z, height / 2 - camera.cy * height * camera.z)
    .scale(camera.z);
}

export function readHash(hash: string) {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const level: Geography = params.get("level") === "county" ? "county" : "tract";
  const metricValue = params.get("metric");
  const metric: Metric = metricValue === "community-conditions" || metricValue === "mountain"
    || metricValue === "cost-of-living" || metricValue === "home-costs"
    || metricValue === "residential-hazard" ? metricValue : "residential-hazard";
  const requestedState = params.get("state") || "";
  const state = requestedState in STATE_FIPS
    ? requestedState as keyof typeof STATE_FIPS
    : "";
  const requestedCounty = params.get("county") || "";
  const county = level === "tract" && state && /^\d{5}$/.test(requestedCounty)
    && requestedCounty.startsWith(STATE_FIPS[state]) ? requestedCounty : "";
  const placePattern = level === "county" ? /^\d{5}$/ : /^\d{11}$/;
  const requestedPlace = params.get("place") || "";
  const place = placePattern.test(requestedPlace)
    && (!state || requestedPlace.startsWith(STATE_FIPS[state]))
    && (!county || requestedPlace.startsWith(county)) ? requestedPlace : "";
  const number = (name: string, fallback: number, min: number, max: number) => {
    const raw = params.get(name);
    if (raw === null || raw.trim() === "") return fallback;
    const value = Number(raw);
    return Number.isFinite(value) ? Math.max(min, Math.min(max, value)) : fallback;
  };
  const optionalNumber = (name: string, min: number, max = Number.POSITIVE_INFINITY) => {
    const raw = params.get(name);
    if (raw === null || raw.trim() === "") return null;
    const value = Number(raw);
    return Number.isFinite(value) && value >= min && value <= max ? value : null;
  };
  return {
    level,
    metric,
    state,
    county: level === "tract" ? county : "",
    place,
    unranked: params.get("unranked") === "1",
    mountainMagnitudeMin: optionalNumber("mountain_magnitude_min", 0),
    communityConditionsGroupMax: (() => {
      const value = optionalNumber("max_community_conditions_group", 1, 10);
      return value !== null && Number.isInteger(value) ? value : null;
    })(),
    costOfLivingIndexMax: optionalNumber("cost_of_living_index_max", 0),
    homeSqftFor1mMin: optionalNumber("home_sqft_for_1m_min", 0),
    housingBuilt2000PlusPctMin: optionalNumber("housing_built_2000_plus_pct_min", 0, 100),
    camera: {
      cx: number("cx", 0.5, 0, 1),
      cy: number("cy", 0.5, 0, 1),
      z: number("z", 1, 1, 12),
    },
  };
}
