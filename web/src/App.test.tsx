import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { communityLabel, mapFocusTarget, mapTooltipClass, mountainLabel, mountainRarityLabel, scoreBand, scoreLabel, scorePillLabel, sensitivityLabel, sortedHazardPercentiles, STATE_ABBREVIATIONS } from "./App";
import RiskMap from "./RiskMap";
import { cameraFromTransform, COMMUNITY_COLOR_SCALE, COMMUNITY_GROUP_COLORS, communityGroupColor, COST_OF_LIVING_COLOR_SCALE, costOfLivingColor, COUNTY_FIT_COLOR_SCALE, COUNTY_MOUNTAIN_COLOR_SCALE, countyFitColor, HAZARD_COLOR_SCALE, HOME_COSTS_COLOR_SCALE, homeBuyingPowerColor, MAP_COLORS, METRIC_COLOR_SCALES, metricColor, metricColorScale, MOUNTAIN_BAND_COLORS, MOUNTAIN_COLOR_SCALE, MOUNTAIN_COLORS, mountainColor, readHash, relativeTransform, scoreColor, transformFromCamera } from "./map";
import type { HazardPercentile, MapFilters, MapScore, PlaceSummary } from "./types";

const mapFilters: MapFilters = {
  state: "", county: "", showUnavailable: false, mountainMagnitudeMin: null,
  communityConditionsGroupMax: null, costOfLivingIndexMax: null,
  homeSqftFor1mMin: null, housingBuilt2000PlusPctMin: null,
};

const tract: PlaceSummary = {
  place_id: "08013012101", name: "08013012101", state: "CO", place_type: "tract",
  population_2020: 0, housing_units_2020: 0, res_hazard_npctl: 21.25,
  res_hazard_spread: 12.5, res_hazard_spectral: 68, res_hazard_tail: 72,
  res_hazard_power4: 65, property_loss_npctl: 44.5,
  res_hazard_data_quality: "partial", res_hazard_available_count: 16,
  res_hazard_coverage_ratio: 16 / 17, alr_npctl: 35.2, alr_valb: 0.00125,
  fema_vintage: "December 2025", census_vintage: "n/a", county_fips: "08013", county_name: "Boulder",
  community_conditions_group: 2, community_conditions_geography: "county", chrr_release_year: 2025,
  mountain_magnitude: 2.3213, mountain_magnitude_version: "mountain_magnitude_v2", mountain_pipeline_version: "mountain_pipeline_v1",
  relief_5km_m: 450, relief_10km_m: 700, relief_20km_m: 1200, relief_40km_m: 1800, relief_20km_pct: 88,
  rugged_fraction_20km: 0.65, rugged_pct: 85, public_mountain_access_raw: 25, public_mountain_access_pct: 75,
  open_mountain_km2_5: 2, open_mountain_km2_15: 7, open_mountain_km2_30: 16,
  restricted_mountain_km2_30: 1, closed_mountain_km2_30: 2, unknown_mountain_km2_30: 1,
  nearest_mountain_trail_km: 3.5, mountain_trail_km_10: 4, mountain_trail_km_25: 9,
  trail_access_raw: 6, trail_access_pct: 70, mountain_population_coverage: 1, mountain_coverage_status: "complete",
  cost_of_living_index: 107.2, cost_of_living_goods_index: 102.1,
  cost_of_living_housing_rents_index: 124.5, cost_of_living_utilities_index: 98.4,
  cost_of_living_other_services_index: 105.6, cost_of_living_geography_type: "metropolitan",
  cost_of_living_geography_id: "14500", cost_of_living_geography_name: "Boulder, CO",
  cost_of_living_release_year: 2024, cost_of_living_coverage_status: "complete",
  cost_of_living_attribution: "BEA RPP",
  home_sqft_for_1m: 2100, home_buying_power_percentile: 42,
  home_median_listing_price: 725000, home_median_listing_price_per_square_foot: 476.19,
  home_median_square_feet: 1850, home_active_listing_count: 430,
  home_market_month: "2026-08", home_costs_coverage_status: "complete",
  home_market_attribution: "Realtor.com Research Data",
  home_market_usage_notice: "For personal local use only.",
  housing_stock_total_units_estimate: 145000, housing_built_2000_plus_pct: 32.5,
  housing_built_2010_plus_pct: 18.2, housing_built_2020_plus_pct: 4.1,
  housing_median_year_built: 1988, housing_stock_release_year: 2024,
  housing_stock_coverage_status: "complete", housing_stock_attribution: "ACS 2024",
};
const county: PlaceSummary = { ...tract, place_id: "08013", name: "Boulder", place_type: "county", res_hazard_npctl: 18.5 };
const hazards: HazardPercentile[] = [
  { code: "WFIR", label: "Wildfire", percentile: 80.5, raw_alrb: 0.003, availability: "valid", fema_eal_rating: "Relatively High" },
  { code: "TSUN", label: "Tsunami", percentile: 0, raw_alrb: null, availability: "not_applicable", fema_eal_rating: "Not Applicable" },
];
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
const layers = [
  { key: "residential-hazard", display_name: "Residential Hazard Exposure", source: "HouseHunter / FEMA NRI", direction: "higher", availability: "available", vintage: "December 2025", geography: "tract and county", attribution: "FEMA NRI", notice: "Not property-level risk." },
  { key: "community-conditions", display_name: "Community Conditions", source: "CHR&R", direction: "lower", availability: "available", vintage: "2025", geography: "county; inherited by tracts", attribution: "CHR&R", notice: "Groups are not percentiles." },
  { key: "mountain", display_name: "Mountain Magnitude", source: "HouseHunter", direction: "higher", availability: "available", vintage: "fixture", geography: "tract and county", attribution: "HouseHunter", notice: "Separate peer groups." },
  { key: "cost-of-living", display_name: "Cost of Living", source: "BEA RPP", direction: "lower", availability: "available", vintage: "2024", geography: "metropolitan or U.S. nonmetropolitan; inherited by tracts", attribution: "U.S. Bureau of Economic Analysis", notice: "U.S. = 100." },
  { key: "home-costs", display_name: "Home Costs", source: "Realtor.com / ACS", direction: "higher", availability: "available", vintage: "2026-08 / ACS 2024", geography: "county market; tract and county housing stock", attribution: "Realtor.com Research Data; U.S. Census Bureau", notice: "Personal local use only." },
];
const sources = [{ source: "home_market", version: "2026-08", release: "2026-08", cached: true, sha256: "fixture", row_count: 1, stale: false, attribution: "Realtor.com Research Data", usage_notice: "For personal local use only.", coverage_status: "complete", error: null }];

function response(body: unknown) { return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }); }
function layerButton() {
  return screen.getByRole("button", { name: /^Map layer:/ });
}
function selectLayer(value: string) {
  const layer = layers.find((item) => item.key === value);
  if (!layer) throw new Error(`Unknown layer ${value}`);
  fireEvent.click(layerButton());
  fireEvent.click(within(screen.getByRole("group", { name: "Map layers" })).getByRole("button", {
    name: `${layer.display_name} — ${layer.source}`,
  }));
}

function mockFetch(
  build = true,
  buildScope: { kind: "national" | "state"; state: string | null } = { kind: "national", state: null },
  homeAvailable = true,
  homeStale = false,
  detailOverrides: Partial<PlaceSummary> = {},
) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v3/meta") return response({ app_version: "3.1.0", mutation_token: "token", reference_assets_ready: true, reference_assets_error: null, map_assets: { ready: true, error: null, schema_version: 1, release: "v1.20", manifest_url: "/map-assets/manifest.json" }, layers: homeAvailable ? layers : layers.map((layer) => layer.key === "home-costs" ? { ...layer, availability: "unavailable", vintage: "unavailable", notice: "Import an approved Realtor.com county file." } : layer), build: build ? { build_id: "fixture", place_count: 1, ranked_place_count: 1, county_count: 1, ranked_county_count: 1, source_vintages: { fema: "December 2025" }, sources: sources.map((source) => ({ ...source, stale: homeStale })), scope: buildScope } : null });
    if (url.pathname === "/map-assets/manifest.json") return response(manifest);
    if (url.pathname.includes("states.hash")) {
      const state = buildScope.state || "CO";
      return response(topology(state, state, state === "AL" ? [-88, 30] : [-106, 38]));
    }
    if (url.pathname.includes("tracts.hash") || url.pathname.includes("tracts-co.hash")) return response(topology(tract.place_id));
    if (url.pathname.includes("counties.hash")) return response(topology(county.place_id));
    if (url.pathname === "/api/v3/map/scores" || url.pathname === "/api/v3/map/scores/core") return response({ schema_version: 5, build_id: "fixture", level: url.searchParams.get("level"), scope: buildScope, columns: { place_id: [url.searchParams.get("level") === "county" ? county.place_id : tract.place_id], res_hazard_npctl: [url.searchParams.get("level") === "county" ? county.res_hazard_npctl : tract.res_hazard_npctl], community_conditions_group: [2], mountain_magnitude: [2.3213], cost_of_living_index: [107.2], home_buying_power_percentile: [homeAvailable ? 42 : null], home_sqft_for_1m: [homeAvailable ? 2100 : null], housing_built_2000_plus_pct: [32.5] } });
    if (url.pathname === "/api/v3/places" || url.pathname === "/api/v3/counties") return response({ total: 1, items: [url.pathname.includes("counties") ? county : tract] });
    if (url.pathname === `/api/v3/places/${tract.place_id}` || url.pathname === `/api/v3/counties/${county.place_id}`) return response({ summary: { ...(url.pathname.includes("counties") ? county : tract), ...detailOverrides }, methodology_notice: "HouseHunter residential hazard composite; not property-level risk.", source_notices: ["Asking-market indicator, not a sale price."], hazard_percentiles: hazards, member_tract_count: url.pathname.includes("counties") ? 12 : null });
    if (url.pathname === "/api/v3/lookup") return response({ status: "resolved", query: "1 Main", matched_address: "1 MAIN", tract_id: tract.place_id, detail: { summary: { ...tract, ...detailOverrides }, methodology_notice: "HouseHunter residential hazard composite.", source_notices: ["Asking-market indicator, not a sale price."], hazard_percentiles: hazards, member_tract_count: null }, provider: "census", precision: "house", approximate: false, attribution: null });
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
            if (!result.ok || body.schema_version !== 5 || body.build_id !== init.expectedBuildId || body.level !== init.level) {
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

it("preserves navigation that occurs while metadata is loading", async () => {
  const baseFetch = mockFetch();
  let releaseMeta: () => void = () => {};
  const pendingMeta = new Promise<void>((resolve) => { releaseMeta = resolve; });
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v3/meta") await pendingMeta;
    return baseFetch(input);
  }));
  window.history.replaceState(null, "", "/#level=tract&metric=residential-hazard");
  render(<App />);
  expect(screen.getByRole("status")).toHaveTextContent("Opening HouseHunter map");

  window.history.pushState(
    null,
    "",
    "/#level=county&metric=mountain&state=CO&cx=0.4000&cy=0.6000&z=2.000",
  );
  window.dispatchEvent(new PopStateEvent("popstate"));
  releaseMeta();

  await screen.findAllByText("HouseHunter");
  await waitFor(() => expect(layerButton()).toHaveAccessibleName(
    "Map layer: Mountain Magnitude — HouseHunter",
  ));
  expect(screen.getByRole("button", { name: "Counties" })).toHaveAttribute("aria-pressed", "true");
  expect(window.location.hash).toContain("metric=mountain");
  expect(window.location.hash).toContain("state=CO");
  expect(window.location.hash).toContain("cx=0.4000");
  expect(window.location.hash).toContain("cy=0.6000");
  expect(window.location.hash).toContain("z=2.000");
});

it("does not leak Explore failures into address Search", async () => {
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v3/places" && url.searchParams.has("direction")) {
      return Promise.resolve(new Response(JSON.stringify({ detail: "Extremes unavailable" }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      }));
    }
    return baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");

  const explore = screen.getByRole("button", { name: "Lowest / Highest" });
  fireEvent.click(explore);
  const explorePanel = screen.getByRole("region", {
    name: "Lowest and highest Residential Hazard Exposure",
  });
  expect(await within(explorePanel).findByRole("alert")).toHaveTextContent("Extremes unavailable");
  fireEvent.click(explore);
  fireEvent.click(screen.getByRole("button", { name: "Search" }));

  expect(within(screen.getByRole("region", { name: "Search" })).queryByRole("alert"))
    .not.toBeInTheDocument();
});

describe("score semantics", () => {
  it("retains qualitative score labels independently of continuous colors", () => {
    expect([0, 19.94, 19.96, 39.94, 39.96, 59.96, 79.96, 100].map(scoreBand)).toEqual([
      "low", "low", "below", "below", "typical", "high", "highest", "highest",
    ]);
    expect(scoreBand(-0.04)).toBe("low");
    expect(scoreBand(100.04)).toBe("highest");
    expect(scoreBand(-0.05)).toBeNull();
    expect(scoreBand(100.06)).toBeNull();
    expect(scoreBand(null)).toBeNull(); expect(scoreBand(101)).toBeNull();
    expect(scoreLabel({ ...tract, res_hazard_npctl: 19.96 })).toBe("20.0");
    expect(scorePillLabel({ ...tract, res_hazard_npctl: 19.96 })).toBe("20.0, below typical");
  });
  it("uses the specified model-sensitivity boundaries", () => {
    expect([9.99, 10, 20, 20.01].map(sensitivityLabel)).toEqual([
      "Low", "Moderate", "Moderate", "High",
    ]);
    expect(sensitivityLabel(null)).toBe("Unavailable");
  });
  it("sorts hazards high-to-low with unrated last", () => {
    const values: HazardPercentile[] = [
      { code: "TSUN", label: "Tsunami", percentile: null, raw_alrb: null, availability: "missing", fema_eal_rating: null },
      { code: "AVLN", label: "Avalanche", percentile: 10, raw_alrb: 1, availability: "valid", fema_eal_rating: "Relatively Low" },
      { code: "WFIR", label: "Wildfire", percentile: 80, raw_alrb: 2, availability: "valid", fema_eal_rating: "Relatively High" },
    ];
    expect(sortedHazardPercentiles(values).map((item) => item.code)).toEqual(["WFIR", "AVLN", "TSUN"]);
  });
  it("validates and clamps URL map state", () => {
    expect(readHash("#level=county&state=co&county=123&place=08013&cx=4&cy=-2&z=99")).toEqual({ level: "county", metric: "residential-hazard", state: "", county: "", place: "08013", unranked: false, mountainMagnitudeMin: null, communityConditionsGroupMax: null, costOfLivingIndexMax: null, homeSqftFor1mMin: null, housingBuilt2000PlusPctMin: null, camera: { cx: 1, cy: 0, z: 12 } });
    expect(readHash("#level=tract&state=ZZ&county=08013&place=08013012101")).toMatchObject({ state: "", county: "", place: "08013012101" });
    expect(readHash("#level=tract&state=CO&county=01001&place=01001000100")).toMatchObject({ state: "CO", county: "", place: "" });
    expect(readHash("#metric=community-conditions").metric).toBe("community-conditions");
    expect(readHash("#metric=mountain&mountain_magnitude_min=2.3")).toMatchObject({ metric: "mountain", mountainMagnitudeMin: 2.3 });
    expect(readHash("#metric=mountain&mountain_magnitude_min=100").mountainMagnitudeMin).toBe(100);
    expect(readHash("#metric=mountain&mountain_magnitude_min=-1").mountainMagnitudeMin).toBeNull();
    expect(readHash("#metric=mountain&mountain_magnitude_min=Infinity").mountainMagnitudeMin).toBeNull();
    expect(readHash("#metric=cost-of-living&max_community_conditions_group=4&cost_of_living_index_max=99.5&home_sqft_for_1m_min=2400&housing_built_2000_plus_pct_min=35")).toMatchObject({
      metric: "cost-of-living", communityConditionsGroupMax: 4, costOfLivingIndexMax: 99.5,
      homeSqftFor1mMin: 2400, housingBuilt2000PlusPctMin: 35,
    });
    expect(readHash("#metric=home-costs").metric).toBe("home-costs");
    expect(readHash("#max_community_conditions_group=11").communityConditionsGroupMax).toBeNull();
    expect(readHash("#housing_built_2000_plus_pct_min=101").housingBuilt2000PlusPctMin).toBeNull();
    expect(readHash("#cost_of_living_index_max=-1").costOfLivingIndexMax).toBeNull();
    expect(readHash("#metric=quality").metric).toBe("residential-hazard");
  });
  it("uses bounded fixed-domain color scales and honest null labels", () => {
    expect(HAZARD_COLOR_SCALE).toHaveLength(5);
    expect(MOUNTAIN_COLOR_SCALE).toHaveLength(11);
    expect(COUNTY_MOUNTAIN_COLOR_SCALE).toHaveLength(9);
    expect(COMMUNITY_COLOR_SCALE).toHaveLength(10);
    expect(COST_OF_LIVING_COLOR_SCALE).toHaveLength(8);
    expect(HOME_COSTS_COLOR_SCALE).toHaveLength(5);
    expect(COUNTY_FIT_COLOR_SCALE).toHaveLength(5);
    expect(METRIC_COLOR_SCALES["residential-hazard"]).toMatchObject({
      minimum: 0, maximum: 100, ticks: [0, 20, 40, 60, 80, 100], classes: 5,
    });
    expect(METRIC_COLOR_SCALES["community-conditions"]).toMatchObject({
      minimum: 1, maximum: 10, ticks: [1, 3, 5, 7, 10], classes: 10,
    });
    expect(METRIC_COLOR_SCALES.mountain).toMatchObject({
      minimum: 0, maximum: 5, ticks: [0, 1, 2, 3, 4, 5], classes: 10,
    });
    expect(METRIC_COLOR_SCALES["cost-of-living"]).toMatchObject({
      minimum: 80, maximum: 120, ticks: [80, 90, 100, 110, 120], classes: 8,
    });
    expect(METRIC_COLOR_SCALES["home-costs"]).toMatchObject({
      minimum: 0, maximum: 100, ticks: [0, 20, 40, 60, 80, 100], classes: 5,
    });
    expect(metricColorScale("mountain", "county")).toMatchObject({
      minimum: 0, maximum: 4, ticks: [0, 1, 2, 3, 4], classes: 8,
    });
    expect(metricColorScale("county-fit")).toMatchObject({
      minimum: 0, maximum: 1, ticks: [0, 0.2, 0.4, 0.6, 0.8, 1], classes: 5,
    });
    expect(METRIC_COLOR_SCALES["residential-hazard"].gradient.match(/#[\da-f]{6}/g)).toHaveLength(5);
    expect(METRIC_COLOR_SCALES["community-conditions"].gradient.match(/#[\da-f]{6}/g))
      .toHaveLength(10);
    expect(METRIC_COLOR_SCALES.mountain.gradient.match(/#[\da-f]{6}/g)).toHaveLength(10);
    expect(METRIC_COLOR_SCALES["cost-of-living"].gradient.match(/#[\da-f]{6}/g)).toHaveLength(8);
    expect(METRIC_COLOR_SCALES["home-costs"].gradient.match(/#[\da-f]{6}/g)).toHaveLength(5);
    expect(metricColorScale("county-fit").gradient.match(/#[\da-f]{6}/g)).toHaveLength(5);
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
    expect(mountainColor(1)).toBe(MOUNTAIN_COLORS.below);
    expect(mountainColor(2)).toBe(MOUNTAIN_COLORS.typical);
    expect(mountainColor(3)).toBe(MOUNTAIN_COLORS.high);
    expect(mountainColor(4)).toBe(MOUNTAIN_COLORS.highest);
    expect(mountainColor(5)).toBe(MOUNTAIN_COLORS.summit);
    expect(mountainColor(100)).toBe(MOUNTAIN_COLORS.summit);
    expect(mountainColor(100, "county")).toBe(MOUNTAIN_COLORS.summit);
    expect(mountainColor(0.49)).toBe(MOUNTAIN_BAND_COLORS[0]);
    expect(mountainColor(0.5)).toBe(MOUNTAIN_BAND_COLORS[1]);
    expect(mountainColor(0.99)).toBe(MOUNTAIN_BAND_COLORS[1]);
    expect(mountainColor(1)).toBe(MOUNTAIN_BAND_COLORS[2]);
    for (const magnitude of [0, 0.5, 1, 2.3213, 3.9]) {
      expect(mountainColor(magnitude, "tract")).toBe(mountainColor(magnitude, "county"));
    }
    expect(COMMUNITY_GROUP_COLORS).toHaveLength(10);
    expect(communityGroupColor(1)).toBe(COMMUNITY_GROUP_COLORS[0]);
    expect(communityGroupColor(10)).toBe(COMMUNITY_GROUP_COLORS[9]);
    expect(communityGroupColor(1.5)).toBeNull();
    expect(communityGroupColor(null)).toBeNull();
    expect(costOfLivingColor(80)).toBe(costOfLivingColor(70));
    expect(costOfLivingColor(120)).toBe(costOfLivingColor(130));
    expect(costOfLivingColor(80)).not.toBe(costOfLivingColor(120));
    expect(costOfLivingColor(null)).toBeNull();
    expect(homeBuyingPowerColor(0)).not.toBe(homeBuyingPowerColor(100));
    expect(homeBuyingPowerColor(-1)).toBeNull();
    expect(homeBuyingPowerColor(101)).toBeNull();
    expect(countyFitColor(0)).not.toBe(countyFitColor(1));
    expect(countyFitColor(-1e-9)).toBeNull();
    expect(countyFitColor(1 + 1e-9)).toBeNull();
    expect(countyFitColor(null)).toBeNull();
    expect(communityLabel({ ...county, community_conditions_group: null })).toBe("Not grouped");
    expect(mountainLabel(tract)).toBe("M2.32");
    expect(mountainRarityLabel(0, "county")).toBe("≈ top 100% of U.S. counties by base exposure");
    expect(mountainRarityLabel(1, "county")).toBe("≈ top 10% of U.S. counties by base exposure");
    expect(mountainRarityLabel(2.3213, "tract")).toBe("≈ top 0.48% of U.S. tracts by base exposure");
    expect(mountainRarityLabel(5, "tract")).toBe("≈ top 0.001% of U.S. tracts by base exposure");
  });
  it("uses high-contrast half-magnitude mountain bands", () => {
    expect(scoreColor(12.5)).toBe(MAP_COLORS.low);
    expect(scoreColor(37.5)).toBe(MAP_COLORS.below);
    for (const boundary of [20, 40, 60, 80]) {
      expect(scoreColor(boundary - 0.1)).not.toBe(scoreColor(boundary + 0.1));
    }
    for (const boundary of [0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4]) {
      expect(mountainColor(boundary - 0.1)).not.toBe(mountainColor(boundary + 0.1));
    }
    const femaColors = new Set(Array.from({ length: 10_001 }, (_, index) => scoreColor(index / 100)));
    const mountainColors = new Set(Array.from({ length: 501 }, (_, index) => mountainColor(index / 100)));
    const costColors = new Set(Array.from({ length: 401 }, (_, index) => costOfLivingColor(80 + index / 10)));
    const homeColors = new Set(Array.from({ length: 10_001 }, (_, index) => homeBuyingPowerColor(index / 100)));
    const fitColors = new Set(Array.from({ length: 1_001 }, (_, index) => countyFitColor(index / 1000)));
    expect(femaColors.size).toBe(5);
    expect(mountainColors.size).toBe(11);
    expect(costColors.size).toBe(8);
    expect(homeColors.size).toBe(5);
    expect(fitColors.size).toBe(5);
  });
  it("changes color at every exact half-magnitude boundary and only clamps the upper endpoint", () => {
    for (let band = 1; band < MOUNTAIN_BAND_COLORS.length; band += 1) {
      const boundary = band / 2;
      expect(mountainColor(boundary - 1e-9, "tract")).toBe(MOUNTAIN_BAND_COLORS[band - 1]);
      expect(mountainColor(boundary, "tract")).toBe(MOUNTAIN_BAND_COLORS[band]);
    }
    expect(mountainColor(4 - 1e-9, "county")).toBe(MOUNTAIN_BAND_COLORS[7]);
    expect(mountainColor(4, "county")).toBe(MOUNTAIN_COLORS.summit);
    expect(mountainColor(4 + 1e-9, "county")).toBe(MOUNTAIN_COLORS.summit);
    expect(mountainColor(5 - 1e-9, "tract")).toBe(MOUNTAIN_BAND_COLORS[9]);
    expect(mountainColor(5, "tract")).toBe(MOUNTAIN_BAND_COLORS[10]);
    expect(mountainColor(5 + 1e-9, "tract")).toBe(MOUNTAIN_BAND_COLORS[10]);
    expect(mountainColor(-1e-9, "tract")).toBeNull();
    expect(mountainColor(-1e-9, "county")).toBeNull();
  });
  it("shares every county color with tracts while retaining the tract-only summit bands", () => {
    for (let band = 0; band <= 7; band += 1) {
      const magnitude = band / 2;
      expect(mountainColor(magnitude, "county")).toBe(MOUNTAIN_BAND_COLORS[band]);
      expect(mountainColor(magnitude, "county")).toBe(mountainColor(magnitude, "tract"));
    }
    expect(COUNTY_MOUNTAIN_COLOR_SCALE).toEqual([...MOUNTAIN_BAND_COLORS.slice(0, 8), MOUNTAIN_COLORS.summit]);
    expect(MOUNTAIN_COLOR_SCALE.slice(10)).toEqual([MOUNTAIN_COLORS.summit]);
  });
  it("keeps legend intervals aligned with map colors and separates the terminal cap", () => {
    const tractScale = metricColorScale("mountain", "tract");
    const countyScale = metricColorScale("mountain", "county");
    expect(tractScale.gradient).toContain(`${mountainColor(4.9235, "tract")} 90% 100%`);
    expect(tractScale.gradient).not.toContain(MOUNTAIN_BAND_COLORS[10]);
    expect(countyScale.gradient).toContain(`${mountainColor(3.4973, "county")} 75% 87.5%`);
    expect(countyScale.gradient).not.toContain(MOUNTAIN_COLORS.summit);
  });
  it("formats approximate peer rarity at release extrema and rejects invalid magnitudes", () => {
    expect(mountainRarityLabel(3.4973, "county"))
      .toBe("≈ top 0.032% of U.S. counties by base exposure");
    expect(mountainRarityLabel(4.9235, "tract"))
      .toBe("≈ top 0.0012% of U.S. tracts by base exposure");
    for (const invalid of [null, -Number.EPSILON, Number.NaN, Number.NEGATIVE_INFINITY, Number.POSITIVE_INFINITY]) {
      expect(mountainRarityLabel(invalid, "tract")).toBe("Rarity unavailable");
    }
  });
  it("rejects every invalid scale value and keeps Community groups on official integer anchors", () => {
    for (const invalid of [Number.NEGATIVE_INFINITY, Number.POSITIVE_INFINITY, Number.NaN, -0.1]) {
      expect(scoreColor(invalid)).toBeNull();
      expect(mountainColor(invalid)).toBeNull();
    }
    expect(scoreColor(100.1)).toBeNull();
    expect(mountainColor(100.1)).toBe(MOUNTAIN_COLORS.summit);
    expect(Array.from({ length: 10 }, (_, index) => communityGroupColor(index + 1)))
      .toEqual([...COMMUNITY_GROUP_COLORS]);
    for (const invalid of [Number.NEGATIVE_INFINITY, Number.POSITIVE_INFINITY, Number.NaN, 0, 1.5, 11]) {
      expect(communityGroupColor(invalid)).toBeNull();
    }
    for (const invalid of [Number.NEGATIVE_INFINITY, Number.POSITIVE_INFINITY, Number.NaN, -0.1, 1.1]) {
      expect(countyFitColor(invalid)).toBeNull();
    }
  });
  it("uses the fixed shared scales on the map independently of unrelated metric values", () => {
    const row: MapScore = {
      place_id: "08013012101",
      res_hazard_npctl: 21.25,
      community_conditions_group: 2,
      mountain_magnitude: 2.3213,
      cost_of_living_index: 107.2,
      home_buying_power_percentile: 42,
      home_sqft_for_1m: 2100,
      housing_built_2000_plus_pct: 32.5,
    };
    expect(metricColor(row, "residential-hazard")).toBe(scoreColor(21.25));
    expect(metricColor({ ...row, mountain_magnitude: 0 }, "residential-hazard")).toBe(scoreColor(21.25));
    expect(metricColor(row, "mountain")).toBe(mountainColor(2.3213));
    expect(metricColor({ ...row, res_hazard_npctl: 100 }, "mountain")).toBe(mountainColor(2.3213));
    expect(metricColor(row, "community-conditions")).toBe(communityGroupColor(2));
    expect(metricColor(row, "cost-of-living")).toBe(costOfLivingColor(107.2));
    expect(metricColor(row, "home-costs")).toBe(homeBuyingPowerColor(42));
    expect(metricColor(null, "residential-hazard")).toBeNull();
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
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v3/map/scores?level=tract" expectedBuildId="fixture" level="tract"
    selected="" filters={mapFilters} focusTarget={null}
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
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v3/map/scores?level=tract" expectedBuildId="fixture" level="tract"
    selected="" filters={mapFilters} focusTarget={null}
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
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v3/map/scores?level=tract"
    expectedBuildId="fixture" level="tract" selected="" filters={mapFilters}
    focusTarget={null} initialCamera={{ cx: 0.5, cy: 0.5, z: 2 }}
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

it.each(["SCORES_READY", "ADDON_READY"] as const)(
  "suspends picking after %s until the replacement frame commits",
  async (eventType) => {
    vi.stubGlobal("fetch", mockFetch());
    render(<RiskMap
      manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v3/map/scores?level=tract"
      expectedBuildId="fixture" level="tract" selected="" filters={mapFilters}
      focusTarget={null} initialCamera={{ cx: 0.5, cy: 0.5, z: 1 }}
      onSelect={() => undefined} onPreview={() => undefined} onCamera={() => undefined}
      onStatus={() => undefined}
    />);
    const viewport = screen.getByRole("img", { name: /Focusable USA tract/ });
    await waitFor(() => expect(viewport).toHaveAttribute("aria-busy", "false"));
    const init = workerMessages.find(({ value }) => value.type === "INIT")!.value;
    workerMessages.length = 0;
    act(() => mockWorkers[0].deliver({
      type: eventType, datasetGeneration: init.datasetGeneration, count: 1,
      kind: "cost-of-living",
    }));
    fireEvent.pointerDown(viewport, {
      pointerId: 1, pointerType: "mouse", isPrimary: true,
      button: 0, buttons: 1, clientX: 500, clientY: 350,
    });
    fireEvent.pointerUp(viewport, {
      pointerId: 1, pointerType: "mouse", isPrimary: true,
      button: 0, buttons: 0, clientX: 500, clientY: 350,
    });
    expect(workerMessages.filter(({ value }) => value.type === "PICK")).toEqual([]);
  },
);

it("ignores a stale score response after changing geography level", async () => {
  let resolveTract!: (value: Response) => void;
  let resolveCounty!: (value: Response) => void;
  const tractResponse = new Promise<Response>((resolve) => { resolveTract = resolve; });
  const countyResponse = new Promise<Response>((resolve) => { resolveCounty = resolve; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v3/map/scores/core") {
      return url.searchParams.get("level") === "county" ? countyResponse : tractResponse;
    }
    return baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Counties" }));
  resolveCounty(response({ schema_version: 5, build_id: "fixture", level: "county", scope: { kind: "national", state: null }, columns: { place_id: [county.place_id], res_hazard_npctl: [county.res_hazard_npctl], community_conditions_group: [2], mountain_magnitude: [2.3213], cost_of_living_index: [107.2], home_buying_power_percentile: [42], home_sqft_for_1m: [2100], housing_built_2000_plus_pct: [32.5] } }));
  await waitFor(() => expect(screen.getByTitle("fixture")).toHaveTextContent("counties"));
  await act(async () => resolveTract(response({ schema_version: 5, build_id: "fixture", level: "tract", scope: { kind: "national", state: null }, columns: { place_id: [tract.place_id], res_hazard_npctl: [tract.res_hazard_npctl], community_conditions_group: [2], mountain_magnitude: [2.3213], cost_of_living_index: [107.2], home_buying_power_percentile: [42], home_sqft_for_1m: [2100], housing_built_2000_plus_pct: [32.5] } })));
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
    if (url.pathname === "/api/v3/map/scores/core") {
      return url.searchParams.get("level") === "county" ? countyResponse : tractResponse;
    }
    return baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Counties" }));
  resolveCounty(response({ schema_version: 5, build_id: "fixture", level: "county", scope: { kind: "national", state: null }, columns: { place_id: [county.place_id], res_hazard_npctl: [county.res_hazard_npctl], community_conditions_group: [2], mountain_magnitude: [2.3213], cost_of_living_index: [107.2], home_buying_power_percentile: [42], home_sqft_for_1m: [2100], housing_built_2000_plus_pct: [32.5] } }));
  await waitFor(() => expect(screen.getByTitle("fixture")).toHaveTextContent("counties"));
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
    if (url.pathname !== "/api/v3/map/scores/core") return baseFetch(input);
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
    schema_version: 5,
    build_id: "fixture",
    level: "county",
    scope: { kind: "national", state: null },
    columns: {
      place_id: [county.place_id],
      res_hazard_npctl: [county.res_hazard_npctl],
      community_conditions_group: [2],
      mountain_magnitude: [2.3213],
      cost_of_living_index: [107.2],
      home_buying_power_percentile: [42],
      home_sqft_for_1m: [2100],
      housing_built_2000_plus_pct: [32.5],
    },
  }));
  await waitFor(() => expect(map).toHaveAttribute("aria-busy", "false"));
  expect(screen.getByTitle("fixture")).toHaveTextContent("counties");
});

it("restarts the map score workers when retrying a failed schema-5 request", async () => {
  let attempts = 0;
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname !== "/api/v3/map/scores/core") return baseFetch(input);
    attempts += 1;
    if (attempts === 1) {
      return Promise.resolve(new Response(JSON.stringify({ detail: "temporary score failure" }), {
        status: 503,
        headers: { "Content-Type": "application/json" },
      }));
    }
    return baseFetch(input);
  }));

  render(<App />);
  const recovery = await screen.findByText("Scores could not be loaded");
  fireEvent.click(recovery.closest("section")!.querySelector("button")!);

  await waitFor(() => expect(attempts).toBe(2));
  await waitFor(() => expect(screen.queryByText("Scores could not be loaded")).not.toBeInTheDocument());
});

it("ignores county options loaded for a previous draft state", async () => {
  let resolveCO!: (value: Response) => void;
  let resolveAL!: (value: Response) => void;
  const coResponse = new Promise<Response>((resolve) => { resolveCO = resolve; });
  const alResponse = new Promise<Response>((resolve) => { resolveAL = resolve; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v3/counties" && url.searchParams.has("state")) {
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
    if (url.pathname === "/api/v3/counties" && url.searchParams.has("state")) {
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
    return url.pathname === "/api/v3/lookup" ? lookup : baseFetch(input);
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
    return url.pathname === "/api/v3/lookup" ? lookup : baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  const search = screen.getByRole("button", { name: "Search" });
  fireEvent.click(search);
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  fireEvent.click(search);
  await act(async () => resolveLookup(response({ status: "resolved", query: "1 Main St", matched_address: "1 MAIN", tract_id: tract.place_id, detail: { summary: tract, methodology_notice: "HouseHunter residential hazard composite.", hazard_percentiles: hazards, member_tract_count: null, source_notices: [] }, provider: "census", precision: "house", approximate: false, attribution: null })));
  expect(screen.queryByRole("dialog", { name: "Tract detail" })).not.toBeInTheDocument();
});

it("discards a resolved address after browser history closes Search", async () => {
  let resolveLookup!: (value: Response) => void;
  const lookup = new Promise<Response>((resolve) => { resolveLookup = resolve; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    return url.pathname === "/api/v3/lookup" ? lookup : baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  act(() => window.dispatchEvent(new PopStateEvent("popstate")));
  expect(screen.queryByLabelText("Search")).not.toBeInTheDocument();
  await act(async () => resolveLookup(response({ status: "resolved", query: "1 Main St", matched_address: "1 MAIN", tract_id: tract.place_id, detail: { summary: tract, methodology_notice: "HouseHunter residential hazard composite.", hazard_percentiles: hazards, member_tract_count: null, source_notices: [] }, provider: "census", precision: "house", approximate: false, attribution: null })));
  expect(screen.queryByRole("dialog", { name: "Tract detail" })).not.toBeInTheDocument();
});

it("does not surface a stale address failure after Search closes", async () => {
  let rejectLookup!: (reason: Error) => void;
  const lookup = new Promise<Response>((_, reject) => { rejectLookup = reject; });
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    return url.pathname === "/api/v3/lookup" ? lookup : baseFetch(input);
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
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v3/map/scores?level=tract"
    expectedBuildId="fixture" level="tract" selected="" filters={mapFilters}
    focusTarget={null} initialCamera={{ cx: 0.5, cy: 0.5, z: 1 }}
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
    semanticGeneration: current.semanticGeneration, snapshotId: 44, metric: "residential-hazard",
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
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v3/map/scores?level=tract"
    expectedBuildId="fixture" level="tract" selected="" filters={mapFilters}
    focusTarget={{ kind: "place", id: tract.place_id, nonce: 12 }}
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
    semanticGeneration: current.semanticGeneration, snapshotId, metric: "residential-hazard",
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
    metric: "residential-hazard",
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
    manifestUrl="/map-assets/manifest.json" scoreUrl="/api/v3/map/scores?level=tract"
    expectedBuildId="fixture" level="tract" selected="" filters={mapFilters}
    focusTarget={{ kind: "state", id: "CO", nonce: 9 }}
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
  expect(within(exports).getByRole("link", { name: "Tracts CSV" })).toHaveAttribute("href", "/api/v3/exports/places.csv");
  expect(within(exports).getByRole("link", { name: "Tracts Parquet" })).toHaveAttribute("href", "/api/v3/exports/places.parquet");
  expect(within(exports).getByRole("link", { name: "Counties CSV" })).toHaveAttribute("href", "/api/v3/exports/counties.csv");
  expect(within(exports).getByRole("link", { name: "Counties Parquet" })).toHaveAttribute("href", "/api/v3/exports/counties.parquet");
  const legend = screen.getByLabelText("Stepped Residential Hazard Exposure color scale with 5 classes, higher is worse");
  expect(legend).toHaveTextContent("not property-level risk");
  expect(legend.querySelector(".legend-gradient")).toHaveStyle({
    backgroundImage: expect.stringContaining("linear-gradient"),
  });
  expect(within(legend).getByRole("img", {
    name: "Residential Hazard Exposure stepped color scale with 5 classes from 0 to 100",
  })).toBeVisible();
  for (const tick of ["0", "20", "40", "60", "80", "100"]) {
    expect(within(legend).getByText(tick)).toBeVisible();
  }
  expect(within(legend).getByText("Unranked")).toBeVisible();
  expect(legend.querySelector(".missing-key .hatched")).toHaveAttribute("aria-hidden", "true");
  expect(screen.queryByLabelText(/^Continuous /)).not.toBeInTheDocument();
  expect(screen.getByRole("img", { name: /focusable USA tract Residential Hazard Exposure map/i })).toBeVisible();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByText("Next")).not.toBeInTheDocument();
});

it("offers all five independent layers in the keyboard-accessible menu", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findByText("HouseHunter");
  const trigger = layerButton();
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("combobox", { name: "Map layer" })).not.toBeInTheDocument();
  fireEvent.click(trigger);
  const menu = screen.getByRole("group", { name: "Map layers" });
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  expect(within(menu).getAllByRole("button").map((option) => option.getAttribute("aria-label"))).toEqual([
    "Residential Hazard Exposure — HouseHunter / FEMA NRI",
    "Community Conditions — CHR&R",
    "Mountain Magnitude — HouseHunter",
    "Cost of Living — BEA RPP",
    "Home Costs — Realtor.com / ACS",
  ]);
  const selected = within(menu).getByRole("button", { name: "Residential Hazard Exposure — HouseHunter / FEMA NRI" });
  expect(selected).toHaveAttribute("aria-pressed", "true");
  await waitFor(() => expect(selected).toHaveFocus());
  fireEvent.click(selected);
  expect(screen.queryByRole("group", { name: "Map layers" })).not.toBeInTheDocument();
  expect(trigger).toHaveAccessibleName("Map layer: Residential Hazard Exposure — HouseHunter / FEMA NRI");
  fireEvent.click(trigger);
  const reopenedMenu = screen.getByRole("group", { name: "Map layers" });
  const reopened = within(reopenedMenu)
    .getByRole("button", { name: "Residential Hazard Exposure — HouseHunter / FEMA NRI" });
  await waitFor(() => expect(reopened).toHaveFocus());
  fireEvent.keyDown(reopened, { key: "ArrowDown" });
  expect(within(reopenedMenu).getByRole("button", { name: "Community Conditions — CHR&R" })).toHaveFocus();
  fireEvent.keyDown(window, { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("group", { name: "Map layers" })).not.toBeInTheDocument());
  await waitFor(() => expect(trigger).toHaveFocus());
  fireEvent.click(trigger);
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(screen.queryByRole("group", { name: "Map layers" })).not.toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Search" })).toBeVisible();
});

it("applies every cross-layer filter atomically and persists it in the URL", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });
  fireEvent.click(screen.getByRole("button", { name: "Filters" }));
  fireEvent.change(screen.getByLabelText("Minimum Mountain Magnitude"), { target: { value: "1.5" } });
  fireEvent.change(screen.getByLabelText("Maximum Community Conditions group"), { target: { value: "4" } });
  fireEvent.change(screen.getByLabelText("Maximum Cost of Living RPP"), { target: { value: "99.5" } });
  fireEvent.change(screen.getByLabelText("Minimum square feet for $1M"), { target: { value: "2400" } });
  fireEvent.change(screen.getByLabelText("Minimum built 2000+ share (%)"), { target: { value: "35" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(window.location.hash).toContain("housing_built_2000_plus_pct_min=35"));
  expect(window.location.hash).toContain("max_community_conditions_group=4");
  expect(window.location.hash).toContain("cost_of_living_index_max=99.5");
  expect(window.location.hash).toContain("home_sqft_for_1m_min=2400");
  const semantic = workerMessages.filter(({ value }) => value.type === "SET_SEMANTICS").at(-1)?.value.semantics;
  expect(semantic).toMatchObject({
    mountainMagnitudeMin: 1.5, communityConditionsGroupMax: 4,
    costOfLivingIndexMax: 99.5, homeSqftFor1mMin: 2400,
    housingBuilt2000PlusPctMin: 35,
  });
});

it("renders Cost of Living and Home Costs legends and a five-card detail", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });
  selectLayer("cost-of-living");
  const costLegend = await screen.findByLabelText(
    "Stepped Cost of Living color scale with 8 classes from 80 to 120, lower is better, U.S. equals 100",
  );
  expect(within(costLegend).getByText("100 · U.S.")).toBeVisible();
  expect(costLegend).toHaveTextContent("BEA RPP · 2024");
  selectLayer("home-costs");
  expect(await screen.findByLabelText(
    "Stepped Home Costs national buying power percentile color scale with 5 classes, higher is better",
  )).toHaveTextContent("asking-market indicator");
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St, Boulder, CO" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  const drawer = await screen.findByRole("dialog", { name: "Tract detail" });
  const cards = drawer.querySelectorAll(".metric-card");
  expect(cards).toHaveLength(5);
  expect(cards[0]).toHaveAttribute("aria-label", "Home Costs");
  expect(cards[0]).toHaveClass("active");
  expect(within(drawer).getByLabelText("Cost of Living")).toHaveTextContent("BEA Boulder, CO MSA");
  expect(within(drawer).getByLabelText("Home Costs")).toHaveTextContent("Built 2000+33%");
  expect(within(drawer).getByLabelText("Home Costs")).toHaveTextContent("ACS 2024 five-year estimates");
  expect(within(drawer).getByText("Asking-market indicator, not a sale price.")).toBeVisible();
});

it("reports unavailable ACS housing-stock assets without claiming direct estimates", async () => {
  vi.stubGlobal("fetch", mockFetch(true, { kind: "national", state: null }, true, false, {
    housing_stock_total_units_estimate: null,
    housing_built_2000_plus_pct: null,
    housing_built_2010_plus_pct: null,
    housing_built_2020_plus_pct: null,
    housing_median_year_built: null,
    housing_stock_release_year: null,
    housing_stock_coverage_status: "asset_unavailable",
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  selectLayer("home-costs");
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(screen.getByText(/result shows tract context, never a property marker/i)).toBeVisible();
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St, Boulder, CO" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  const homeCard = await within(await screen.findByRole("dialog", { name: "Tract detail" }))
    .findByLabelText("Home Costs");
  expect(homeCard).toHaveTextContent("Housing stock coverage: asset unavailable (ACS housing-stock asset)");
  expect(homeCard).not.toHaveTextContent("Housing stock uses direct");
});

it("surfaces a stale Home Costs market release in the information panel", async () => {
  vi.stubGlobal("fetch", mockFetch(true, { kind: "national", state: null }, true, true));
  render(<App />);
  await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Information" }));
  const staleNotice = (await screen.findByText("Home Costs market release is stale.", { exact: false }))
    .closest("p");
  expect(staleNotice).toHaveTextContent("2026-08 release is older than 62 days");
});

it("carries live Home Costs staleness from metadata into tract details", async () => {
  vi.stubGlobal("fetch", mockFetch(true, { kind: "national", state: null }, true, true));
  render(<App />);
  await screen.findByText("HouseHunter");
  selectLayer("home-costs");
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  fireEvent.change(screen.getByLabelText("Street address"), {
    target: { value: "1 Main St, Boulder, CO" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));

  const homeCard = await within(await screen.findByRole("dialog", { name: "Tract detail" }))
    .findByLabelText("Home Costs");
  expect(homeCard).toHaveTextContent("Market release is stale.");
});

it("orders Home Costs extremes by square feet with most before least", async () => {
  const baseFetch = mockFetch();
  const directions: string[] = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v3/places"
      && url.searchParams.get("sort") === "home_sqft_for_1m") {
      const direction = url.searchParams.get("direction") || "";
      directions.push(direction);
      const item = direction === "desc"
        ? { ...tract, name: "Most buying power", home_sqft_for_1m: 5000 }
        : { ...tract, name: "Least buying power", home_sqft_for_1m: 1000 };
      return Promise.resolve(response({ total: 1, items: [item] }));
    }
    return baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  selectLayer("home-costs");
  fireEvent.click(screen.getByRole("button", { name: "Most / Least" }));
  const panel = await screen.findByRole("region", { name: "Most and least square feet for $1M" });
  const most = await within(panel).findByRole("heading", { name: "Most square feet for $1M" });
  const least = within(panel).getByRole("heading", { name: "Least square feet for $1M" });
  expect(most.closest("section")).toHaveTextContent(/Most buying power.*5,000 sq ft \/ \$1M/s);
  expect(least.closest("section")).toHaveTextContent(/Least buying power.*1,000 sq ft \/ \$1M/s);
  expect(directions.sort()).toEqual(["asc", "desc"]);
});

it("keeps an unavailable Home Costs layer selectable and explains the local import", async () => {
  vi.stubGlobal("fetch", mockFetch(true, { kind: "national", state: null }, false));
  render(<App />);
  await screen.findByText("HouseHunter");
  selectLayer("home-costs");
  const legend = await screen.findByLabelText(
    "Stepped Home Costs national buying power percentile color scale with 5 classes, higher is better",
  );
  expect(layerButton()).toHaveAccessibleName("Map layer: Home Costs — Realtor.com / ACS");
  expect(legend).toHaveTextContent("Unavailable in this snapshot");
  fireEvent.click(screen.getByRole("button", { name: "Information" }));
  expect(screen.getByRole("region", { name: "About this map" })).toHaveTextContent(
    "this map never blends dimensions into one score",
  );
  expect(screen.getByRole("region", { name: "About this map" })).toHaveTextContent(
    "househunter top-counties",
  );
  expect(screen.getByRole("region", { name: "About this map" })).toHaveTextContent(
    "househunter import-home-market FILE --acknowledge-personal-use",
  );
});

it("keeps the committed legend and accessible map description aligned during metric rendering", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });
  const canvas = screen.getByRole("img", { name: /focusable USA tract Residential Hazard Exposure map/i });

  selectLayer("mountain");

  expect(screen.getByText("Updating Mountain Magnitude map…")).toBeVisible();
  expect(canvas).toHaveAttribute("aria-busy", "true");
  expect(canvas).toHaveAccessibleName(/tract Residential Hazard Exposure map/i);
  expect(screen.getByLabelText("Stepped Residential Hazard Exposure color scale with 5 classes, higher is worse")).toBeVisible();
  expect(screen.queryByLabelText(/Stepped Mountain Magnitude color scale/)).not.toBeInTheDocument();

  expect(await screen.findByLabelText(
    "Stepped Mountain Magnitude color scale for U.S. tracts, higher means fewer equal-or-higher peers",
  )).toBeVisible();
  await waitFor(() => expect(canvas).toHaveAttribute("aria-busy", "false"));
  expect(canvas).toHaveAccessibleName(/tract Mountain Magnitude map/i);
  expect(document.querySelector(".map-updating")).not.toBeInTheDocument();
});

it("commits only the final metric after rapid successive map changes", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });
  const canvas = screen.getByRole("img", { name: /focusable USA tract Residential Hazard Exposure map/i });

  selectLayer("mountain");
  selectLayer("community-conditions");

  expect(canvas).toHaveAttribute("aria-busy", "true");
  expect(screen.getByLabelText("Stepped Residential Hazard Exposure color scale with 5 classes, higher is worse")).toBeVisible();
  const finalLegend = await screen.findByLabelText(
    "Stepped Community Conditions color scale with 10 classes, Group 1 is healthiest",
  );
  expect(finalLegend).toBeVisible();
  await waitFor(() => expect(canvas).toHaveAttribute("aria-busy", "false"));
  expect(canvas).toHaveAccessibleName(/tract Community Conditions map/i);
  expect(screen.queryByLabelText(/Stepped Mountain Magnitude color scale/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Updating .* map…/)).not.toBeInTheDocument();
});

it("finishes a metric transition when active filters leave no interactive geographies", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findAllByText("1 tracts interactive", {}, { timeout: 3000 });

  fireEvent.click(screen.getByRole("button", { name: "Filters" }));
  fireEvent.change(screen.getByLabelText("Minimum Mountain Magnitude"), { target: { value: "100" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  selectLayer("mountain");

  const canvas = screen.getByRole("img", { name: /focusable USA tract/i });
  expect(await screen.findByLabelText(
    "Stepped Mountain Magnitude color scale for U.S. tracts, higher means fewer equal-or-higher peers",
  )).toBeVisible();
  await waitFor(() => expect(canvas).toHaveAttribute("aria-busy", "false"));
  expect(document.querySelector(".map-updating")).not.toBeInTheDocument();
});

it("scopes show-unavailable to the active layer and keeps cross-layer filters", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findByText("HouseHunter");

  fireEvent.click(screen.getByRole("button", { name: "Filters" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "Show hazard-unranked geographies" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  expect(screen.getByRole("button", { name: "Filters · On" })).toBeVisible();

  selectLayer("community-conditions");
  expect(screen.getByRole("button", { name: "Filters" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Filters" }));
  expect(screen.getByRole("checkbox", { name: "Show Community-ungrouped geographies" }))
    .not.toBeChecked();
  expect(screen.getByText(/Explicit metric thresholds always exclude unavailable values/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Filters" }));

  selectLayer("mountain");
  expect(screen.getByRole("button", { name: "Filters" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Filters" }));
  expect(screen.getByRole("checkbox", { name: "Show Mountain-unavailable geographies" }))
    .not.toBeChecked();
  fireEvent.click(screen.getByRole("checkbox", { name: "Show Mountain-unavailable geographies" }));
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  fireEvent.click(layerButton());
  fireEvent.click(within(screen.getByRole("group", { name: "Map layers" })).getByRole("button", {
    name: "Mountain Magnitude — HouseHunter",
  }));
  expect(screen.getByRole("button", { name: "Filters · On" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Filters · On" }));
  expect(screen.getByRole("checkbox", { name: "Show Mountain-unavailable geographies" })).toBeChecked();
});

it("shows fresh loading feedback and does not refetch when closing extremes", async () => {
  const baseFetch = mockFetch();
  let extremeRequests = 0;
  const extremeStates: Array<string | null> = [];
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v3/places" && url.searchParams.get("sort") === "res_hazard_npctl") {
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
  expect(screen.queryByRole("region", { name: "Lowest and highest Residential Hazard Exposure" })).not.toBeInTheDocument();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(extremeRequests).toBe(2);
  expect(extremeStates).toEqual([null, null]);
});

it("switches to Community Conditions and browses county groups without changing map grain", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findByText("HouseHunter");
  expect(layerButton()).toHaveAccessibleName("Map layer: Residential Hazard Exposure — HouseHunter / FEMA NRI");
  selectLayer("community-conditions");
  await waitFor(() => expect(window.location.hash).toContain("metric=community-conditions"));
  const legend = await screen.findByLabelText(
    "Stepped Community Conditions color scale with 10 classes, Group 1 is healthiest",
  );
  expect(legend).toBeVisible();
  expect(within(legend).getByRole("img", {
    name: "Community Conditions stepped color scale from Group 1 to Group 10",
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
  expect(within(drawer).getByLabelText("Residential Hazard Exposure")).toBeVisible();
  expect(within(drawer).getByLabelText("Community Conditions")).toHaveClass("active");
  expect(within(drawer).getByText("Group 2 of 10")).toBeVisible();
  expect(within(drawer).getByText(/County geography/)).toBeVisible();
});

it("shows an empty Community Conditions range for an ungrouped territory", async () => {
  window.history.replaceState(null, "", "/#metric=community-conditions&state=PR");
  const baseFetch = mockFetch();
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v3/counties" && url.searchParams.get("sort") === "community_conditions_group") {
      return Promise.resolve(response({
        total: 1,
        items: [{ ...county, place_id: "72001", state: "PR", community_conditions_group: null }],
      }));
    }
    return baseFetch(input);
  }));

  render(<App />);
  await screen.findByText("HouseHunter");
  selectLayer("community-conditions");
  fireEvent.click(screen.getByRole("button", { name: "Best / Worst" }));
  const panel = await screen.findByRole("region", { name: "Best and worst Community Conditions" });

  expect(await within(panel).findByText("No grouped counties in this scope.")).toBeVisible();
  expect(within(panel).queryByText(/Group null/)).not.toBeInTheDocument();
});

it("maps and filters Mountain Magnitude with an honest expandable breakdown", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findByText("HouseHunter");

  selectLayer("mountain");
  const legend = await screen.findByLabelText(
    "Stepped Mountain Magnitude color scale for U.S. tracts, higher means fewer equal-or-higher peers",
  );
  expect(legend).toBeVisible();
  expect(within(legend).getByRole("img", {
    name: "Mountain Magnitude half-step color bands from M0 up to M5 for U.S. tracts; M5 and above use the separate cap color",
  })).toBeVisible();
  for (const tick of ["0", "1", "2", "3", "4", "5"]) {
    expect(within(legend).getByText(tick)).toBeVisible();
  }
  expect(within(legend).getByText("Unavailable")).toBeVisible();
  expect(within(legend).getByText("M5+").querySelector("i"))
    .toHaveStyle({ backgroundColor: mountainColor(5, "tract")! });
  expect(legend.querySelector(".missing-key .hatched")).toHaveAttribute("aria-hidden", "true");
  fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
  fireEvent.change(screen.getByLabelText("Minimum Mountain Magnitude"), { target: { value: "2.3" } });
  fireEvent.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() => expect(window.location.hash).toContain("mountain_magnitude_min=2.3"));

  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St, Boulder, CO" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  const drawer = await screen.findByRole("dialog", { name: "Tract detail" });
  expect(await within(drawer).findByLabelText("Mountain Magnitude")).toHaveClass("active");
  expect(within(drawer).getByLabelText("Residential Hazard Exposure").querySelector(":scope > span"))
    .toHaveStyle({ color: scoreColor(tract.res_hazard_npctl)! });
  const wildfire = within(drawer).getAllByText("Wildfire")
    .find((element) => element.closest(".contribution"))!.closest(".contribution");
  expect(wildfire?.querySelector(".bar i"))
    .toHaveStyle({ backgroundColor: scoreColor(hazards[0].percentile)! });
  fireEvent.click(within(drawer).getByText("Mountain Magnitude breakdown"));
  expect(within(drawer).getByText(/property-specific views/)).toBeVisible();
  expect(within(drawer).getByText(/1,200 m/)).toBeVisible();
  expect(within(drawer).getByLabelText("Mountain Magnitude"))
    .toHaveTextContent("≈ top 0.48% of U.S. tracts by base exposure");
});

it("uses grain-specific magnitude legends without changing shared half-magnitude colors", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findByText("HouseHunter");

  selectLayer("mountain");
  const tractLegend = await screen.findByLabelText(
    "Stepped Mountain Magnitude color scale for U.S. tracts, higher means fewer equal-or-higher peers",
  );
  expect(within(tractLegend).getByRole("img", {
    name: "Mountain Magnitude half-step color bands from M0 up to M5 for U.S. tracts; M5 and above use the separate cap color",
  })).toBeVisible();
  expect(within(tractLegend).getByText("5")).toBeVisible();
  expect(within(tractLegend).getByText("M5+")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "Counties" }));
  const countyLegend = await screen.findByLabelText(
    "Stepped Mountain Magnitude color scale for U.S. counties, higher means fewer equal-or-higher peers",
  );
  expect(within(countyLegend).getByRole("img", {
    name: "Mountain Magnitude half-step color bands from M0 up to M4 for U.S. counties; M4 and above use the separate cap color",
  })).toBeVisible();
  expect(within(countyLegend).queryByText("5")).not.toBeInTheDocument();
  expect(within(countyLegend).getByText("M4+")).toBeVisible();
  expect(countyLegend).toHaveTextContent("not comparable across grains");
});

it("discards a stale Community Conditions page after returning to group summaries", async () => {
  const baseFetch = mockFetch();
  let resolveNext: ((response: Response) => void) | undefined;
  vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://127.0.0.1");
    if (url.pathname === "/api/v3/counties" && url.searchParams.get("limit") === "50") {
      if (url.searchParams.get("offset") === "50") {
        return new Promise<Response>((resolve) => { resolveNext = resolve; });
      }
      return Promise.resolve(response({ total: 51, items: [county] }));
    }
    return baseFetch(input);
  }));
  render(<App />);
  await screen.findByText("HouseHunter");
  selectLayer("community-conditions");
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
  expect((await within(drawer).findAllByText("Wildfire"))[0]).toBeVisible();
  expect(within(drawer).getByText("0.0 · Not applicable")).toBeVisible();
  expect(window.location.hash).not.toContain("Main");
  expect(window.location.hash).toContain("state=CO");
  expect(window.location.hash).toContain("county=08013");
});

it("navigates between tract and county detail workflows", async () => {
  vi.stubGlobal("fetch", mockFetch()); render(<App />); await screen.findByText("HouseHunter");
  selectLayer("home-costs");
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  fireEvent.change(screen.getByLabelText("Street address"), { target: { value: "1 Main St, Boulder, CO" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  const tractDrawer = await screen.findByRole("dialog", { name: "Tract detail" });
  fireEvent.click(await within(tractDrawer).findByRole("button", { name: "View Boulder county" }));
  const countyDrawer = await screen.findByRole("dialog", { name: "County detail" });
  expect((await within(countyDrawer).findAllByText("Wildfire"))[0]).toBeVisible();
  expect(within(countyDrawer).getByText("0.0 · Not applicable")).toBeVisible();
  expect(within(countyDrawer).getByLabelText("Home Costs")).toHaveTextContent("County market geography");
  fireEvent.click(await within(countyDrawer).findByRole("button", { name: "View 12 tracts" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Tracts" })).toHaveAttribute("aria-pressed", "true"));
  expect(screen.queryByRole("dialog", { name: "County detail" })).not.toBeInTheDocument();
  expect(window.location.hash).toContain("state=CO");
  expect(window.location.hash).toContain("county=08013");
});

it("keeps preparation inside the neutral map shell", async () => {
  vi.stubGlobal("fetch", mockFetch(false)); render(<App />);
  expect(await screen.findByRole("heading", { name: "Prepare the national hazard map" })).toBeVisible();
  expect(screen.getByRole("img", { name: /USA tract Residential Hazard Exposure map/i })).toBeVisible();
  expect(screen.getByRole("button", { name: "Prepare national data" })).toBeVisible();
});
