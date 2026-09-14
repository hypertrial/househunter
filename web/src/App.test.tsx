import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { communityLabel, mapFocusTarget, mapTooltipClass, mountainLabel, scoreBand, scoreLabel, scorePillLabel, sortedHazardPercentiles, STATE_ABBREVIATIONS } from "./App";
import RiskMap from "./RiskMap";
import { cameraFromTransform, COMMUNITY_COLOR_SCALE, COMMUNITY_GROUP_COLORS, communityGroupColor, FEMA_COLOR_SCALE, MAP_COLORS, METRIC_COLOR_SCALES, metricColor, MOUNTAIN_COLOR_SCALE, MOUNTAIN_COLORS, mountainColor, readHash, relativeTransform, scoreColor, transformFromCamera } from "./map";
import type { HazardPercentile, MapScore, PlaceSummary } from "./types";

const tract: PlaceSummary = {
  place_id: "08013012101", name: "08013012101", state: "CO", place_type: "tract",
  population_2020: 0, housing_units_2020: 0, risk_score: 21.25, coverage_status: "complete",
  fema_vintage: "December 2025", census_vintage: "n/a", county_fips: "08013", county_name: "Boulder",
  community_conditions_group: 2, community_conditions_geography: "county", chrr_release_year: 2025,
  mountain_score: 82.5, mountain_score_version: "mountain_score_v1", mountain_pipeline_version: "mountain_pipeline_v1",
  relief_5km_m: 450, relief_10km_m: 700, relief_20km_m: 1200, relief_40km_m: 1800, relief_20km_pct: 88,
  rugged_fraction_20km: 0.65, rugged_pct: 85, public_mountain_access_raw: 25, public_mountain_access_pct: 75,
  open_mountain_km2_5: 2, open_mountain_km2_15: 7, open_mountain_km2_30: 16,
  restricted_mountain_km2_30: 1, closed_mountain_km2_30: 2, unknown_mountain_km2_30: 1,
  nearest_mountain_trail_km: 3.5, mountain_trail_km_10: 4, mountain_trail_km_25: 9,
  trail_access_raw: 6, trail_access_pct: 70, mountain_population_coverage: 1, mountain_coverage_status: "complete",
};
const county: PlaceSummary = { ...tract, place_id: "08013", name: "Boulder", place_type: "county", risk_score: 18.5 };
const hazards = [{ code: "WFIR", label: "Wildfire", percentile: 80.5 }, { code: "TSUN", label: "Tsunami", percentile: null }];
const topology = (id: string, state = "CO", translate = [-106, 38]) => ({
  type: "Topology", transform: { scale: [0.01, 0.01], translate },
  objects: { geography: { type: "GeometryCollection", geometries: [{ type: "Polygon", id, properties: { place_id: id, state, county_fips: "08013", name: id }, arcs: [[0]] }] } },
  arcs: [[[0, 0], [100, 0], [0, 100], [-100, 0], [0, -100]]],
});
const manifest = {
  schema_version: 1, release: "v1.20", initial_compressed_size: 100,
  files: [
    { key: "states-national", filename: "states.hash.topojson.gz", level: "state", lod: "national", jurisdiction: null, feature_count: 1, bounds: [-106, 38, -105, 39], compressed_size: 1, sha256: "x" },
    { key: "tracts-national", filename: "tracts.hash.topojson.gz", level: "tract", lod: "national", jurisdiction: null, feature_count: 1, bounds: [-106, 38, -105, 39], compressed_size: 1, sha256: "x" },
    { key: "tracts-co", filename: "tracts-co.hash.topojson.gz", level: "tract", lod: "detail", jurisdiction: "CO", feature_count: 1, bounds: [-106, 38, -105, 39], compressed_size: 1, sha256: "x" },
    { key: "counties-national", filename: "counties.hash.topojson.gz", level: "county", lod: "national", jurisdiction: null, feature_count: 1, bounds: [-106, 38, -105, 39], compressed_size: 1, sha256: "x" },
  ],
};

function response(body: unknown) { return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }); }

function mockFetch(
  build = true,
  buildScope: { kind: "national" | "state"; state: string | null } = { kind: "national", state: null },
) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v1/meta") return response({ app_version: "1", mutation_token: "token", reference_assets_ready: true, reference_assets_error: null, map_assets: { ready: true, error: null, schema_version: 1, release: "v1.20", manifest_url: "/map-assets/manifest.json" }, build: build ? { build_id: "fixture", place_count: 1, ranked_place_count: 1, county_count: 1, ranked_county_count: 1, source_vintages: { fema: "December 2025" }, scope: buildScope } : null });
    if (url.pathname === "/map-assets/manifest.json") return response(manifest);
    if (url.pathname.includes("states.hash")) {
      const state = buildScope.state || "CO";
      return response(topology(state, state, state === "AL" ? [-88, 30] : [-106, 38]));
    }
    if (url.pathname.includes("tracts.hash") || url.pathname.includes("tracts-co.hash")) return response(topology(tract.place_id));
    if (url.pathname.includes("counties.hash")) return response(topology(county.place_id));
    if (url.pathname === "/api/v1/map/scores") return response({ schema_version: 2, build_id: "fixture", level: url.searchParams.get("level"), scope: buildScope, columns: { place_id: [url.searchParams.get("level") === "county" ? county.place_id : tract.place_id], risk_score: [url.searchParams.get("level") === "county" ? county.risk_score : tract.risk_score], community_conditions_group: [2], mountain_score: [82.5] } });
    if (url.pathname === "/api/v1/places" || url.pathname === "/api/v1/counties") return response({ total: 1, items: [url.pathname.includes("counties") ? county : tract] });
    if (url.pathname === `/api/v1/places/${tract.place_id}` || url.pathname === `/api/v1/counties/${county.place_id}`) return response({ summary: url.pathname.includes("counties") ? county : tract, total_weighted_housing: 0, coverage_ratio: 1, methodology_notice: "Published FEMA percentile; not property-level risk.", tract_contributions: [], hazard_percentiles: hazards, member_tract_count: url.pathname.includes("counties") ? 12 : null });
    if (url.pathname === "/api/v1/lookup") return response({ status: "resolved", query: "1 Main", matched_address: "1 MAIN", tract_id: tract.place_id, detail: { summary: tract, total_weighted_housing: 0, coverage_ratio: 1, methodology_notice: "Published FEMA percentile.", tract_contributions: [], hazard_percentiles: hazards, member_tract_count: null }, provider: "census", precision: "house", approximate: false, attribution: null });
    return response({});
  });
}

const workerMessages: Array<{ worker: string; value: Record<string, unknown> }> = [];
const bitmapTransfers: unknown[] = [];
const presentationOrder: string[] = [];
const mockWorkers: MockWorker[] = [];

class MockWorker {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: ErrorEvent) => void) | null = null;
  private readonly renderer: boolean;
  private stopped = false;
  private ready = false;
  private frameState: Record<string, unknown> = {};
  private scoreCount = 0;
  private readonly snapshotInteractivity = new Map<unknown, boolean>();

  constructor(url: string | URL) {
    this.renderer = String(url).includes("mapRenderer");
    mockWorkers.push(this);
  }

  deliver(data: Record<string, unknown>) {
    if ((data.type === "FRAME" || data.type === "FRAME_REUSED") && typeof data.interactive === "boolean") {
      this.snapshotInteractivity.set(data.snapshotId, data.interactive);
    }
    this.onmessage?.({ data } as MessageEvent);
  }

  fail(message: string) {
    this.onerror?.({ message } as ErrorEvent);
  }

  private emit(data: Record<string, unknown>) {
    if ((data.type === "FRAME" || data.type === "FRAME_REUSED") && typeof data.interactive === "boolean") {
      this.snapshotInteractivity.set(data.snapshotId, data.interactive);
    }
    if (!this.stopped) queueMicrotask(() => {
      if (!this.stopped) this.onmessage?.({ data } as MessageEvent);
    });
  }

  private frame() {
    if (!this.renderer || !this.ready) return;
    const state = this.frameState as {
      datasetGeneration: number; viewportGeneration: number; cameraGeneration: number;
      semanticGeneration: number; camera: { k: number; x: number; y: number };
      width: number; height: number; ratio: number; metric: string; level: string;
    };
    this.emit({
      type: "FRAME", datasetGeneration: state.datasetGeneration,
      viewportGeneration: state.viewportGeneration, cameraGeneration: state.cameraGeneration,
      semanticGeneration: state.semanticGeneration, snapshotId: Date.now() + Math.random(),
      metric: state.metric, camera: state.camera, originX: -state.width * 0.25,
      originY: -state.height * 0.25, width: state.width * 1.5, height: state.height * 1.5,
      ratio: state.ratio, featureCount: this.scoreCount, bitmap: { close: vi.fn() },
      interactive: true,
    });
  }

  postMessage(value: Record<string, unknown>) {
    workerMessages.push({ worker: this.renderer ? "renderer" : "loader", value });
    if (value.type === "FRAME_COMMITTED") presentationOrder.push(`ack:${String(value.presented)}`);
    if (!this.renderer || this.stopped) return;
    if (value.type === "INIT") {
      const init = value as typeof value & {
        datasetGeneration: number; viewportGeneration: number; cameraGeneration: number;
        semanticGeneration: number; camera: { k: number; x: number; y: number };
        width: number; height: number; ratio: number; level: string; scoreUrl: string;
        expectedBuildId: string; semantics: { metric: string; neutralOnly: boolean };
      };
      this.frameState = { ...init, metric: init.semantics.metric };
      const load = async () => {
        if (!init.semantics.neutralOnly) {
          try {
            const result = await fetch(init.scoreUrl);
            const body = await result.json() as { schema_version?: number; build_id?: string; level?: string; columns?: { place_id?: string[] } };
            if (!result.ok || body.schema_version !== 2 || body.build_id !== init.expectedBuildId || body.level !== init.level) {
              throw new Error("Map scores do not match the current build");
            }
            this.scoreCount = body.columns?.place_id?.length || 0;
            this.emit({ type: "SCORES_READY", datasetGeneration: init.datasetGeneration, count: this.scoreCount });
          } catch (caught) {
            this.emit({ type: "ERROR", datasetGeneration: init.datasetGeneration, kind: "score", message: caught instanceof Error ? caught.message : "Map score load failed" });
          }
        }
        this.ready = true;
        this.frame();
      };
      void load();
    } else if (value.type === "SET_SEMANTICS") {
      this.frameState.semanticGeneration = value.semanticGeneration;
      this.frameState.metric = (value.semantics as { metric: string }).metric;
      this.frame();
    } else if (value.type === "SET_SELECTION") {
      this.frameState.semanticGeneration = value.semanticGeneration;
      this.frame();
    } else if (value.type === "SET_CAMERA") {
      this.frameState.cameraGeneration = value.cameraGeneration;
      this.frameState.camera = value.camera;
      this.frame();
    } else if (value.type === "RESIZE") {
      Object.assign(this.frameState, value);
      this.frame();
    } else if (value.type === "PICK") {
      this.emit({ type: "PICK_RESULT", datasetGeneration: value.datasetGeneration, requestId: value.requestId, mode: value.mode, snapshotId: value.snapshotId, preview: null });
    } else if (value.type === "FOCUS") {
      this.emit({
        type: "FOCUS_RESULT", datasetGeneration: value.datasetGeneration,
        requestId: value.requestId, snapshotId: value.snapshotId,
        bounds: this.snapshotInteractivity.get(value.snapshotId) === false ? null : [[100, 100], [200, 200]],
      });
    } else if (value.type === "DISPOSE") this.stopped = true;
  }

  terminate() { this.stopped = true; }
}

beforeEach(() => {
  workerMessages.length = 0;
  bitmapTransfers.length = 0;
  presentationOrder.length = 0;
  mockWorkers.length = 0;
  window.history.replaceState(null, "", "/");
  class Observer {
    constructor(private readonly callback: () => void) {}
    observe() { this.callback(); }
    disconnect() { /* test stub */ }
  }
  vi.stubGlobal("ResizeObserver", Observer);
  vi.stubGlobal("Worker", MockWorker);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ x: 0, y: 0, top: 0, left: 0, right: 1000, bottom: 700, width: 1000, height: 700, toJSON: () => ({}) });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => ({
    transferFromImageBitmap: (bitmap: unknown) => { bitmapTransfers.push(bitmap); presentationOrder.push("present"); },
  }) as unknown as RenderingContext);
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("score semantics", () => {
  it("retains qualitative score labels independently of continuous colors", () => {
    expect([0, 19.94, 19.96, 39.94, 39.96, 59.96, 79.96, 100].map(scoreBand)).toEqual([
      "low", "low", "below", "below", "typical", "high", "highest", "highest",
    ]);
    expect(scoreBand(null)).toBeNull(); expect(scoreBand(101)).toBeNull();
    expect(scoreLabel({ ...tract, risk_score: 19.96 })).toBe("20.0");
    expect(scorePillLabel({ ...tract, risk_score: 19.96 })).toBe("20.0, below typical");
  });
  it("sorts hazards high-to-low with unrated last", () => {
    const values: HazardPercentile[] = [{ code: "TSUN", label: "Tsunami", percentile: null }, { code: "AVLN", label: "Avalanche", percentile: 10 }, { code: "WFIR", label: "Wildfire", percentile: 80 }];
    expect(sortedHazardPercentiles(values).map((item) => item.code)).toEqual(["WFIR", "AVLN", "TSUN"]);
  });
  it("validates and clamps URL map state", () => {
    expect(readHash("#level=county&state=co&county=123&place=08013&cx=4&cy=-2&z=99")).toEqual({ level: "county", metric: "fema", state: "", county: "", place: "08013", unranked: false, mountainMin: null, camera: { cx: 1, cy: 0, z: 12 } });
    expect(readHash("#level=tract&state=ZZ&county=08013&place=08013012101")).toMatchObject({ state: "", county: "", place: "08013012101" });
    expect(readHash("#level=tract&state=CO&county=01001&place=01001000100")).toMatchObject({ state: "CO", county: "", place: "" });
    expect(readHash("#metric=community-conditions").metric).toBe("community-conditions");
    expect(readHash("#metric=mountain&mountain_min=80")).toMatchObject({ metric: "mountain", mountainMin: 80 });
    expect(readHash("#metric=quality").metric).toBe("fema");
  });
  it("uses bounded fixed-domain continuous color scales and honest null labels", () => {
    expect(FEMA_COLOR_SCALE).toHaveLength(256);
    expect(MOUNTAIN_COLOR_SCALE).toHaveLength(256);
    expect(COMMUNITY_COLOR_SCALE).toHaveLength(256);
    expect(METRIC_COLOR_SCALES.fema).toMatchObject({
      minimum: 0, maximum: 100, ticks: [0, 20, 40, 60, 80, 100],
    });
    expect(METRIC_COLOR_SCALES["community-conditions"]).toMatchObject({
      minimum: 1, maximum: 10, ticks: [1, 3, 5, 7, 10],
    });
    expect(METRIC_COLOR_SCALES.fema.gradient.match(/#[\da-f]{6}/g)).toHaveLength(256);
    expect(METRIC_COLOR_SCALES["community-conditions"].gradient.match(/#[\da-f]{6}/g))
      .toHaveLength(256);
    expect(METRIC_COLOR_SCALES.mountain.gradient.match(/#[\da-f]{6}/g)).toHaveLength(256);
    expect(scoreColor(0)).toBe(MAP_COLORS.low);
    expect(scoreColor(25)).toBe(MAP_COLORS.below);
    expect(scoreColor(50)).toBe(MAP_COLORS.typical);
    expect(scoreColor(75)).toBe(MAP_COLORS.high);
    expect(scoreColor(100)).toBe(MAP_COLORS.highest);
    expect(scoreColor(19.8)).not.toBe(scoreColor(20.2));
    expect(scoreColor(null)).toBeNull();
    expect(scoreColor(Number.NaN)).toBeNull();
    expect(scoreColor(-0.1)).toBeNull();
    expect(scoreColor(100.1)).toBeNull();
    expect(mountainColor(0)).toBe(MOUNTAIN_COLORS.low);
    expect(mountainColor(25)).toBe(MOUNTAIN_COLORS.below);
    expect(mountainColor(50)).toBe(MOUNTAIN_COLORS.typical);
    expect(mountainColor(75)).toBe(MOUNTAIN_COLORS.high);
    expect(mountainColor(100)).toBe(MOUNTAIN_COLORS.highest);
    expect(COMMUNITY_GROUP_COLORS).toHaveLength(10);
    expect(communityGroupColor(1)).toBe("#7fa87e");
    expect(communityGroupColor(10)).toBe("#b14a3c");
    expect(communityGroupColor(1.5)).toBeNull();
    expect(communityGroupColor(null)).toBeNull();
    expect(communityLabel({ ...county, community_conditions_group: null })).toBe("Not grouped");
    expect(mountainLabel(tract)).toBe("82.5 /100");
  });
  it("interpolates between anchors without recreating former score bands", () => {
    expect(scoreColor(12.5)).toBe("#a2ac64");
    expect(scoreColor(37.5)).toBe("#cbac39");
    for (const boundary of [20, 40, 60, 80]) {
      expect(scoreColor(boundary - 0.1)).not.toBe(scoreColor(boundary + 0.1));
      expect(mountainColor(boundary - 0.1)).not.toBe(mountainColor(boundary + 0.1));
    }
    const femaColors = new Set(Array.from({ length: 10_001 }, (_, index) => scoreColor(index / 100)));
    const mountainColors = new Set(Array.from({ length: 10_001 }, (_, index) => mountainColor(index / 100)));
    expect(femaColors.size).toBeGreaterThan(5);
    expect(femaColors.size).toBeLessThanOrEqual(256);
    expect(mountainColors.size).toBeGreaterThan(5);
    expect(mountainColors.size).toBeLessThanOrEqual(256);
  });
  it("rejects every invalid scale value and keeps Community groups on official integer anchors", () => {
    for (const invalid of [Number.NEGATIVE_INFINITY, Number.POSITIVE_INFINITY, Number.NaN, -0.1, 100.1]) {
      expect(scoreColor(invalid)).toBeNull();
      expect(mountainColor(invalid)).toBeNull();
    }
    expect(Array.from({ length: 10 }, (_, index) => communityGroupColor(index + 1)))
      .toEqual(COMMUNITY_GROUP_COLORS);
    for (const invalid of [Number.NEGATIVE_INFINITY, Number.POSITIVE_INFINITY, Number.NaN, 0, 1.5, 11]) {
      expect(communityGroupColor(invalid)).toBeNull();
    }
  });
  it("uses the fixed shared scales on the map independently of unrelated metric values", () => {
    const row: MapScore = {
      place_id: "08013012101",
      risk_score: 21.25,
      community_conditions_group: 2,
      mountain_score: 82.5,
    };
    expect(metricColor(row, "fema")).toBe(scoreColor(21.25));
    expect(metricColor({ ...row, mountain_score: 0 }, "fema")).toBe(scoreColor(21.25));
    expect(metricColor(row, "mountain")).toBe(mountainColor(82.5));
    expect(metricColor({ ...row, risk_score: 100 }, "mountain")).toBe(mountainColor(82.5));
    expect(metricColor(row, "community-conditions")).toBe(communityGroupColor(2));
    expect(metricColor(null, "fema")).toBeNull();
  });
  it("computes the gesture delta from the last committed camera", () => {
    expect(relativeTransform(
      { k: 6, x: -900, y: -420 },
      { k: 3, x: -300, y: -120 },
    )).toEqual({ k: 2, x: -300, y: -180 });
  });
  it("preserves a normalized camera when viewport dimensions change", () => {
    const initial = { cx: 0.37, cy: 0.61, z: 5 };
    const oldTransform = transformFromCamera(initial, 1280, 720);
    const normalized = cameraFromTransform(oldTransform, 1280, 720);
    expect(cameraFromTransform(transformFromCamera(normalized, 640, 900), 640, 900))
      .toEqual(initial);
  });
  it("positions hover previews away from viewport edges", () => {
    expect(mapTooltipClass(100, 100, 1000, 700)).toBe("map-tooltip");
    expect(mapTooltipClass(900, 100, 1000, 700)).toBe("map-tooltip tooltip-left");
    expect(mapTooltipClass(900, 600, 1000, 700)).toBe("map-tooltip tooltip-left tooltip-up");
  });
});

it("continues zooming from a camera restored from the URL", async () => {
  vi.stubGlobal("fetch", mockFetch());
  const onCamera = vi.fn();
  render(<RiskMap
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v1/map/scores?level=tract" expectedBuildId="fixture" level="tract"
    selected="" state="" county="" showUnranked={false} focusTarget={null}
    initialCamera={{ cx: 0.5, cy: 0.5, z: 5 }} onSelect={() => undefined}
    onPreview={() => undefined} onCamera={onCamera} onStatus={() => undefined}
  />);
  const canvas = document.querySelector<HTMLElement>(".risk-canvas")!;
  await waitFor(() => expect((canvas as HTMLElement & { __zoom?: { k: number } }).__zoom?.k).toBe(5));
  fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
  await waitFor(() => expect(onCamera).toHaveBeenLastCalledWith(expect.objectContaining({ z: 7.5 })));
});

it("preserves the normalized camera through a responsive resize", async () => {
  let resize: () => void = () => undefined;
  let width = 1000;
  vi.stubGlobal("ResizeObserver", class {
    constructor(callback: () => void) { resize = callback; }
    observe() { /* test stub */ }
    disconnect() { /* test stub */ }
  });
  vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockImplementation(() => ({
    x: 0, y: 0, top: 0, left: 0, right: width, bottom: 700,
    width, height: 700, toJSON: () => ({}),
  }));
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => width);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(() => 700);
  vi.stubGlobal("fetch", mockFetch());
  const onCamera = vi.fn();
  render(<RiskMap
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v1/map/scores?level=tract" expectedBuildId="fixture" level="tract"
    selected="" state="" county="" showUnranked={false} focusTarget={null}
    initialCamera={{ cx: 0.37, cy: 0.61, z: 2 }} onSelect={() => undefined}
    onPreview={() => undefined} onCamera={onCamera} onStatus={() => undefined}
  />);
  const canvas = document.querySelector<HTMLElement>(".risk-canvas")!;
  await waitFor(() => expect((canvas as HTMLElement & { __zoom?: { k: number } }).__zoom?.k).toBe(2));
  width = 500;
  act(() => resize());
  await waitFor(() => expect((canvas as HTMLElement & { __zoom?: { x: number, y: number } }).__zoom)
    .toMatchObject({ x: -120, y: -504 }));
  const cameraMessages = workerMessages.filter(({ worker, value }) => worker === "renderer"
    && (value.type === "SET_CAMERA" || value.type === "RESIZE"));
  const resizeIndex = cameraMessages.map(({ value }) => value.type).lastIndexOf("RESIZE");
  expect(cameraMessages[resizeIndex - 1]?.value.type).toBe("SET_CAMERA");
  expect(cameraMessages[resizeIndex].value.cameraGeneration).toBeGreaterThan(
    Number(cameraMessages[resizeIndex - 1].value.cameraGeneration),
  );
  fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
  await waitFor(() => expect(onCamera).toHaveBeenLastCalledWith(expect.objectContaining({ cx: 0.37, cy: 0.61, z: 3 })));
});

it("pans with pointer-only Chrome input without activating a geography", async () => {
  vi.stubGlobal("fetch", mockFetch());
  const onCamera = vi.fn();
  render(<RiskMap
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v1/map/scores?level=tract"
    expectedBuildId="fixture" level="tract" selected="" state="" county=""
    showUnranked={false} focusTarget={null} initialCamera={{ cx: 0.5, cy: 0.5, z: 2 }}
    onSelect={() => undefined} onPreview={() => undefined} onCamera={onCamera}
    onStatus={() => undefined}
  />);
  const viewport = screen.getByRole("img", { name: /Focusable USA tract/ });
  await waitFor(() => expect(viewport).toHaveAttribute("aria-busy", "false"));
  await waitFor(() => expect(onCamera).toHaveBeenCalled());
  onCamera.mockClear();
  fireEvent.pointerDown(viewport, {
    pointerId: 7, pointerType: "mouse", isPrimary: true,
    button: 0, buttons: 1, clientX: 500, clientY: 350,
  });
  fireEvent.pointerMove(viewport, {
    pointerId: 7, pointerType: "mouse", isPrimary: true,
    button: -1, buttons: 1, clientX: 600, clientY: 400,
  });
  fireEvent.pointerUp(viewport, {
    pointerId: 7, pointerType: "mouse", isPrimary: true,
    button: 0, buttons: 0, clientX: 600, clientY: 400,
  });

  await waitFor(() => expect(onCamera).toHaveBeenCalledTimes(1));
  expect(onCamera).toHaveBeenLastCalledWith(expect.objectContaining({ cx: 0.45, z: 2 }));
  expect(workerMessages.filter(({ value }) => value.type === "PICK")).toEqual([]);

  onCamera.mockClear();
  fireEvent.pointerDown(viewport, {
    pointerId: 8, pointerType: "mouse", isPrimary: true, ctrlKey: true,
    button: 0, buttons: 1, clientX: 600, clientY: 400,
  });
  fireEvent.pointerMove(viewport, {
    pointerId: 8, pointerType: "mouse", isPrimary: true, ctrlKey: true,
    button: -1, buttons: 1, clientX: 700, clientY: 450,
  });
  fireEvent.pointerUp(viewport, {
    pointerId: 8, pointerType: "mouse", isPrimary: true, ctrlKey: true,
    button: 0, buttons: 0, clientX: 700, clientY: 450,
  });
  expect(onCamera).not.toHaveBeenCalled();
});

it("ignores a stale score response after changing geography level", async () => {
  let resolveTract!: (value: Response) => void;
  let resolveCounty!: (value: Response) => void;
  const tractResponse = new Promise<Response>((resolve) => { resolveTract = resolve; });
  const countyResponse = new Promise<Response>((resolve) => { resolveCounty = resolve; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v1/map/scores") {
      return url.searchParams.get("level") === "county" ? countyResponse : tractResponse;
    }
    return baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Counties" }));
  resolveCounty(response({ schema_version: 2, build_id: "fixture", level: "county", scope: { kind: "national", state: null }, columns: { place_id: [county.place_id], risk_score: [county.risk_score], community_conditions_group: [2], mountain_score: [82.5] } }));
  await waitFor(() => expect(screen.getByTitle("fixture")).toHaveTextContent("county"));
  await act(async () => resolveTract(response({ schema_version: 2, build_id: "fixture", level: "tract", scope: { kind: "national", state: null }, columns: { place_id: [tract.place_id], risk_score: [tract.risk_score], community_conditions_group: [2], mountain_score: [82.5] } })));
  expect(screen.getByTitle("fixture")).not.toHaveTextContent("tracts ready");
  expect(screen.getByRole("button", { name: "Counties" })).toHaveAttribute("aria-pressed", "true");
});

it("ignores a stale score failure after the replacement level succeeds", async () => {
  let rejectTract!: (reason: Error) => void;
  let resolveCounty!: (value: Response) => void;
  const tractResponse = new Promise<Response>((_, reject) => { rejectTract = reject; });
  const countyResponse = new Promise<Response>((resolve) => { resolveCounty = resolve; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v1/map/scores") {
      return url.searchParams.get("level") === "county" ? countyResponse : tractResponse;
    }
    return baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Counties" }));
  resolveCounty(response({ schema_version: 2, build_id: "fixture", level: "county", scope: { kind: "national", state: null }, columns: { place_id: [county.place_id], risk_score: [county.risk_score], community_conditions_group: [2], mountain_score: [82.5] } }));
  await waitFor(() => expect(screen.getByTitle("fixture")).toHaveTextContent("county"));
  await act(async () => rejectTract(new Error("obsolete tract request failed")));
  expect(screen.queryByText("Scores could not be loaded")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Counties" })).toHaveAttribute("aria-pressed", "true");
});

it("loads the replacement level after a score failure and marks it busy until interactive", async () => {
  let resolveCounty!: (value: Response) => void;
  const countyResponse = new Promise<Response>((resolve) => { resolveCounty = resolve; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname !== "/api/v1/map/scores") return baseFetch(input);
    if (url.searchParams.get("level") === "county") return countyResponse;
    return new Response(JSON.stringify({ detail: "tract scores unavailable" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }));
  render(<App />);
  await screen.findByText("Scores could not be loaded");

  fireEvent.click(screen.getByRole("button", { name: "Counties" }));
  const map = screen.getByRole("img", { name: /Focusable USA county/ });
  expect(map).toHaveAttribute("aria-busy", "true");
  expect(screen.queryByText("Scores could not be loaded")).not.toBeInTheDocument();

  resolveCounty(response({
    schema_version: 2,
    build_id: "fixture",
    level: "county",
    scope: { kind: "national", state: null },
    columns: {
      place_id: [county.place_id],
      risk_score: [county.risk_score],
      community_conditions_group: [2],
      mountain_score: [82.5],
    },
  }));
  await waitFor(() => expect(map).toHaveAttribute("aria-busy", "false"));
  expect(screen.getByTitle("fixture")).toHaveTextContent("county");
});

it("ignores county options loaded for a previous draft state", async () => {
  let resolveCO!: (value: Response) => void;
  let resolveAL!: (value: Response) => void;
  const coResponse = new Promise<Response>((resolve) => { resolveCO = resolve; });
  const alResponse = new Promise<Response>((resolve) => { resolveAL = resolve; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v1/counties" && url.searchParams.has("state")) {
      return url.searchParams.get("state") === "CO" ? coResponse : alResponse;
    }
    return baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
  const stateSelect = screen.getByLabelText("State");
  fireEvent.change(stateSelect, { target: { value: "CO" } });
  fireEvent.change(stateSelect, { target: { value: "AL" } });
  resolveAL(response({ items: [{ ...county, place_id: "01001", name: "Autauga", state: "AL" }] }));
  await screen.findByRole("option", { name: "Autauga" });
  await act(async () => resolveCO(response({ items: [county] })));
  expect(stateSelect).toHaveValue("AL");
  expect(screen.queryByRole("option", { name: "Boulder" })).not.toBeInTheDocument();
});

it("does not clear current county options when an older state request fails", async () => {
  let rejectCO!: (reason: Error) => void;
  let resolveAL!: (value: Response) => void;
  const coResponse = new Promise<Response>((_, reject) => { rejectCO = reject; });
  const alResponse = new Promise<Response>((resolve) => { resolveAL = resolve; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v1/counties" && url.searchParams.has("state")) {
      return url.searchParams.get("state") === "CO" ? coResponse : alResponse;
    }
    return baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
  const stateSelect = screen.getByLabelText("State");
  fireEvent.change(stateSelect, { target: { value: "CO" } });
  fireEvent.change(stateSelect, { target: { value: "AL" } });
  resolveAL(response({ items: [{ ...county, place_id: "01001", name: "Autauga", state: "AL" }] }));
  await screen.findByRole("option", { name: "Autauga" });
  await act(async () => rejectCO(new Error("obsolete county request failed")));
  expect(screen.getByRole("option", { name: "Autauga" })).toBeInTheDocument();
});

it("discards address confirmations after the query changes", async () => {
  let resolveLookup!: (value: Response) => void;
  const lookup = new Promise<Response>((resolve) => { resolveLookup = resolve; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    return url.pathname === "/api/v1/lookup" ? lookup : baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  const input = screen.getByLabelText("Street address");
  fireEvent.change(input, { target: { value: "1 First St, Denver, CO" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  fireEvent.change(input, { target: { value: "2 Second St, Denver, CO" } });
  await act(async () => resolveLookup(response({ status: "confirmation_required", query: "1 First St, Denver, CO", message: "Confirm A", attribution: "OSM", candidates: [{ candidate_id: "candidate-a", matched_address: "1 First St" }] })));
  expect(screen.queryByRole("button", { name: /Use approximate street location/ })).not.toBeInTheDocument();
});

it("discards a resolved address after Search closes", async () => {
  let resolveLookup!: (value: Response) => void;
  const lookup = new Promise<Response>((resolve) => { resolveLookup = resolve; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    return url.pathname === "/api/v1/lookup" ? lookup : baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  const search = screen.getByRole("button", { name: "Search" });
  fireEvent.click(search);
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  fireEvent.click(search);
  await act(async () => resolveLookup(response({ status: "resolved", query: "1 Main St", matched_address: "1 MAIN", tract_id: tract.place_id, detail: { summary: tract, total_weighted_housing: 0, coverage_ratio: 1, methodology_notice: "Published FEMA percentile.", tract_contributions: [], hazard_percentiles: hazards, member_tract_count: null }, provider: "census", precision: "house", approximate: false, attribution: null })));
  expect(screen.queryByRole("dialog", { name: "Tract detail" })).not.toBeInTheDocument();
});

it("discards a resolved address after browser history closes Search", async () => {
  let resolveLookup!: (value: Response) => void;
  const lookup = new Promise<Response>((resolve) => { resolveLookup = resolve; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    return url.pathname === "/api/v1/lookup" ? lookup : baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  act(() => window.dispatchEvent(new PopStateEvent("popstate")));
  expect(screen.queryByLabelText("Search")).not.toBeInTheDocument();
  await act(async () => resolveLookup(response({ status: "resolved", query: "1 Main St", matched_address: "1 MAIN", tract_id: tract.place_id, detail: { summary: tract, total_weighted_housing: 0, coverage_ratio: 1, methodology_notice: "Published FEMA percentile.", tract_contributions: [], hazard_percentiles: hazards, member_tract_count: null }, provider: "census", precision: "house", approximate: false, attribution: null })));
  expect(screen.queryByRole("dialog", { name: "Tract detail" })).not.toBeInTheDocument();
});

it("does not surface a stale address failure after Search closes", async () => {
  let rejectLookup!: (reason: Error) => void;
  const lookup = new Promise<Response>((_, reject) => { rejectLookup = reject; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    return url.pathname === "/api/v1/lookup" ? lookup : baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  const search = screen.getByRole("button", { name: "Search" });
  fireEvent.click(search);
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  fireEvent.click(search);
  await act(async () => rejectLookup(new Error("obsolete lookup failed")));
  fireEvent.click(search);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Find tract" })).toBeEnabled();
});

it("acknowledges a worker snapshot only after atomically presenting its bitmap", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findByText("HouseHunter");
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });
  await waitFor(() => {
    expect(bitmapTransfers.length).toBeGreaterThan(0);
    expect(workerMessages.some(({ value }) => value.type === "FRAME_COMMITTED" && value.presented === true)).toBe(true);
  });
  expect(presentationOrder.indexOf("present")).toBeLessThan(presentationOrder.indexOf("ack:true"));
});

it("presents an outline frame without declaring the map visible or accepting picks", () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
  const onStatus = vi.fn();
  const onVisibleCommit = vi.fn();
  const onInteractiveCommit = vi.fn();
  render(<RiskMap
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v1/map/scores?level=tract"
    expectedBuildId="fixture" level="tract" selected="" state="" county=""
    showUnranked={false} focusTarget={null} initialCamera={{ cx: 0.5, cy: 0.5, z: 1 }}
    onSelect={() => undefined} onPreview={() => undefined} onCamera={() => undefined}
    onStatus={onStatus} onVisibleCommit={onVisibleCommit} onInteractiveCommit={onInteractiveCommit}
  />);
  const init = workerMessages.find(({ value }) => value.type === "INIT")!.value as {
    datasetGeneration: number; viewportGeneration: number; cameraGeneration: number;
    semanticGeneration: number; camera: { k: number; x: number; y: number };
    width: number; height: number; ratio: number;
  };
  const current = workerMessages.reduce((value, message) => {
    if (message.worker !== "renderer") return value;
    if (["RESIZE", "SET_CAMERA", "SET_SEMANTICS", "SET_SELECTION"].includes(String(message.value.type))) {
      return { ...value, ...message.value } as typeof init;
    }
    return value;
  }, init);
  act(() => mockWorkers[0].deliver({
    type: "FRAME", datasetGeneration: current.datasetGeneration,
    viewportGeneration: current.viewportGeneration, cameraGeneration: current.cameraGeneration,
    semanticGeneration: current.semanticGeneration, snapshotId: 44, metric: "fema",
    camera: current.camera, originX: -48, originY: -48,
    width: current.width + 96, height: current.height + 96, ratio: current.ratio,
    featureCount: 0, interactive: false, bitmap: { close: vi.fn() },
  }));
  expect(bitmapTransfers).toHaveLength(1);
  expect(onVisibleCommit).not.toHaveBeenCalled();
  expect(onInteractiveCommit).not.toHaveBeenCalled();
  expect(onStatus).not.toHaveBeenCalledWith(expect.stringContaining("interactive"));
  const viewport = screen.getByRole("img", { name: /Focusable USA tract/ });
  fireEvent.pointerDown(viewport, { pointerId: 1, clientX: 500, clientY: 350 });
  fireEvent.pointerUp(viewport, { pointerId: 1, clientX: 500, clientY: 350 });
  fireEvent.keyDown(viewport, { key: "Enter" });
  expect(workerMessages.filter(({ value }) => value.type === "PICK")).toEqual([]);
});

it("retries a place focus after a non-interactive outline cannot resolve it", async () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => undefined)));
  render(<RiskMap
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v1/map/scores?level=tract"
    expectedBuildId="fixture" level="tract" selected="" state="" county=""
    showUnranked={false} focusTarget={{ kind: "place", id: tract.place_id, nonce: 12 }}
    initialCamera={{ cx: 0.5, cy: 0.5, z: 1 }} onSelect={() => undefined}
    onPreview={() => undefined} onCamera={() => undefined} onStatus={() => undefined}
  />);
  const init = workerMessages.find(({ value }) => value.type === "INIT")!.value as {
    datasetGeneration: number; viewportGeneration: number; cameraGeneration: number;
    semanticGeneration: number; camera: { k: number; x: number; y: number };
    width: number; height: number; ratio: number;
  };
  const current = workerMessages.reduce((value, message) => {
    if (message.worker !== "renderer") return value;
    if (["RESIZE", "SET_CAMERA", "SET_SEMANTICS", "SET_SELECTION"].includes(String(message.value.type))) {
      return { ...value, ...message.value } as typeof init;
    }
    return value;
  }, init);
  const frame = (snapshotId: number, interactive: boolean, featureCount: number) => ({
    type: "FRAME", datasetGeneration: current.datasetGeneration,
    viewportGeneration: current.viewportGeneration, cameraGeneration: current.cameraGeneration,
    semanticGeneration: current.semanticGeneration, snapshotId, metric: "fema",
    camera: current.camera, originX: -48, originY: -48,
    width: current.width + 96, height: current.height + 96, ratio: current.ratio,
    featureCount, interactive, bitmap: { close: vi.fn() },
  });

  act(() => mockWorkers[0].deliver(frame(50, false, 0)));
  await waitFor(() => expect(workerMessages.filter(({ value }) => value.type === "FOCUS")).toHaveLength(1));
  act(() => mockWorkers[0].deliver(frame(51, true, 1)));
  await waitFor(() => expect(workerMessages.filter(({ value }) => value.type === "FOCUS")).toHaveLength(2));
});

it("closes and negatively acknowledges a stale bitmap without presenting it", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });
  const renderer = mockWorkers[0];
  const init = workerMessages.find(({ value }) => value.type === "INIT")!.value as {
    datasetGeneration: number; viewportGeneration: number; cameraGeneration: number;
    semanticGeneration: number; camera: { k: number; x: number; y: number };
    width: number; height: number; ratio: number;
  };
  const bitmap = { close: vi.fn() };
  const transferCount = bitmapTransfers.length;
  act(() => renderer.deliver({
    type: "FRAME",
    datasetGeneration: init.datasetGeneration,
    viewportGeneration: init.viewportGeneration + 10_000,
    cameraGeneration: init.cameraGeneration,
    semanticGeneration: init.semanticGeneration,
    snapshotId: 987_654,
    metric: "fema",
    camera: init.camera,
    originX: 0,
    originY: 0,
    width: init.width,
    height: init.height,
    ratio: init.ratio,
    featureCount: 1,
    interactive: true,
    bitmap,
  }));
  await waitFor(() => expect(bitmap.close).toHaveBeenCalledOnce());
  expect(bitmapTransfers).toHaveLength(transferCount);
  expect(workerMessages.some(({ value }) => value.type === "FRAME_COMMITTED"
    && value.snapshotId === 987_654 && value.presented === false)).toBe(true);
});

it("requests focus once per nonce even when focus itself commits more frames", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<RiskMap
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v1/map/scores?level=tract"
    expectedBuildId="fixture" level="tract" selected="" state="" county=""
    showUnranked={false} focusTarget={{ kind: "state", id: "CO", nonce: 9 }}
    initialCamera={{ cx: 0.5, cy: 0.5, z: 1 }} onSelect={() => undefined}
    onPreview={() => undefined} onCamera={() => undefined} onStatus={() => undefined}
  />);
  await waitFor(() => expect(workerMessages.filter(({ value }) => value.type === "FOCUS")).toHaveLength(1));
  await waitFor(() => expect(workerMessages.some(({ value }) => value.type === "SET_CAMERA")).toBe(true));
  await act(async () => Promise.resolve());
  expect(workerMessages.filter(({ value }) => value.type === "FOCUS")).toHaveLength(1);
});

it("preserves the last presented bitmap across a worker crash and creates a fresh worker pair on retry", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });
  const canvas = document.querySelector<HTMLCanvasElement>(".map-presentation")!;
  const snapshot = canvas.dataset.snapshotId;
  const transferCount = bitmapTransfers.length;
  act(() => mockWorkers[0].fail("renderer crashed"));
  expect(await screen.findByRole("alert")).toHaveTextContent("renderer crashed");
  expect(canvas.dataset.snapshotId).toBe(snapshot);
  expect(bitmapTransfers).toHaveLength(transferCount);
  fireEvent.click(screen.getByRole("button", { name: "Restart map" }));
  await waitFor(() => expect(mockWorkers).toHaveLength(4));
  await waitFor(() => expect(bitmapTransfers.length).toBeGreaterThan(transferCount));
});

it("renders the map as the only primary UI with all retained controls", async () => {
  vi.stubGlobal("fetch", mockFetch()); render(<App />);
  expect(await screen.findByText("HouseHunter")).toBeVisible();
  expect(screen.getByRole("button", { name: "Tracts" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "Counties" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Lowest / Highest" })).toBeVisible();
  expect(screen.getByRole("button", { name: /Filters/ })).toBeVisible();
  expect(screen.getByRole("button", { name: "Search" })).toHaveAttribute("aria-expanded", "false");
  expect(screen.getByRole("button", { name: "More" })).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(screen.getByRole("button", { name: "More" }));
  expect(screen.getByRole("button", { name: "More" })).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("region", { name: "More actions" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Export snapshot" }));
  const exports = screen.getByRole("navigation", { name: "Exports" });
  expect(screen.getByRole("button", { name: "More" })).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByRole("button", { name: "More" })).toHaveAttribute("aria-controls", "exports-panel");
  expect(within(exports).getByRole("link", { name: "Tracts CSV" })).toHaveAttribute("href", "/api/v1/exports/places.csv");
  expect(within(exports).getByRole("link", { name: "Tracts Parquet" })).toHaveAttribute("href", "/api/v1/exports/places.parquet");
  expect(within(exports).getByRole("link", { name: "Counties CSV" })).toHaveAttribute("href", "/api/v1/exports/counties.csv");
  expect(within(exports).getByRole("link", { name: "Counties Parquet" })).toHaveAttribute("href", "/api/v1/exports/counties.parquet");
  const legend = screen.getByLabelText("Continuous score color scale, lower is better");
  expect(legend).toHaveTextContent("not property-level risk");
  expect(legend.querySelector(".legend-gradient")).toHaveStyle({
    backgroundImage: expect.stringContaining("linear-gradient"),
  });
  expect(within(legend).getByRole("img", {
    name: "FEMA Risk continuous color ramp from 0 to 100",
  })).toBeVisible();
  for (const tick of ["0", "20", "40", "60", "80", "100"]) {
    expect(within(legend).getByText(tick)).toBeVisible();
  }
  expect(within(legend).getByText("Unranked")).toBeVisible();
  expect(legend.querySelector(".missing-key .hatched")).toHaveAttribute("aria-hidden", "true");
  expect(screen.getByRole("img", { name: /focusable USA tract risk map/i })).toBeVisible();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByText("Next")).not.toBeInTheDocument();
});

it("keeps the committed legend and accessible map description aligned during metric rendering", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });
  const canvas = screen.getByRole("img", { name: /focusable USA tract risk map/i });

  fireEvent.click(screen.getByRole("button", { name: "Mountain Score" }));

  expect(screen.getByText("Updating Mountain Score map…")).toBeVisible();
  expect(canvas).toHaveAttribute("aria-busy", "true");
  expect(canvas).toHaveAccessibleName(/tract risk map/i);
  expect(screen.getByLabelText("Continuous score color scale, lower is better")).toBeVisible();
  expect(screen.queryByLabelText(/Continuous Mountain Score color scale/)).not.toBeInTheDocument();

  expect(await screen.findByLabelText(
    "Continuous Mountain Score color scale, higher means more mountain characteristics",
  )).toBeVisible();
  await waitFor(() => expect(canvas).toHaveAttribute("aria-busy", "false"));
  expect(canvas).toHaveAccessibleName(/tract Mountain Score map/i);
  expect(document.querySelector(".map-updating")).not.toBeInTheDocument();
});

it("commits only the final metric after rapid successive map changes", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });
  const canvas = screen.getByRole("img", { name: /focusable USA tract risk map/i });

  fireEvent.click(screen.getByRole("button", { name: "Mountain Score" }));
  fireEvent.click(screen.getByRole("button", { name: "Community Conditions" }));

  expect(canvas).toHaveAttribute("aria-busy", "true");
  expect(screen.getByLabelText("Continuous score color scale, lower is better")).toBeVisible();
  const finalLegend = await screen.findByLabelText(
    "Continuous Community Conditions color scale, Group 1 is healthiest",
  );
  expect(finalLegend).toBeVisible();
  await waitFor(() => expect(canvas).toHaveAttribute("aria-busy", "false"));
  expect(canvas).toHaveAccessibleName(/tract Community Conditions map/i);
  expect(screen.queryByLabelText(/Continuous Mountain Score color scale/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Updating .* map…/)).not.toBeInTheDocument();
});

it("finishes a metric transition when active filters leave no interactive geographies", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });

  fireEvent.click(screen.getByRole("button", { name: "Filters" }));
  fireEvent.change(screen.getByLabelText("Minimum Mountain Score"), { target: { value: "100" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  fireEvent.click(screen.getByRole("button", { name: "Mountain Score" }));

  const canvas = screen.getByRole("img", { name: /focusable USA tract/i });
  expect(await screen.findByLabelText(
    "Continuous Mountain Score color scale, higher means more mountain characteristics",
  )).toBeVisible();
  await waitFor(() => expect(canvas).toHaveAttribute("aria-busy", "false"));
  expect(document.querySelector(".map-updating")).not.toBeInTheDocument();
});

it("reports effective filters and does not expose an ineffective Community checkbox", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findByText("HouseHunter");

  fireEvent.click(screen.getByRole("button", { name: "Filters" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "Show FEMA-unranked geographies" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(screen.getByRole("button", { name: "Filters · On" })).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "Community Conditions" }));
  expect(screen.getByRole("button", { name: "Filters" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Filters" }));
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  expect(screen.getByText("Not-grouped geographies always remain visible on this layer.")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Filters" }));

  fireEvent.click(screen.getByRole("button", { name: "Mountain Score" }));
  expect(screen.getByRole("button", { name: "Filters · On" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Filters · On" }));
  expect(screen.getByRole("checkbox", { name: "Show Mountain-unavailable geographies" }))
    .toBeChecked();
});

it("shows fresh loading feedback and does not refetch when closing extremes", async () => {
  const baseFetch = mockFetch();
  let extremeRequests = 0;
  const extremeStates: Array<string | null> = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v1/places" && url.searchParams.get("sort") === "risk_score") {
      extremeRequests += 1;
      extremeStates.push(url.searchParams.get("state"));
      return new Promise<Response>(() => undefined);
    }
    return baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  const trigger = screen.getByRole("button", { name: "Lowest / Highest" });
  fireEvent.click(trigger);
  expect(screen.getByRole("status")).toHaveTextContent("Loading lowest and highest");
  expect(screen.queryByRole("heading", { name: "Lowest" })).not.toBeInTheDocument();
  expect(extremeRequests).toBe(2);
  fireEvent.click(trigger);
  expect(screen.queryByRole("region", { name: "Lowest and highest risk" })).not.toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(extremeRequests).toBe(2);
  expect(extremeStates).toEqual([null, null]);
});

it("switches to Community Conditions and browses county groups without changing map grain", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findByText("HouseHunter");
  expect(screen.getByRole("button", { name: "FEMA Risk" })).toHaveAttribute("aria-pressed", "true");
  fireEvent.click(screen.getByRole("button", { name: "Community Conditions" }));
  await waitFor(() => expect(window.location.hash).toContain("metric=community-conditions"));
  const legend = await screen.findByLabelText(
    "Continuous Community Conditions color scale, Group 1 is healthiest",
  );
  expect(legend).toBeVisible();
  expect(within(legend).getByRole("img", {
    name: "Community Conditions color ramp from Group 1 to Group 10",
  })).toBeVisible();
  for (const tick of ["1", "3", "5", "7", "10"]) {
    expect(within(legend).getByText(tick)).toBeVisible();
  }
  expect(within(legend).getByText("Not grouped")).toBeVisible();
  expect(legend.querySelector(".missing-key .hatched")).toHaveAttribute("aria-hidden", "true");
  fireEvent.click(screen.getByRole("button", { name: "Best / Worst" }));
  const panel = await screen.findByRole("region", { name: "Best and worst Community Conditions" });
  expect(await within(panel).findByRole("heading", { name: "Best present · Group 2 · 1 counties" })).toBeVisible();
  expect(within(panel).getByRole("heading", { name: "Worst present · Group 2 · 1 counties" })).toBeVisible();
  fireEvent.click(within(panel).getAllByRole("button", { name: "Browse all" })[0]);
  expect(await within(panel).findByRole("heading", { name: "Group 2 · 1 counties" })).toBeVisible();
  expect(within(panel).getByText("1–1 of 1")).toBeVisible();
  fireEvent.click(within(panel).getByRole("button", { name: /Boulder.*Group 2 of 10/ }));
  const drawer = await screen.findByRole("dialog", { name: "County detail" });
  expect(screen.getByRole("button", { name: "Tracts" })).toHaveAttribute("aria-pressed", "true");
  expect(within(drawer).getByLabelText("FEMA risk")).toBeVisible();
  expect(within(drawer).getByLabelText("Community Conditions")).toHaveClass("active");
  expect(within(drawer).getByText("Group 2 of 10")).toBeVisible();
  expect(within(drawer).getByText(/Better conditions/)).toBeVisible();
});

it("shows an empty Community Conditions range for an ungrouped territory", async () => {
  window.history.replaceState(null, "", "/#metric=community-conditions&state=PR");
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v1/counties" && url.searchParams.get("sort") === "community_conditions_group") {
      return Promise.resolve(response({
        total: 1,
        items: [{ ...county, place_id: "72001", state: "PR", community_conditions_group: null }],
      }));
    }
    return baseFetch(input);
  }));

  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Community Conditions" }));
  fireEvent.click(screen.getByRole("button", { name: "Best / Worst" }));
  const panel = await screen.findByRole("region", { name: "Best and worst Community Conditions" });

  expect(await within(panel).findByText("No grouped counties in this scope.")).toBeVisible();
  expect(within(panel).queryByText(/Group null/)).not.toBeInTheDocument();
});

it("maps and filters Mountain Score with an honest expandable breakdown", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findByText("HouseHunter");

  fireEvent.click(screen.getByRole("button", { name: "Mountain Score" }));
  const legend = await screen.findByLabelText(
    "Continuous Mountain Score color scale, higher means more mountain characteristics",
  );
  expect(legend).toBeVisible();
  expect(within(legend).getByRole("img", {
    name: "Mountain Score continuous color ramp from 0 to 100",
  })).toBeVisible();
  for (const tick of ["0", "20", "40", "60", "80", "100"]) {
    expect(within(legend).getByText(tick)).toBeVisible();
  }
  expect(within(legend).getByText("Unavailable")).toBeVisible();
  expect(legend.querySelector(".missing-key .hatched")).toHaveAttribute("aria-hidden", "true");
  fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
  fireEvent.change(screen.getByLabelText("Minimum Mountain Score"), { target: { value: "80" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(window.location.hash).toContain("mountain_min=80"));

  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St, Boulder, CO" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  const drawer = await screen.findByRole("dialog", { name: "Tract detail" });
  expect(await within(drawer).findByLabelText("Mountain Score")).toHaveClass("active");
  expect(within(drawer).getByLabelText("FEMA risk").querySelector(":scope > span"))
    .toHaveStyle({ color: scoreColor(tract.risk_score)! });
  const wildfire = within(drawer).getByText("Wildfire").closest(".contribution");
  expect(wildfire?.querySelector(".bar i"))
    .toHaveStyle({ backgroundColor: scoreColor(hazards[0].percentile)! });
  fireEvent.click(within(drawer).getByText("Mountain Score breakdown"));
  expect(within(drawer).getByText(/property-specific views/)).toBeVisible();
  expect(within(drawer).getByText(/1,200 m/)).toBeVisible();
});

it("discards a stale Community Conditions page after returning to group summaries", async () => {
  const baseFetch = mockFetch();
  let resolveNext: ((response: Response) => void) | undefined;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v1/counties" && url.searchParams.get("limit") === "50") {
      if (url.searchParams.get("offset") === "50") {
        return new Promise<Response>((resolve) => { resolveNext = resolve; });
      }
      return Promise.resolve(response({ total: 51, items: [county] }));
    }
    return baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Community Conditions" }));
  fireEvent.click(screen.getByRole("button", { name: "Best / Worst" }));
  const panel = await screen.findByRole("region", { name: "Best and worst Community Conditions" });
  fireEvent.click((await within(panel).findAllByRole("button", { name: "Browse all" }))[0]);
  expect(await within(panel).findByRole("heading", { name: "Group 2 · 51 counties" })).toBeVisible();
  fireEvent.click(within(panel).getByRole("button", { name: "Next" }));
  await waitFor(() => expect(resolveNext).toBeDefined());
  fireEvent.click(within(panel).getByRole("button", { name: "← Back to groups" }));
  await act(async () => resolveNext?.(response({ total: 51, items: [county] })));
  expect(await within(panel).findByRole("heading", { name: "Best present · Group 2 · 1 counties" })).toBeVisible();
  expect(within(panel).queryByRole("heading", { name: "Group 2 · 51 counties" })).not.toBeInTheDocument();
});

it("applies state and county filters and changes geography levels", async () => {
  const request = mockFetch(); vi.stubGlobal("fetch", request); render(<App />); await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
  const panel = screen.getByRole("region", { name: "Map filters" });
  const stateSelect = within(panel).getByLabelText("State");
  expect(within(stateSelect).getAllByRole("option").map((option) => option.textContent))
    .toEqual(["All states & territories", ...STATE_ABBREVIATIONS]);
  expect(STATE_ABBREVIATIONS).toHaveLength(56);
  expect(STATE_ABBREVIATIONS).toEqual([...STATE_ABBREVIATIONS].sort());
  fireEvent.change(stateSelect, { target: { value: "CO" } });
  await waitFor(() => expect(within(panel).getByLabelText("County")).toBeEnabled());
  fireEvent.change(within(panel).getByLabelText("County"), { target: { value: "08013" } });
  fireEvent.click(within(panel).getByRole("button", { name: "Apply" }));
  expect(window.location.hash).toContain("state=CO"); expect(window.location.hash).toContain("county=08013");
  fireEvent.click(screen.getByRole("button", { name: "Counties" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Counties" })).toHaveAttribute("aria-pressed", "true"));
  expect(window.location.hash).not.toContain("county=");
});

it("locks a state-scoped build to its built state and discards incompatible deep links", async () => {
  window.history.replaceState(null, "", "/#level=tract&state=CO&county=08013&place=08013012101");
  vi.stubGlobal("fetch", mockFetch(true, { kind: "state", state: "AL" }));
  render(<App />);
  await screen.findByText("HouseHunter");
  expect(screen.queryByRole("dialog", { name: "Tract detail" })).not.toBeInTheDocument();
  await waitFor(() => expect(window.location.hash).toContain("state=AL"));
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });
  expect(mapFocusTarget("", "AL")).toEqual({ kind: "state", id: "AL" });
  fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
  const panel = screen.getByRole("region", { name: "Map filters" });
  expect(within(panel).getByLabelText("State")).toBeDisabled();
  expect(within(panel).getByLabelText("State")).toHaveValue("AL");
  fireEvent.click(within(panel).getByRole("button", { name: "Clear" }));
  expect(window.location.hash).not.toContain("state=CO");
  expect(window.location.hash).not.toContain("county=");
  expect(window.location.hash).not.toContain("place=");
});

it("looks up a street address and opens tract detail", async () => {
  vi.stubGlobal("fetch", mockFetch()); render(<App />); await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(screen.queryByRole("tab", { name: "Place / FIPS" })).not.toBeInTheDocument();
  expect(screen.getByText(/Census geocoder through this loopback server/i)).toBeVisible();
  expect(screen.getByText(/Do not submit confidential addresses/i)).toBeVisible();
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St, Boulder, CO" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  const drawer = await screen.findByRole("dialog", { name: "Tract detail" });
  expect(await within(drawer).findByText("Wildfire")).toBeVisible();
  expect(within(drawer).getByText("No rating")).toBeVisible();
  expect(window.location.hash).not.toContain("Main");
  expect(window.location.hash).toContain("state=CO");
  expect(window.location.hash).toContain("county=08013");
});

it("navigates between tract and county detail workflows", async () => {
  vi.stubGlobal("fetch", mockFetch()); render(<App />); await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St, Boulder, CO" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  const tractDrawer = await screen.findByRole("dialog", { name: "Tract detail" });
  fireEvent.click(await within(tractDrawer).findByRole("button", { name: "View Boulder county" }));
  const countyDrawer = await screen.findByRole("dialog", { name: "County detail" });
  expect(await within(countyDrawer).findByText("Wildfire")).toBeVisible();
  expect(within(countyDrawer).getByText("No rating")).toBeVisible();
  fireEvent.click(await within(countyDrawer).findByRole("button", { name: "View 12 tracts" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Tracts" })).toHaveAttribute("aria-pressed", "true"));
  expect(screen.queryByRole("dialog", { name: "County detail" })).not.toBeInTheDocument();
  expect(window.location.hash).toContain("state=CO");
  expect(window.location.hash).toContain("county=08013");
});

it("keeps preparation inside the neutral map shell", async () => {
  vi.stubGlobal("fetch", mockFetch(false)); render(<App />);
  expect(await screen.findByRole("heading", { name: "Prepare the national risk map" })).toBeVisible();
  expect(screen.getByRole("img", { name: /USA tract risk map/i })).toBeVisible();
  expect(screen.getByRole("button", { name: "Prepare national data" })).toBeVisible();
});
