import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App, { scoreLabel, sortedHazardPercentiles, STATE_ABBREVIATIONS } from "./App";
import type { HazardPercentile, PlaceSummary } from "./types";

const place = {
  risk_score: 42.04,
  coverage_status: "complete",
  county_fips: "01001",
  county_name: "Autauga",
} as PlaceSummary;

function placesParams(calls: unknown[][]): URLSearchParams[] {
  return calls
    .map(([url]) => String(url))
    .filter((url) => url.includes("/api/v1/places?"))
    .map((url) => new URL(url, "http://127.0.0.1").searchParams);
}

function countiesParams(calls: unknown[][]): URLSearchParams[] {
  return calls
    .map(([url]) => String(url))
    .filter((url) => url.includes("/api/v1/counties?"))
    .map((url) => new URL(url, "http://127.0.0.1").searchParams);
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("scoreLabel", () => {
  it("shows full-precision scores at one decimal place", () => {
    expect(scoreLabel(place)).toBe("42.0");
  });

  it("labels missing values without presenting them as zero", () => {
    expect(scoreLabel({ ...place, risk_score: null, coverage_status: "missing_fema" })).toBe(
      "Not ranked",
    );
  });
});

describe("sortedHazardPercentiles", () => {
  it("orders rated hazards high to low and keeps no rating last", () => {
    const hazards: HazardPercentile[] = [
      { code: "TSUN", label: "Tsunami", percentile: null },
      { code: "AVLN", label: "Avalanche", percentile: 10 },
      { code: "WFIR", label: "Wildfire", percentile: 80 },
      { code: "VLCN", label: "Volcanic Activity", percentile: null },
    ];
    expect(sortedHazardPercentiles(hazards).map((item) => item.code)).toEqual([
      "WFIR",
      "AVLN",
      "TSUN",
      "VLCN",
    ]);
  });
});

describe("ranking workflow", () => {
  it("exposes accessible filters and preserves score direction", async () => {
    const request = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.includes("/meta")
        ? {
            app_version: "1.0.0",
            mutation_token: "token",
            reference_assets_ready: true,
            reference_assets_error: null,
            build: {
              build_id: "fixture",
              place_count: 1,
              ranked_place_count: 1,
              source_vintages: { fema: "December 2025" },
              scope: { kind: "national", state: null },
            },
          }
        : url.includes("/api/v1/counties?")
          ? {
              items: [
                {
                  ...place,
                  place_id: "08013",
                  name: "Boulder",
                  state: "CO",
                  place_type: "county",
                  population_2020: 0,
                  housing_units_2020: 0,
                  county_fips: "08013",
                  county_name: "Boulder",
                },
              ],
              total: 1,
            }
          : {
              items: [
                {
                  ...place,
                  place_id: "01001000100",
                  name: "01001000100",
                  state: "AL",
                  place_type: "tract",
                  population_2020: 0,
                  housing_units_2020: 0,
                },
              ],
              total: 100,
            };
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    expect(await screen.findByRole("heading", { name: /lower risk/i })).toBeVisible();
    expect(screen.getByLabelText("Search")).toBeVisible();
    const stateFilter = screen.getByLabelText("State");
    expect(stateFilter.tagName).toBe("SELECT");
    expect(stateFilter).toHaveDisplayValue("All states");
    expect([...stateFilter.querySelectorAll("option")].map((option) => option.textContent)).toEqual([
      "All states",
      ...STATE_ABBREVIATIONS,
    ]);
    expect([...STATE_ABBREVIATIONS]).toEqual([...STATE_ABBREVIATIONS].slice().sort());
    expect(new Set(STATE_ABBREVIATIONS).size).toBe(56);
    const countyFilter = screen.getByLabelText("County");
    expect(countyFilter).toBeDisabled();
    expect(countyFilter).toHaveDisplayValue("All counties");
    expect(screen.getByRole("columnheader", { name: "County" })).toBeVisible();
    await waitFor(() => expect(placesParams(request.mock.calls).length).toBeGreaterThan(0));
    expect(placesParams(request.mock.calls).at(-1)?.has("state")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(placesParams(request.mock.calls).at(-1)?.get("offset")).toBe("50"));
    fireEvent.change(stateFilter, { target: { value: "CO" } });
    await waitFor(() => {
      const latest = placesParams(request.mock.calls).at(-1);
      expect(latest?.get("state")).toBe("CO");
      expect(latest?.get("offset")).toBe("0");
    });
    await waitFor(() => expect(countyFilter).toBeEnabled());
    await waitFor(() => expect(within(countyFilter).getByRole("option", { name: "Boulder" })).toBeVisible());
    fireEvent.change(countyFilter, { target: { value: "08013" } });
    await waitFor(() => expect(placesParams(request.mock.calls).at(-1)?.get("county")).toBe("08013"));
    fireEvent.change(stateFilter, { target: { value: "" } });
    await waitFor(() => expect(placesParams(request.mock.calls).at(-1)?.has("state")).toBe(false));
    expect(screen.getByLabelText("Include incomplete")).not.toBeChecked();
    const riskHeader = screen.getByRole("columnheader", { name: /risk score/i });
    expect(riskHeader).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(screen.getByRole("button", { name: /risk score/i }));
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => String(url).includes("direction=desc"))).toBe(true),
    );
    expect(riskHeader).toHaveAttribute("aria-sort", "descending");
    fireEvent.click(screen.getByRole("button", { name: "Counties" }));
    await waitFor(() => expect(countiesParams(request.mock.calls).some((params) => !params.has("sort") || params.get("sort") === "risk_score" || params.get("limit") === "50")).toBe(true));
    expect(screen.getByText(/ranked among counties/i)).toBeVisible();
    expect(screen.getByRole("button", { name: "Counties" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("link", { name: "Tracts CSV" })).toBeVisible();
    expect(screen.getByRole("link", { name: "Counties CSV" })).toBeVisible();
  });

  it("shows published hazard percentiles and opens member tracts from a county", async () => {
    const request = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const tractSummary = {
        ...place,
        place_id: "01001000100",
        name: "01001000100",
        state: "AL",
        place_type: "tract",
        population_2020: 0,
        housing_units_2020: 0,
      };
      const countySummary = {
        ...place,
        place_id: "01001",
        name: "Autauga",
        state: "AL",
        place_type: "county",
        population_2020: 0,
        housing_units_2020: 0,
        county_fips: "01001",
        county_name: "Autauga",
      };
      const hazards = [
        { code: "WFIR", label: "Wildfire", percentile: 80 },
        { code: "AVLN", label: "Avalanche", percentile: 10 },
        { code: "TSUN", label: "Tsunami", percentile: null },
      ];
      let body: unknown = { items: [tractSummary], total: 1 };
      if (url.includes("/meta")) {
        body = {
          app_version: "1.0.0",
          mutation_token: "token",
          reference_assets_ready: true,
          reference_assets_error: null,
          build: {
            build_id: "fixture",
            place_count: 1,
            ranked_place_count: 1,
            source_vintages: { fema: "December 2025" },
            scope: { kind: "national", state: null },
          },
        };
      } else if (url.includes("/api/v1/counties/01001")) {
        body = {
          summary: countySummary,
          total_weighted_housing: 0,
          coverage_ratio: 1,
          methodology_notice: "HouseHunter ranks FEMA counties by published county ALR_NPCTL.",
          tract_contributions: [],
          hazard_percentiles: hazards,
          member_tract_count: 3,
        };
      } else if (url.includes("/api/v1/counties?")) {
        body = { items: [countySummary], total: 1 };
      } else if (url.includes("/api/v1/places/01001000100")) {
        body = {
          summary: tractSummary,
          total_weighted_housing: 0,
          coverage_ratio: 1,
          methodology_notice: "HouseHunter ranks FEMA tracts by published ALR_NPCTL.",
          tract_contributions: [],
          hazard_percentiles: hazards,
          member_tract_count: null,
        };
      }
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    expect(await screen.findByRole("heading", { name: /lower risk/i })).toBeVisible();
    fireEvent.click(await screen.findByRole("row", { name: /01001000100/ }));
    expect(await screen.findByRole("heading", { name: "Published hazard percentiles" })).toBeVisible();
    expect(screen.getByText("Wildfire")).toBeVisible();
    expect(screen.getByText("80.0")).toBeVisible();
    expect(screen.getByText("No rating")).toBeVisible();
    const bars = screen.getAllByText(/Wildfire|Avalanche|Tsunami/).map((node) => node.textContent);
    expect(bars.indexOf("Wildfire")).toBeLessThan(bars.indexOf("Tsunami"));
    fireEvent.click(screen.getByRole("button", { name: "Close tract detail" }));
    fireEvent.click(screen.getByRole("button", { name: "Counties" }));
    fireEvent.click(await screen.findByRole("row", { name: /Autauga/ }));
    expect(await screen.findByRole("button", { name: "View 3 tracts" })).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Close county detail" }));
    fireEvent.click(screen.getByRole("button", { name: "Tracts" }));
    fireEvent.click(await screen.findByRole("row", { name: /01001000100/ }));
    fireEvent.click(await screen.findByRole("button", { name: "View Autauga county" }));
    expect(await screen.findByRole("heading", { name: "Autauga, AL" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "01001000100, AL" })).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "View 3 tracts" }));
    await waitFor(() => {
      const latest = placesParams(request.mock.calls).at(-1);
      expect(latest?.get("county")).toBe("01001");
      expect(latest?.get("state")).toBe("AL");
    });
    expect(screen.getByRole("button", { name: "Tracts" })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(screen.getByLabelText("County")).toHaveDisplayValue("Autauga"));
  });

  it("starts preparation with the per-launch token", async () => {
    const request = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input);
      const body = url.includes("/meta")
        ? { app_version: "1.0.0", mutation_token: "secret", reference_assets_ready: true, reference_assets_error: null, build: null }
        : { job_id: "job", state: "queued", progress: 0, message: "Queued", error: null };
      if (url.endsWith("/jobs")) {
        expect(new Headers(options?.headers).get("X-HouseHunter-Token")).toBe("secret");
      }
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /prepare national data/i }));
    expect(await screen.findByRole("button", { name: "Cancel" })).toBeVisible();
  });
});
