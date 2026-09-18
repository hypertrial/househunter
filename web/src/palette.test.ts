import { describe, expect, it } from "vitest";
import appSource from "./App.tsx?raw";
import countyFitSource from "./CountyFitWorkspace.tsx?raw";
import cssSource from "./styles.css?raw";
import workerSource from "./mapRenderer.worker.ts?raw";
import {
  applyPaletteCssVariables,
  classColors,
  CSS_VARIABLES,
  LAYER_STYLE,
  layerClass,
  layerColor,
  layerStyle,
  MAGNITUDE_CAP,
  RAMPS,
  scoreBand,
  SURFACE,
} from "./palette";
import type { MapMetric } from "./types";

const judgementLayers = [
  { key: "residential-hazard", polarity: "worse-high", direction: "higher" },
  { key: "community-conditions", polarity: "worse-high", direction: "lower" },
  { key: "cost-of-living", polarity: "worse-high", direction: "lower" },
  { key: "home-costs", polarity: "better-high", direction: "higher" },
] as const;

function hexLightness(hex: string): number {
  const channels = [1, 3, 5].map((index) => {
    const value = Number.parseInt(hex.slice(index, index + 2), 16) / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  const [l, m, s] = [
    0.4122214708 * channels[0] + 0.5363325363 * channels[1] + 0.0514459929 * channels[2],
    0.2119034982 * channels[0] + 0.6806995451 * channels[1] + 0.1073969566 * channels[2],
    0.0883024619 * channels[0] + 0.2817188376 * channels[1] + 0.6299787005 * channels[2],
  ].map((value) => Math.cbrt(value));
  return 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s;
}

function relativeLuminance(hex: string): number {
  const channels = [1, 3, 5].map((index) => {
    const value = Number.parseInt(hex.slice(index, index + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrast(left: string, right: string): number {
  const [bright, dark] = [relativeLuminance(left), relativeLuminance(right)].sort((a, b) => b - a);
  return (bright + 0.05) / (dark + 0.05);
}

describe("palette tokens", () => {
  it("keeps every LAYER_STYLE row monotonic with ticks inside the domain", () => {
    for (const [metric, style] of Object.entries(LAYER_STYLE)) {
      expect(style.stops).toHaveLength(style.classes);
      expect(style.ticks[0]).toBe(style.domain[0]);
      expect(style.ticks.at(-1)).toBe(style.domain[1]);
      expect(style.ticks).toEqual([...style.ticks].sort((left, right) => left - right));
      const colors = classColors(metric as MapMetric);
      expect(new Set(colors).size).toBe("cap" in style && style.cap ? style.classes + 1 : style.classes);
    }
  });

  it("round-trips CSS variables onto document.documentElement", () => {
    const root = document.createElement("div");
    applyPaletteCssVariables(root);
    expect(root.style.getPropertyValue("--bg")).toBe(SURFACE.bg);
    expect(root.style.getPropertyValue("--no-data")).toBe(SURFACE.noData);
    expect(root.style.getPropertyValue("--accent-fit")).toBe(RAMPS.fit[3]);
    expect(root.style.getPropertyValue("--ramp-concern-0")).toBe(RAMPS.concern[0]);
    expect(root.style.getPropertyValue("--ramp-magnitude-cap")).toBe(MAGNITUDE_CAP);
    expect(Object.keys(CSS_VARIABLES).length).toBeGreaterThan(40);
  });

  it("keeps frontend polarity aligned with layer-descriptor direction", () => {
    for (const layer of judgementLayers) {
      expect(LAYER_STYLE[layer.key].polarity).toBe(layer.polarity);
    }
  });

  it("keeps ramp lightness monotonic and data stops distinguishable from the map chrome", () => {
    expect(RAMPS.concern.map(hexLightness)).toEqual(
      [...RAMPS.concern.map(hexLightness)].sort((left, right) => right - left),
    );
    expect(RAMPS.fit.map(hexLightness)).toEqual(
      [...RAMPS.fit.map(hexLightness)].sort((left, right) => left - right),
    );
    expect(RAMPS.magnitude.map(hexLightness)).toEqual(
      [...RAMPS.magnitude.map(hexLightness)].sort((left, right) => left - right),
    );
    for (const color of [...RAMPS.concern, ...RAMPS.fit, MAGNITUDE_CAP]) {
      expect(contrast(color, SURFACE.bg)).toBeGreaterThanOrEqual(2.9);
      expect(contrast(color, SURFACE.noData)).toBeGreaterThanOrEqual(1.7);
      expect(color).not.toBe(SURFACE.noData);
      expect(color).not.toBe(SURFACE.selection);
    }
    for (const color of RAMPS.magnitude) {
      expect(contrast(color, SURFACE.bg)).toBeGreaterThanOrEqual(2.3);
      expect(color).not.toBe(SURFACE.noData);
    }
  });

  it("assigns hazard classes with scoreBand rounding and mountain caps", () => {
    expect(layerClass(19.94, "residential-hazard")).toBe(0);
    expect(layerClass(19.96, "residential-hazard")).toBe(1);
    expect(scoreBand(19.94)).toBe("low");
    expect(scoreBand(19.96)).toBe("below");
    expect(layerColor(21.25, "residential-hazard")).toBe(RAMPS.concern[3]);
    expect(layerColor(80.5, "residential-hazard")).toBe(RAMPS.concern[9]);
    // Round first (Number(value.toFixed(1))), then accept/reject the domain.
    // -0.04 becomes -0 after toFixed(1); Object.is treats that as distinct from +0.
    expect(layerColor(-0.04, "residential-hazard")).toBe(RAMPS.concern[0]);
    expect(scoreBand(-0.04)).toBe("low");
    expect(layerClass(-0.05, "residential-hazard")).toBeNull();
    expect(scoreBand(-0.05)).toBeNull();
    expect(layerClass(100.04, "residential-hazard")).toBe(4);
    expect(scoreBand(100.04)).toBe("highest");
    expect(layerClass(100.06, "residential-hazard")).toBeNull();
    expect(scoreBand(100.06)).toBeNull();
    expect(layerClass(0.49, "mountain")).toBe(0);
    expect(layerClass(0.5, "mountain")).toBe(1);
    expect(layerClass(5, "mountain", "tract")).toBe(10);
    expect(layerColor(5, "mountain", "tract")).toBe(MAGNITUDE_CAP);
    expect(layerColor(4, "mountain", "county")).toBe(MAGNITUDE_CAP);
    expect(layerColor(3.99, "mountain", "county")).toBe(RAMPS.magnitude[7]);
    expect(layerStyle("mountain", "county").classes).toBe(8);
    expect(layerColor(70, "cost-of-living")).toBe(layerColor(80, "cost-of-living"));
    expect(layerColor(0.82, "county-fit")).toBe(RAMPS.fit[4]);
  });

  it("keeps cost, home, and county-fit on discrete class boundaries", () => {
    const costStops = [0, 1, 3, 4, 5, 6, 8, 9] as const;
    expect([80, 85, 90, 95, 100, 105, 110, 115].map((value) => layerColor(value, "cost-of-living")))
      .toEqual(costStops.map((stop) => RAMPS.concern[stop]));
    for (let klass = 1; klass < 8; klass += 1) {
      const boundary = 80 + klass * 5;
      expect(layerClass(boundary - 1e-9, "cost-of-living")).toBe(klass - 1);
      expect(layerClass(boundary, "cost-of-living")).toBe(klass);
    }
    expect(layerClass(120, "cost-of-living")).toBe(7);
    expect(layerColor(130, "cost-of-living")).toBe(RAMPS.concern[9]);

    const homeStops = [9, 7, 5, 3, 0] as const;
    expect([0, 20, 40, 60, 80].map((value) => layerColor(value, "home-costs")))
      .toEqual(homeStops.map((stop) => RAMPS.concern[stop]));
    expect(layerColor(100, "home-costs")).toBe(RAMPS.concern[0]);
    for (const boundary of [20, 40, 60, 80]) {
      expect(layerClass(boundary - 1e-9, "home-costs")).toBe(boundary / 20 - 1);
      expect(layerClass(boundary, "home-costs")).toBe(boundary / 20);
    }
    expect(layerClass(-1e-9, "home-costs")).toBeNull();
    expect(layerClass(100 + 1e-9, "home-costs")).toBeNull();

    expect([0, 0.21, 0.41, 0.61, 0.81].map((value) => layerColor(value, "county-fit")))
      .toEqual([...RAMPS.fit]);
    expect(layerColor(1, "county-fit")).toBe(RAMPS.fit[4]);
    expect(layerColor(1, "county-fit", "county")).toBe(layerColor(1, "county-fit", "tract"));
    expect(layerClass(0.2, "county-fit")).toBe(1);
    expect(layerClass(0.4, "county-fit")).toBe(2);
    expect(layerClass(0.6, "county-fit")).toBe(3);
    expect(layerClass(0.8, "county-fit")).toBe(4);
    expect(layerClass(0.1999, "county-fit")).toBe(0);
    expect(layerClass(0.3999, "county-fit")).toBe(1);
    expect(layerClass(0.5999, "county-fit")).toBe(2);
    expect(layerClass(0.7999, "county-fit")).toBe(3);
    expect(layerClass(-1e-9, "county-fit")).toBeNull();
    expect(layerClass(1 + 1e-9, "county-fit")).toBeNull();
    expect(classColors("county-fit")).toHaveLength(5);
    expect(classColors("cost-of-living")).toHaveLength(8);
    expect(classColors("home-costs")).toHaveLength(5);
  });

  it("forbids raw color literals in map consumers and styles.css", () => {
    const sources = [
      ["mapRenderer.worker.ts", workerSource],
      ["App.tsx", appSource],
      ["CountyFitWorkspace.tsx", countyFitSource],
    ] as const;
    for (const [name, source] of sources) {
      expect(source, name).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(source, name).not.toMatch(/rgba?\(/);
      if (name !== "mapRenderer.worker.ts") {
        expect(source, name).toMatch(/Stepped .+ color scale/);
        expect(source, name).not.toMatch(/Continuous .+ color scale/);
      }
    }
    const leftovers = [...cssSource.matchAll(/#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)/g)]
      .map((match) => match[0])
      .filter((value) => value !== "#10191e" && value !== "#edf2ef");
    expect(leftovers).toEqual([]);
  });
});
