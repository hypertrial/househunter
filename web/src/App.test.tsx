import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { mapFocusTarget, mapTooltipClass, SCORE_BANDS, scoreBand, scoreLabel, scoreToneClass, sortedHazardPercentiles, STATE_ABBREVIATIONS } from "./App";
import RiskMap from "./RiskMap";
import { cameraFromTransform, readHash, relativeTransform, transformFromCamera } from "./map";
import type { HazardPercentile, PlaceSummary } from "./types";

const tract: PlaceSummary = {
  place_id: "08013012101", name: "08013012101", state: "CO", place_type: "tract",
  population_2020: 0, housing_units_2020: 0, risk_score: 21.25, coverage_status: "complete",
  fema_vintage: "December 2025", census_vintage: "n/a", county_fips: "08013", county_name: "Boulder",
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
    if (url.pathname === "/api/v1/map/scores") return response({ schema_version: 1, build_id: "fixture", level: url.searchParams.get("level"), scope: buildScope, rows: [{ place_id: url.searchParams.get("level") === "county" ? county.place_id : tract.place_id, risk_score: url.searchParams.get("level") === "county" ? county.risk_score : tract.risk_score, coverage_status: "complete" }] });
    if (url.pathname === "/api/v1/places" || url.pathname === "/api/v1/counties") return response({ total: 1, items: [url.pathname.includes("counties") ? county : tract] });
    if (url.pathname === `/api/v1/places/${tract.place_id}` || url.pathname === `/api/v1/counties/${county.place_id}`) return response({ summary: url.pathname.includes("counties") ? county : tract, total_weighted_housing: 0, coverage_ratio: 1, methodology_notice: "Published FEMA percentile; not property-level risk.", tract_contributions: [], hazard_percentiles: hazards, member_tract_count: url.pathname.includes("counties") ? 12 : null });
    if (url.pathname === "/api/v1/lookup") return response({ status: "resolved", query: "1 Main", matched_address: "1 MAIN", tract_id: tract.place_id, detail: { summary: tract, total_weighted_housing: 0, coverage_ratio: 1, methodology_notice: "Published FEMA percentile.", tract_contributions: [], hazard_percentiles: hazards, member_tract_count: null }, provider: "census", precision: "house", approximate: false, attribution: null });
    return response({});
  });
}

const canvasContexts = new Map<HTMLCanvasElement, CanvasRenderingContext2D>();

beforeEach(() => {
  canvasContexts.clear();
  window.history.replaceState(null, "", "/");
  class Observer { observe() { /* test stub */ } disconnect() { /* test stub */ } }
  vi.stubGlobal("ResizeObserver", Observer);
  vi.spyOn(HTMLCanvasElement.prototype, "getBoundingClientRect").mockReturnValue({ x: 0, y: 0, top: 0, left: 0, right: 1000, bottom: 700, width: 1000, height: 700, toJSON: () => ({}) });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(function (this: HTMLCanvasElement) {
    let context = canvasContexts.get(this);
    if (!context) {
      context = {
        setTransform: vi.fn(), clearRect: vi.fn(), fillRect: vi.fn(), drawImage: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), closePath: vi.fn(), arc: vi.fn(), fill: vi.fn(), stroke: vi.fn(), fillText: vi.fn(), createPattern: vi.fn(() => "pattern"), getImageData: vi.fn(() => ({ data: new Uint8ClampedArray([0, 0, 0, 0]) })),
      } as unknown as CanvasRenderingContext2D;
      canvasContexts.set(this, context);
    }
    return context;
  });
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("score semantics", () => {
  it("bins the displayed one-decimal percentile at every boundary", () => {
    expect([0, 19.94, 19.96, 39.94, 39.96, 59.96, 79.96, 100].map(scoreBand)).toEqual([
      "low", "low", "below", "below", "typical", "high", "highest", "highest",
    ]);
    expect(scoreBand(null)).toBeNull(); expect(scoreBand(101)).toBeNull(); expect(scoreToneClass(null)).toBe("missing");
    expect([0, 20, 40, 60, 80].map(scoreToneClass)).toEqual([
      "score-low", "score-below", "score-typical", "score-high", "score-highest",
    ]);
    expect(scoreLabel({ ...tract, risk_score: 19.96 })).toBe("20.0");
    expect(SCORE_BANDS.map((item) => item.label)).toEqual(["0–20", "20–40", "40–60", "60–80", "80–100"]);
  });
  it("sorts hazards high-to-low with unrated last", () => {
    const values: HazardPercentile[] = [{ code: "TSUN", label: "Tsunami", percentile: null }, { code: "AVLN", label: "Avalanche", percentile: 10 }, { code: "WFIR", label: "Wildfire", percentile: 80 }];
    expect(sortedHazardPercentiles(values).map((item) => item.code)).toEqual(["WFIR", "AVLN", "TSUN"]);
  });
  it("validates and clamps URL map state", () => {
    expect(readHash("#level=county&state=co&county=123&place=08013&cx=4&cy=-2&z=99")).toEqual({ level: "county", state: "", county: "", place: "08013", unranked: false, camera: { cx: 1, cy: 0, z: 12 } });
    expect(readHash("#level=tract&state=ZZ&county=08013&place=08013012101")).toMatchObject({ state: "", county: "", place: "08013012101" });
    expect(readHash("#level=tract&state=CO&county=01001&place=01001000100")).toMatchObject({ state: "CO", county: "", place: "" });
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
    manifestUrl="/map-assets/manifest.json" level="tract" rows={[]}
    selected="" state="" county="" showUnranked={false} focusTarget={null}
    initialCamera={{ cx: 0.5, cy: 0.5, z: 5 }} onSelect={() => undefined}
    onPreview={() => undefined} onCamera={onCamera} onStatus={() => undefined}
  />);
  const canvas = document.querySelector<HTMLCanvasElement>("canvas.risk-canvas")!;
  await waitFor(() => expect((canvas as HTMLCanvasElement & { __zoom?: { k: number } }).__zoom?.k).toBe(5));
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
  vi.mocked(HTMLCanvasElement.prototype.getBoundingClientRect).mockImplementation(() => ({
    x: 0, y: 0, top: 0, left: 0, right: width, bottom: 700,
    width, height: 700, toJSON: () => ({}),
  }));
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(() => width);
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(() => 700);
  vi.stubGlobal("fetch", mockFetch());
  const onCamera = vi.fn();
  render(<RiskMap
    manifestUrl="/map-assets/manifest.json" level="tract" rows={[]}
    selected="" state="" county="" showUnranked={false} focusTarget={null}
    initialCamera={{ cx: 0.37, cy: 0.61, z: 2 }} onSelect={() => undefined}
    onPreview={() => undefined} onCamera={onCamera} onStatus={() => undefined}
  />);
  const canvas = document.querySelector<HTMLCanvasElement>("canvas.risk-canvas")!;
  await waitFor(() => expect((canvas as HTMLCanvasElement & { __zoom?: { k: number } }).__zoom?.k).toBe(2));
  width = 500;
  act(() => resize());
  await waitFor(() => expect((canvas as HTMLCanvasElement & { __zoom?: { x: number, y: number } }).__zoom)
    .toMatchObject({ x: -120, y: -504 }));
  fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
  await waitFor(() => expect(onCamera).toHaveBeenLastCalledWith(expect.objectContaining({ cx: 0.37, cy: 0.61, z: 3 })));
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
  resolveCounty(response({ schema_version: 1, build_id: "fixture", level: "county", scope: { kind: "national", state: null }, rows: [{ place_id: county.place_id, risk_score: county.risk_score, coverage_status: "complete" }] }));
  await waitFor(() => expect(screen.getByTitle("fixture")).toHaveTextContent("county"));
  await act(async () => resolveTract(response({ schema_version: 1, build_id: "fixture", level: "tract", scope: { kind: "national", state: null }, rows: [{ place_id: tract.place_id, risk_score: tract.risk_score, coverage_status: "complete" }] })));
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
  resolveCounty(response({ schema_version: 1, build_id: "fixture", level: "county", scope: { kind: "national", state: null }, rows: [{ place_id: county.place_id, risk_score: county.risk_score, coverage_status: "complete" }] }));
  await waitFor(() => expect(screen.getByTitle("fixture")).toHaveTextContent("county"));
  await act(async () => rejectTract(new Error("obsolete tract request failed")));
  expect(screen.queryByText("Scores could not be loaded")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Counties" })).toHaveAttribute("aria-pressed", "true");
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

it("rasterizes settled vectors before atomically committing the active zoom frame", async () => {
  vi.stubGlobal("fetch", mockFetch());
  render(<App />);
  await screen.findByText("HouseHunter");
  await screen.findAllByText("1 tracts interactive");
  const zoomIn = screen.getByRole("button", { name: "Zoom in" });
  for (let index = 0; index < 4; index += 1) fireEvent.click(zoomIn);
  await waitFor(() => {
    const visibleCanvas = document.querySelector<HTMLCanvasElement>("canvas.risk-canvas");
    const visibleContext = visibleCanvas ? canvasContexts.get(visibleCanvas) : null;
    expect(visibleContext).toBeTruthy();
    const visibleDraws = vi.mocked(visibleContext!.drawImage).mock.calls;
    const committedIndex = [...visibleDraws.keys()].reverse().find((index) => {
      const sourceContext = canvasContexts.get(visibleDraws[index][0] as HTMLCanvasElement);
      return sourceContext && vi.mocked(sourceContext.setTransform).mock.calls.some(([scale]) => Number(scale) > 4);
    });
    if (committedIndex === undefined) throw new Error("The settled high-zoom buffer has not committed yet");
    const committedContext = canvasContexts.get(visibleDraws[committedIndex][0] as HTMLCanvasElement)!;
    const paintOrders = [
      ...vi.mocked(committedContext.fill).mock.invocationCallOrder,
      ...vi.mocked(committedContext.stroke).mock.invocationCallOrder,
      ...vi.mocked(committedContext.fillText).mock.invocationCallOrder,
    ];
    expect(paintOrders.length).toBeGreaterThan(0);
    expect(vi.mocked(visibleContext!.drawImage).mock.invocationCallOrder[committedIndex])
      .toBeGreaterThan(Math.max(...paintOrders));
  });
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
  expect(within(exports).getByRole("link", { name: "Tracts CSV" })).toHaveAttribute("href", "/api/v1/exports/places.csv");
  expect(within(exports).getByRole("link", { name: "Tracts Parquet" })).toHaveAttribute("href", "/api/v1/exports/places.parquet");
  expect(within(exports).getByRole("link", { name: "Counties CSV" })).toHaveAttribute("href", "/api/v1/exports/counties.csv");
  expect(within(exports).getByRole("link", { name: "Counties Parquet" })).toHaveAttribute("href", "/api/v1/exports/counties.parquet");
  expect(screen.getByLabelText("Score color scale, lower is better")).toHaveTextContent("not property-level risk");
  expect(screen.getByRole("img", { name: /focusable USA tract risk map/i })).toBeVisible();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByText("Next")).not.toBeInTheDocument();
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
  await screen.findAllByText("1 tracts interactive");
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
