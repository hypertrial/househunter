import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { mapFocusTarget, SCORE_BANDS, scoreBand, scoreLabel, scoreToneClass, sortedHazardPercentiles } from "./App";
import { readHash } from "./map";
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
    if (url.pathname.includes("tracts.hash")) return response(topology(tract.place_id));
    if (url.pathname.includes("counties.hash")) return response(topology(county.place_id));
    if (url.pathname === "/api/v1/map/scores") return response({ schema_version: 1, build_id: "fixture", level: url.searchParams.get("level"), scope: buildScope, rows: [{ place_id: url.searchParams.get("level") === "county" ? county.place_id : tract.place_id, risk_score: url.searchParams.get("level") === "county" ? county.risk_score : tract.risk_score, coverage_status: "complete" }] });
    if (url.pathname === "/api/v1/places" || url.pathname === "/api/v1/counties") return response({ total: 1, items: [url.pathname.includes("counties") ? county : tract] });
    if (url.pathname === `/api/v1/places/${tract.place_id}` || url.pathname === `/api/v1/counties/${county.place_id}`) return response({ summary: url.pathname.includes("counties") ? county : tract, total_weighted_housing: 0, coverage_ratio: 1, methodology_notice: "Published FEMA percentile; not property-level risk.", tract_contributions: [], hazard_percentiles: hazards, member_tract_count: url.pathname.includes("counties") ? 12 : null });
    if (url.pathname === "/api/v1/lookup") return response({ status: "resolved", query: "1 Main", matched_address: "1 MAIN", tract_id: tract.place_id, detail: { summary: tract, total_weighted_housing: 0, coverage_ratio: 1, methodology_notice: "Published FEMA percentile.", tract_contributions: [], hazard_percentiles: hazards, member_tract_count: null }, provider: "census", precision: "house", approximate: false, attribution: null });
    return response({});
  });
}

beforeEach(() => {
  window.history.replaceState(null, "", "/");
  class Observer { observe() { /* test stub */ } disconnect() { /* test stub */ } }
  vi.stubGlobal("ResizeObserver", Observer);
  vi.spyOn(HTMLCanvasElement.prototype, "getBoundingClientRect").mockReturnValue({ x: 0, y: 0, top: 0, left: 0, right: 1000, bottom: 700, width: 1000, height: 700, toJSON: () => ({}) });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
    setTransform: vi.fn(), clearRect: vi.fn(), fillRect: vi.fn(), drawImage: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), closePath: vi.fn(), arc: vi.fn(), fill: vi.fn(), stroke: vi.fn(), fillText: vi.fn(), createPattern: vi.fn(() => "pattern"), getImageData: vi.fn(() => ({ data: new Uint8ClampedArray([0, 0, 0, 0]) })),
  } as unknown as CanvasRenderingContext2D);
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("score semantics", () => {
  it("bins the displayed one-decimal percentile at every boundary", () => {
    expect([0, 19.94, 19.96, 39.94, 39.96, 59.96, 79.96, 100].map(scoreBand)).toEqual([
      "low", "low", "below", "below", "typical", "high", "highest", "highest",
    ]);
    expect(scoreBand(null)).toBeNull(); expect(scoreBand(101)).toBeNull(); expect(scoreToneClass(null)).toBe("missing");
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
});

it("renders the map as the only primary UI with all retained controls", async () => {
  vi.stubGlobal("fetch", mockFetch()); render(<App />);
  expect(await screen.findByText("HouseHunter")).toBeVisible();
  expect(screen.getByRole("button", { name: "Tracts" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "Counties" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Lowest / Highest" })).toBeVisible();
  expect(screen.getByRole("button", { name: /Filters/ })).toBeVisible();
  expect(screen.getByLabelText("Score color scale, lower is better")).toHaveTextContent("not property-level risk");
  expect(screen.getByRole("img", { name: /focusable USA tract risk map/i })).toBeVisible();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByText("Next")).not.toBeInTheDocument();
});

it("applies state and county filters and changes geography levels", async () => {
  const request = mockFetch(); vi.stubGlobal("fetch", request); render(<App />); await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: /^Filters/ }));
  const panel = screen.getByRole("region", { name: "Map filters" });
  fireEvent.change(within(panel).getByLabelText("State"), { target: { value: "CO" } });
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

it("keeps place and address searches explicit and opens tract detail", async () => {
  vi.stubGlobal("fetch", mockFetch()); render(<App />); await screen.findByText("HouseHunter");
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  expect(screen.getByRole("tab", { name: "Place / FIPS" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("tab", { name: "Address" }));
  expect(screen.getByText(/Census geocoder through this loopback server/i)).toBeVisible();
  fireEvent.change(screen.getByLabelText("House address"), { target: { value: "1 Main St, Boulder, CO" } });
  fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
  const drawer = await screen.findByRole("dialog", { name: "Tract detail" });
  expect(await within(drawer).findByText("Wildfire")).toBeVisible();
  expect(within(drawer).getByText("No rating")).toBeVisible();
  expect(window.location.hash).not.toContain("Main");
  expect(window.location.hash).toContain("state=CO");
  expect(window.location.hash).toContain("county=08013");
});

it("keeps preparation inside the neutral map shell", async () => {
  vi.stubGlobal("fetch", mockFetch(false)); render(<App />);
  expect(await screen.findByRole("heading", { name: "Prepare the national risk map" })).toBeVisible();
  expect(screen.getByRole("img", { name: /USA tract risk map/i })).toBeVisible();
  expect(screen.getByRole("button", { name: "Prepare national data" })).toBeVisible();
});
