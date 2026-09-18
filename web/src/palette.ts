import type { Geography, MapMetric, MapScore, Metric } from "./types";

export const SURFACE = {
  bg: "#10191e",
  noData: "#344149",
  hatchBg: "#3b474e",
  hatchFg: "#7a8589",
  hatchFallback: "#566169",
  stroke: "rgba(9,15,18,.54)",
  strokeFiltered: "#233038",
  stateOutline: "rgba(223,231,230,.52)",
  stateLabel: "rgba(231,238,236,.72)",
  selection: "#f7f4e8",
} as const;

export const RAMPS = {
  concern: [
    "#a1e7e2", "#98dcbe", "#b2cd84", "#d6b858", "#e79c27",
    "#ee7b00", "#ec5a00", "#e13a01", "#d42320", "#c2152f",
  ],
  magnitude: [
    "#3b528b", "#2c728e", "#21918c", "#1fa088", "#28ae80",
    "#3fbc73", "#5ec962", "#84d44b", "#addc30", "#d8e219",
  ],
  fit: ["#5e6b73", "#56899a", "#4dabb8", "#56d1c8", "#7df5d2"],
} as const;

export const MAGNITUDE_CAP = "#f2f5f7";

export const UI = {
  text: "#edf2ef",
  textMuted: "#b9c7c2",
  textFaint: "#849692",
  textSoft: "#aebdb8",
  numeric: "#eadfb8",
  accent: "#eadfb8",
  accentFit: RAMPS.fit[3],
  focus: "#f3c94f",
  success: "#8fd9c3",
  warning: RAMPS.concern[4],
  danger: "#e06a5a",
  dangerText: "#ffd6cc",
  dangerSurface: "rgba(140,62,46,.25)",
  dangerBorder: "#8e4d41",
  surface1: "#0d1519",
  surface2: "#111b20",
  surface3: "#151f24",
  surfaceInput: "#0d171c",
  panel: "rgba(20,31,37,.96)",
  panelDock: "rgba(17,27,32,.90)",
  border: "rgba(225,235,232,.16)",
  borderStrong: "rgba(225,235,232,.18)",
  borderSoft: "rgba(222,233,230,.14)",
  hover: "rgba(218,230,226,.10)",
  primary: "#d8e4da",
  primaryText: "#101816",
  secondary: "#35444b",
  shadow: "rgba(0,0,0,.38)",
  shadowSoft: "rgba(0,0,0,.22)",
  vignette: "rgba(2,7,10,.28)",
  white18: "rgba(255,255,255,.18)",
  white90: "rgba(255,255,255,.90)",
  zoomBg: "rgba(23,34,40,.92)",
  errorCard: "rgba(37,25,25,.96)",
  confirmSurface: "rgba(111,88,32,.18)",
  confirmBorder: "#7a6936",
  pillSurface: "rgba(66,104,83,.15)",
  pillBorder: "rgba(153,181,166,.28)",
  readinessSurface: "rgba(55,46,23,.92)",
  readinessBorder: "rgba(210,167,39,.35)",
  valid: "#cde0ce",
  validSurface: "rgba(82,132,91,.2)",
  inputBorder: "#53636a",
} as const;

export type RampName = keyof typeof RAMPS;
export type Polarity = "worse-high" | "better-high" | "neutral";
export type ScoreBand = "low" | "below" | "typical" | "high" | "highest";

const SCORE_BANDS = ["low", "below", "typical", "high", "highest"] as const;

export interface LayerStyle {
  ramp: RampName;
  polarity: Polarity;
  domain: readonly [number, number];
  ticks: readonly number[];
  classes: number;
  stops: readonly number[];
  cap?: string;
}

export const LAYER_STYLE = {
  "residential-hazard": {
    ramp: "concern",
    polarity: "worse-high",
    domain: [0, 100],
    ticks: [0, 20, 40, 60, 80, 100],
    classes: 5,
    stops: [0, 3, 5, 7, 9],
  },
  "community-conditions": {
    ramp: "concern",
    polarity: "worse-high",
    domain: [1, 10],
    ticks: [1, 3, 5, 7, 10],
    classes: 10,
    stops: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
  },
  mountain: {
    ramp: "magnitude",
    polarity: "neutral",
    domain: [0, 5],
    ticks: [0, 1, 2, 3, 4, 5],
    classes: 10,
    stops: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    cap: MAGNITUDE_CAP,
  },
  "cost-of-living": {
    ramp: "concern",
    polarity: "worse-high",
    domain: [80, 120],
    ticks: [80, 90, 100, 110, 120],
    classes: 8,
    stops: [0, 1, 3, 4, 5, 6, 8, 9],
  },
  "home-costs": {
    ramp: "concern",
    polarity: "better-high",
    domain: [0, 100],
    ticks: [0, 20, 40, 60, 80, 100],
    classes: 5,
    stops: [9, 7, 5, 3, 0],
  },
  "county-fit": {
    ramp: "fit",
    polarity: "better-high",
    domain: [0, 1],
    ticks: [0, 0.2, 0.4, 0.6, 0.8, 1],
    classes: 5,
    stops: [0, 1, 2, 3, 4],
  },
} as const satisfies Record<MapMetric, LayerStyle>;

const COUNTY_MOUNTAIN_STYLE = {
  ...LAYER_STYLE.mountain,
  domain: [0, 4] as const,
  ticks: [0, 1, 2, 3, 4] as const,
  classes: 8,
  stops: [0, 1, 2, 3, 4, 5, 6, 7] as const,
} satisfies LayerStyle;

export function layerStyle(metric: MapMetric, level: Geography = "tract"): LayerStyle {
  if (metric === "mountain" && level === "county") return COUNTY_MOUNTAIN_STYLE;
  return LAYER_STYLE[metric];
}

export function layerClass(
  value: number | null,
  metric: MapMetric,
  level: Geography = "tract",
): number | null {
  const style = layerStyle(metric, level);
  if (value === null || !Number.isFinite(value)) return null;
  if (metric === "community-conditions") {
    return Number.isInteger(value) && value >= 1 && value <= 10 ? value - 1 : null;
  }
  if (metric === "mountain") {
    if (value < 0) return null;
    const maximum = style.domain[1];
    if (value >= maximum) return style.classes;
    return Math.min(style.classes - 1, Math.floor(value * 2));
  }
  if (metric === "cost-of-living") {
    const clamped = Math.max(style.domain[0], Math.min(style.domain[1], value));
    const width = (style.domain[1] - style.domain[0]) / style.classes;
    return Math.min(style.classes - 1, Math.floor((clamped - style.domain[0]) / width));
  }
  const displayed = metric === "residential-hazard" ? Number(value.toFixed(1)) : value;
  if (displayed < style.domain[0] || displayed > style.domain[1]) return null;
  const span = style.domain[1] - style.domain[0];
  const unit = displayed - style.domain[0];
  return Math.min(style.classes - 1, Math.floor((unit / span) * style.classes));
}

export function layerColor(
  value: number | null,
  metric: MapMetric,
  level: Geography = "tract",
): string | null {
  const klass = layerClass(value, metric, level);
  if (klass === null) return null;
  const style = layerStyle(metric, level);
  if (style.cap && klass >= style.classes) return style.cap;
  return RAMPS[style.ramp][style.stops[klass]];
}

export function classColors(metric: MapMetric, level: Geography = "tract"): readonly string[] {
  const style = layerStyle(metric, level);
  const colors = style.stops.map((stop) => RAMPS[style.ramp][stop]);
  return style.cap ? [...colors, style.cap] : colors;
}

export function legendColors(metric: MapMetric, level: Geography = "tract"): readonly string[] {
  const style = layerStyle(metric, level);
  return style.stops.map((stop) => RAMPS[style.ramp][stop]);
}

export function gradient(colors: readonly string[]): string {
  return `linear-gradient(90deg, ${colors.map((color, index) =>
    `${color} ${index * 100 / colors.length}% ${(index + 1) * 100 / colors.length}%`
  ).join(", ")})`;
}

export interface ColorScale {
  minimum: number;
  maximum: number;
  ticks: readonly number[];
  colors: readonly string[];
  gradient: string;
  classes: number;
}

export function metricColorScale(metric: MapMetric, level: Geography = "tract"): ColorScale {
  const style = layerStyle(metric, level);
  const colors = legendColors(metric, level);
  return {
    minimum: style.domain[0],
    maximum: style.domain[1],
    ticks: style.ticks,
    colors,
    gradient: gradient(colors),
    classes: style.classes,
  };
}

export const MAP_COLORS = {
  low: RAMPS.concern[0],
  below: RAMPS.concern[3],
  typical: RAMPS.concern[5],
  high: RAMPS.concern[7],
  highest: RAMPS.concern[9],
} as const;

export const COMMUNITY_GROUP_COLORS = RAMPS.concern;
export const MOUNTAIN_BAND_COLORS = [...RAMPS.magnitude, MAGNITUDE_CAP] as const;
export const MOUNTAIN_COLORS = {
  low: MOUNTAIN_BAND_COLORS[0],
  below: MOUNTAIN_BAND_COLORS[2],
  typical: MOUNTAIN_BAND_COLORS[4],
  high: MOUNTAIN_BAND_COLORS[6],
  highest: MOUNTAIN_BAND_COLORS[8],
  summit: MAGNITUDE_CAP,
} as const;

export const HAZARD_COLOR_SCALE = classColors("residential-hazard");
export const COMMUNITY_COLOR_SCALE = classColors("community-conditions");
export const MOUNTAIN_COLOR_SCALE = classColors("mountain", "tract");
export const COUNTY_MOUNTAIN_COLOR_SCALE = classColors("mountain", "county");
export const COST_OF_LIVING_COLOR_SCALE = classColors("cost-of-living");
export const HOME_COSTS_COLOR_SCALE = classColors("home-costs");
export const COUNTY_FIT_COLOR_SCALE = classColors("county-fit");
export const COUNTY_FIT_GRADIENT = metricColorScale("county-fit").gradient;

export const METRIC_COLOR_SCALES = {
  "residential-hazard": metricColorScale("residential-hazard"),
  "community-conditions": metricColorScale("community-conditions"),
  mountain: metricColorScale("mountain", "tract"),
  "cost-of-living": metricColorScale("cost-of-living"),
  "home-costs": metricColorScale("home-costs"),
} as const satisfies Record<Metric, ColorScale>;

export function scoreBand(value: number | null): ScoreBand | null {
  const klass = layerClass(value, "residential-hazard");
  return klass === null ? null : SCORE_BANDS[klass];
}

export function scoreColor(value: number | null): string | null {
  return layerColor(value, "residential-hazard");
}

export function communityGroupColor(value: number | null): string | null {
  return layerColor(value, "community-conditions");
}

export function mountainColor(value: number | null, level: Geography = "tract"): string | null {
  return layerColor(value, "mountain", level);
}

export function costOfLivingColor(value: number | null): string | null {
  return layerColor(value, "cost-of-living");
}

export function homeBuyingPowerColor(value: number | null): string | null {
  return layerColor(value, "home-costs");
}

export function countyFitColor(value: number | null): string | null {
  return layerColor(value, "county-fit");
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
  return layerColor(value, metric, level);
}

export const CSS_VARIABLES: Record<string, string> = {
  "--bg": SURFACE.bg,
  "--no-data": SURFACE.noData,
  "--hatch-bg": SURFACE.hatchBg,
  "--hatch-fg": SURFACE.hatchFg,
  "--hatch-fallback": SURFACE.hatchFallback,
  "--stroke": SURFACE.stroke,
  "--stroke-filtered": SURFACE.strokeFiltered,
  "--state-outline": SURFACE.stateOutline,
  "--state-label": SURFACE.stateLabel,
  "--selection": SURFACE.selection,
  "--text": UI.text,
  "--text-muted": UI.textMuted,
  "--text-faint": UI.textFaint,
  "--text-soft": UI.textSoft,
  "--numeric": UI.numeric,
  "--accent": UI.accent,
  "--accent-fit": UI.accentFit,
  "--focus": UI.focus,
  "--success": UI.success,
  "--warning": UI.warning,
  "--danger": UI.danger,
  "--danger-text": UI.dangerText,
  "--danger-surface": UI.dangerSurface,
  "--danger-border": UI.dangerBorder,
  "--surface-1": UI.surface1,
  "--surface-2": UI.surface2,
  "--surface-3": UI.surface3,
  "--surface-input": UI.surfaceInput,
  "--panel": UI.panel,
  "--panel-dock": UI.panelDock,
  "--border": UI.border,
  "--border-strong": UI.borderStrong,
  "--border-soft": UI.borderSoft,
  "--hover": UI.hover,
  "--primary": UI.primary,
  "--primary-text": UI.primaryText,
  "--secondary": UI.secondary,
  "--shadow": UI.shadow,
  "--shadow-soft": UI.shadowSoft,
  "--vignette": UI.vignette,
  "--white-18": UI.white18,
  "--white-90": UI.white90,
  "--zoom-bg": UI.zoomBg,
  "--error-card": UI.errorCard,
  "--confirm-surface": UI.confirmSurface,
  "--confirm-border": UI.confirmBorder,
  "--pill-surface": UI.pillSurface,
  "--pill-border": UI.pillBorder,
  "--readiness-surface": UI.readinessSurface,
  "--readiness-border": UI.readinessBorder,
  "--valid": UI.valid,
  "--valid-surface": UI.validSurface,
  "--input-border": UI.inputBorder,
};

for (const [name, stops] of Object.entries(RAMPS)) {
  stops.forEach((color, index) => {
    CSS_VARIABLES[`--ramp-${name}-${index}`] = color;
  });
}
CSS_VARIABLES["--ramp-magnitude-cap"] = MAGNITUDE_CAP;

export function applyPaletteCssVariables(root: HTMLElement): void {
  for (const [name, value] of Object.entries(CSS_VARIABLES)) {
    root.style.setProperty(name, value);
  }
}
