import { zoomIdentity, type ZoomTransform } from "d3-zoom";
import type { Geography, MapManifest, MapScore, Metric } from "./types";

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

export const MOUNTAIN_COLORS = {
  low: "#59676c",
  below: "#73806d",
  typical: "#929271",
  high: "#b49b69",
  highest: "#d9bd76",
} as const;

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
  const band = scoreBand(value);
  return band ? MAP_COLORS[band] : null;
}

export function communityGroupColor(value: number | null): string | null {
  return value !== null && Number.isInteger(value) && value >= 1 && value <= 10
    ? COMMUNITY_GROUP_COLORS[value - 1]
    : null;
}

export function metricColor(score: MapScore | null | undefined, metric: Metric): string | null {
  if (metric === "community-conditions") {
    return communityGroupColor(score?.community_conditions_group ?? null);
  }
  const band = scoreBand(metric === "mountain" ? score?.mountain_score ?? null : score?.risk_score ?? null);
  return band ? (metric === "mountain" ? MOUNTAIN_COLORS : MAP_COLORS)[band] : null;
}

export function scoreMap(rows: MapScore[]): Map<string, MapScore> {
  return new Map(rows.map((row) => [row.place_id, row]));
}

export function nationalAsset(manifest: MapManifest, level: Geography | "state") {
  return manifest.files.find((asset) => asset.level === level && asset.lod === "national");
}

export function detailAsset(manifest: MapManifest, state: string) {
  return manifest.files.find(
    (asset) => asset.level === "tract" && asset.lod === "detail" && asset.jurisdiction === state,
  );
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
    ? metricValue
    : "fema";
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
  return {
    level,
    metric,
    state,
    county: level === "tract" ? county : "",
    place,
    unranked: params.get("unranked") === "1",
    mountainMin: params.has("mountain_min")
      ? number("mountain_min", 0, 0, 100)
      : null,
    camera: {
      cx: number("cx", 0.5, 0, 1),
      cy: number("cy", 0.5, 0, 1),
      z: number("z", 1, 1, 12),
    },
  };
}
