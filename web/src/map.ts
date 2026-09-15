import { zoomIdentity, type ZoomTransform } from "d3-zoom";
import type { Geography, MapScore, Metric } from "./types";

export const MAP_COLORS = {
  low: "#7fa87e",
  below: "#c4b04a",
  typical: "#d2a727",
  high: "#c56a42",
  highest: "#b14a3c",
} as const;

export type ScoreBand = keyof typeof MAP_COLORS;

export const COMMUNITY_GROUP_COLORS = [
  "#7fa87e", "#9eac67", "#bcaf50", "#c9ad3e", "#cfa92f",
  "#cf992d", "#c97e39", "#c36641", "#ba583f", "#b14a3c",
] as const;

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

export const MOUNTAIN_BAND_COLORS = [
  "#440154", "#482878", "#3e4989", "#31688e", "#26828e", "#1f9e89",
  "#35b779", "#6ece58", "#b5de2b", "#fde725", "#fff4a8",
] as const;

export const MOUNTAIN_COLORS = {
  low: MOUNTAIN_BAND_COLORS[0],
  below: MOUNTAIN_BAND_COLORS[2],
  typical: MOUNTAIN_BAND_COLORS[4],
  high: MOUNTAIN_BAND_COLORS[6],
  highest: MOUNTAIN_BAND_COLORS[8],
  summit: MOUNTAIN_BAND_COLORS[10],
} as const;

const COLOR_SCALE_SIZE = 256;

function colorScale(anchors: readonly string[]): readonly string[] {
  const channels = anchors.map((color) => [
    Number.parseInt(color.slice(1, 3), 16),
    Number.parseInt(color.slice(3, 5), 16),
    Number.parseInt(color.slice(5, 7), 16),
  ]);
  const positions = anchors.map((_, index) =>
    Math.round(index * (COLOR_SCALE_SIZE - 1) / (anchors.length - 1))
  );
  return Array.from({ length: COLOR_SCALE_SIZE }, (_, index) => {
    let right = 1;
    while (index > positions[right]) right += 1;
    const left = right - 1;
    const mix = (index - positions[left]) / (positions[right] - positions[left]);
    const rgb = channels[left].map((channel, offset) =>
      Math.round(channel + (channels[right][offset] - channel) * mix)
    );
    return `#${rgb.map((channel) => channel.toString(16).padStart(2, "0")).join("")}`;
  });
}

function gradient(colors: readonly string[]): string {
  return `linear-gradient(90deg, ${colors.map((color, index) =>
    `${color} ${index * 100 / colors.length}% ${(index + 1) * 100 / colors.length}%`
  ).join(", ")})`;
}

const HAZARD_ANCHORS = Object.values(MAP_COLORS);
export const HAZARD_COLOR_SCALE = colorScale(HAZARD_ANCHORS);
export const COMMUNITY_COLOR_SCALE = colorScale(COMMUNITY_GROUP_COLORS);
export const MOUNTAIN_COLOR_SCALE = MOUNTAIN_BAND_COLORS;
export const COUNTY_MOUNTAIN_COLOR_SCALE = MOUNTAIN_BAND_COLORS.slice(0, 9);
export const COST_OF_LIVING_COLOR_SCALE = colorScale(HAZARD_ANCHORS);
export const HOME_COSTS_COLOR_SCALE = colorScale([...HAZARD_ANCHORS].reverse());

export const METRIC_COLOR_SCALES = {
  "residential-hazard": {
    minimum: 0, maximum: 100, ticks: [0, 20, 40, 60, 80, 100],
    distinguishAt: [20, 40, 60, 80],
    colors: HAZARD_COLOR_SCALE, gradient: gradient(HAZARD_COLOR_SCALE),
  },
  "community-conditions": {
    minimum: 1, maximum: 10, ticks: [1, 3, 5, 7, 10],
    distinguishAt: [],
    colors: COMMUNITY_COLOR_SCALE, gradient: gradient(COMMUNITY_COLOR_SCALE),
  },
  mountain: {
    minimum: 0, maximum: 5, ticks: [0, 1, 2, 3, 4, 5],
    distinguishAt: [1, 2, 3, 4],
    colors: MOUNTAIN_COLOR_SCALE, gradient: gradient(MOUNTAIN_COLOR_SCALE.slice(0, -1)),
  },
  "cost-of-living": {
    minimum: 80, maximum: 120, ticks: [80, 90, 100, 110, 120],
    distinguishAt: [100],
    colors: COST_OF_LIVING_COLOR_SCALE, gradient: gradient(COST_OF_LIVING_COLOR_SCALE),
  },
  "home-costs": {
    minimum: 0, maximum: 100, ticks: [0, 20, 40, 60, 80, 100],
    distinguishAt: [],
    colors: HOME_COSTS_COLOR_SCALE, gradient: gradient(HOME_COSTS_COLOR_SCALE),
  },
} as const satisfies Record<Metric, {
  minimum: number;
  maximum: number;
  ticks: readonly number[];
  distinguishAt: readonly number[];
  colors: readonly string[];
  gradient: string;
}>;

export function metricColorScale(metric: Metric, level: Geography) {
  if (metric !== "mountain" || level === "tract") return METRIC_COLOR_SCALES[metric];
  return {
    minimum: 0,
    maximum: 4,
    ticks: [0, 1, 2, 3, 4] as const,
    distinguishAt: [1, 2, 3] as const,
    colors: COUNTY_MOUNTAIN_COLOR_SCALE,
    gradient: gradient(COUNTY_MOUNTAIN_COLOR_SCALE.slice(0, -1)),
  };
}

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

export function scoreBand(value: number | null): ScoreBand | null {
  if (value === null || !Number.isFinite(value) || value < 0 || value > 100) return null;
  const displayed = Number(value.toFixed(1));
  if (displayed < 20) return "low";
  if (displayed < 40) return "below";
  if (displayed < 60) return "typical";
  if (displayed < 80) return "high";
  return "highest";
}

export function scoreColor(value: number | null): string | null {
  return scaleColor(value, METRIC_COLOR_SCALES["residential-hazard"]);
}

export function communityGroupColor(value: number | null): string | null {
  return value !== null && Number.isInteger(value)
    ? scaleColor(value, METRIC_COLOR_SCALES["community-conditions"])
    : null;
}

export function mountainColor(value: number | null, level: Geography = "tract"): string | null {
  if (value === null || !Number.isFinite(value) || value < 0) return null;
  const maximum = level === "tract" ? 5 : 4;
  return MOUNTAIN_BAND_COLORS[Math.min(Math.floor(value * 2), maximum * 2)];
}

export function costOfLivingColor(value: number | null): string | null {
  if (value === null || !Number.isFinite(value)) return null;
  return scaleColor(Math.max(80, Math.min(120, value)), METRIC_COLOR_SCALES["cost-of-living"]);
}

export function homeBuyingPowerColor(value: number | null): string | null {
  return scaleColor(value, METRIC_COLOR_SCALES["home-costs"]);
}

function scaleColor(
  value: number | null,
  scale: {
    minimum: number;
    maximum: number;
    distinguishAt: readonly number[];
    colors: readonly string[];
  },
): string | null {
  const { minimum, maximum, colors } = scale;
  if (value === null || !Number.isFinite(value) || value < minimum || value > maximum) {
    return null;
  }
  let index = Math.round((value - minimum) / (maximum - minimum) * (colors.length - 1));
  for (const boundary of scale.distinguishAt) {
    const boundaryIndex = Math.round(
      (boundary - minimum) / (maximum - minimum) * (colors.length - 1),
    );
    if (value < boundary || index !== boundaryIndex) continue;
    const lowerColor = colors[index];
    while (index < colors.length - 1 && colors[index] === lowerColor) index += 1;
  }
  return colors[index];
}

export function metricColor(
  score: MapScore | null | undefined,
  metric: Metric,
  level: Geography = "tract",
): string | null {
  const value = metric === "residential-hazard" ? score?.res_hazard_npctl
    : metric === "community-conditions" ? score?.community_conditions_group
      : metric === "mountain" ? score?.mountain_magnitude
        : metric === "cost-of-living" ? score?.cost_of_living_index
          : score?.home_buying_power_percentile;
  return metricValueColor(value ?? null, metric, level);
}

export function metricValueColor(
  value: number | null,
  metric: Metric,
  level: Geography = "tract",
): string | null {
  if (metric === "community-conditions") return communityGroupColor(value);
  if (metric === "mountain") return mountainColor(value, level);
  if (metric === "cost-of-living") return costOfLivingColor(value);
  if (metric === "home-costs") return homeBuyingPowerColor(value);
  return scoreColor(value);
}

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
