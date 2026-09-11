import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App, { scoreBand, scoreLabel, scorePillLabel, scoreToneClass, sortedHazardPercentiles, STATE_ABBREVIATIONS } from "./App";
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

describe("scoreBand", () => {
  it("bins inclusive 0 and 100 onto a five-step sequential scale", () => {
    expect(scoreBand(0)).toBe("low");
    expect(scoreBand(19.9)).toBe("low");
    expect(scoreBand(19.94)).toBe("low");
    expect(scoreBand(19.96)).toBe("below");
    expect(scoreBand(20)).toBe("below");
    expect(scoreBand(39.9)).toBe("below");
    expect(scoreBand(40)).toBe("typical");
    expect(scoreBand(42.04)).toBe("typical");
    expect(scoreBand(60)).toBe("high");
    expect(scoreBand(80)).toBe("highest");
    expect(scoreBand(100)).toBe("highest");
  });

  it("bins the displayed one-decimal value, not the raw float", () => {
    expect(scoreLabel({ ...place, risk_score: 19.96 })).toBe("20.0");
    expect(scoreBand(19.96)).toBe("below");
    expect(scoreToneClass(19.96)).toBe("score-below");
    expect(scorePillLabel({ ...place, risk_score: 19.96 })).toBe("20.0, below typical");
    expect(scoreBand(19.999)).toBe("below");
    expect(scoreBand(39.95)).toBe("typical");
    expect(scoreBand(59.95)).toBe("high");
    expect(scoreBand(79.95)).toBe("highest");
  });

  it("treats each displayed threshold as the start of the next bin, not the end of the previous", () => {
    expect(scoreBand(19.9)).toBe("low");
    expect(scoreBand(20)).not.toBe("low");
    expect(scoreBand(39.9)).toBe("below");
    expect(scoreBand(40)).not.toBe("below");
    expect(scoreBand(59.9)).toBe("typical");
    expect(scoreBand(60)).not.toBe("typical");
    expect(scoreBand(79.9)).toBe("high");
    expect(scoreBand(80)).not.toBe("high");
    expect(scoreBand(99.9)).toBe("highest");
    expect(scoreBand(100)).toBe("highest");
  });

  it("does not paint missing or out-of-range values as low", () => {
    expect(scoreBand(null)).toBeNull();
    expect(scoreBand(Number.NaN)).toBeNull();
    expect(scoreBand(-0.1)).toBeNull();
    expect(scoreBand(100.1)).toBeNull();
    expect(scoreToneClass(null)).toBe("missing");
    expect(scoreToneClass(42.04)).toBe("score-typical");
    expect(scorePillLabel({ ...place, risk_score: null })).toBeUndefined();
    expect(scorePillLabel(place)).toBe("42.0, typical");
  });

  it("maps non-finite and out-of-range values to missing, never score-low", () => {
    const invalid = [null, Number.NaN, Number.NEGATIVE_INFINITY, -1, -0.1, 100.1, 101, Number.POSITIVE_INFINITY];
    for (const value of invalid) {
      expect(scoreBand(value)).toBeNull();
      expect(scoreToneClass(value)).toBe("missing");
      expect(scoreToneClass(value)).not.toBe("score-low");
    }
  });

  it("maps each sequential bin to a matching score-* tone class", () => {
    expect(scoreToneClass(0)).toBe("score-low");
    expect(scoreToneClass(19.9)).toBe("score-low");
    expect(scoreToneClass(19.96)).toBe("score-below");
    expect(scoreToneClass(20)).toBe("score-below");
    expect(scoreToneClass(40)).toBe("score-typical");
    expect(scoreToneClass(60)).toBe("score-high");
    expect(scoreToneClass(80)).toBe("score-highest");
    expect(scoreToneClass(100)).toBe("score-highest");
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
    const scorePill = await screen.findByText("42.0");
    expect(scorePill).toHaveClass("pill", "score-typical");
    expect(scorePill).toHaveAccessibleName("42.0, typical");
    expect(screen.getByLabelText("Score color scale, lower is better")).toBeVisible();
    expect(screen.getByText("Lower is better")).toBeVisible();
    expect(screen.getByText("0–20")).toHaveClass("pill", "score-low");
    expect(screen.getByText("20–40")).toHaveClass("pill", "score-below");
    expect(screen.getByText("40–60")).toHaveClass("pill", "score-typical");
    expect(screen.getByText("60–80")).toHaveClass("pill", "score-high");
    expect(screen.getByText("80–100")).toHaveClass("pill", "score-highest");
    expect(screen.queryByText("0-20")).not.toBeInTheDocument();
    expect(screen.queryByText(/Very High|Relatively High|Relatively Moderate|Relatively Low|Very Low/i)).not.toBeInTheDocument();
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

  it("keeps unranked scores gray instead of the low band", async () => {
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
              ranked_place_count: 0,
              source_vintages: { fema: "December 2025" },
              scope: { kind: "national", state: null },
            },
          }
          : url.includes("/api/v1/places/01001000100")
            ? {
                summary: {
                  ...place,
                  place_id: "01001000100",
                  name: "01001000100",
                  state: "AL",
                  place_type: "tract",
                  population_2020: 0,
                  housing_units_2020: 0,
                  risk_score: null,
                  coverage_status: "missing_fema",
                },
                total_weighted_housing: 0,
                coverage_ratio: 0,
                methodology_notice: "HouseHunter ranks FEMA tracts by published ALR_NPCTL.",
                tract_contributions: [],
                hazard_percentiles: [{ code: "TSUN", label: "Tsunami", percentile: null }],
                member_tract_count: null,
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
                    risk_score: null,
                    coverage_status: "missing_fema",
                  },
                ],
                total: 1,
              };
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    const pill = await screen.findByText("Not ranked");
    expect(pill).toHaveClass("pill", "missing");
    expect(pill).not.toHaveClass("score-low");
    expect(pill).not.toHaveClass("score-below");
    expect(pill).not.toHaveClass("score-typical");
    expect(pill).not.toHaveClass("score-high");
    expect(pill).not.toHaveClass("score-highest");
    expect(screen.getByText("missing fema")).toBeVisible();
    fireEvent.click(await screen.findByRole("row", { name: /01001000100/ }));
    const dialog = await screen.findByRole("dialog");
    const hero = within(dialog).getByText("Not ranked").closest(".score");
    expect(hero).toHaveClass("score", "missing");
    expect(hero).not.toHaveClass("score-low");
    const missingBar = within(dialog).getByText("Tsunami").closest(".contribution")?.querySelector(".bar");
    expect(missingBar).toHaveClass("missing");
    expect(missingBar).not.toHaveClass("score-low");
    expect(missingBar?.querySelector("i")?.getAttribute("style")).toMatch(/width:\s*0%/);
  });

  it("uses the same sequential band on the table pill, detail hero, and hazard bar", async () => {
    const summary = {
      ...place,
      place_id: "01001000100",
      name: "01001000100",
      state: "AL",
      place_type: "tract",
      population_2020: 0,
      housing_units_2020: 0,
      risk_score: 60,
    };
    const hazards = [
      { code: "WFIR", label: "Wildfire", percentile: 60 },
      { code: "AVLN", label: "Avalanche", percentile: 0 },
      { code: "TSUN", label: "Tsunami", percentile: null },
    ];
    const request = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      let body: unknown = { items: [summary], total: 1 };
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
      } else if (url.includes("/api/v1/places/01001000100")) {
        body = {
          summary,
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
    const tablePill = await screen.findByText("60.0");
    expect(tablePill).toHaveClass("pill", "score-high");
    expect(tablePill).toHaveAccessibleName("60.0, high among peers");
    fireEvent.click(await screen.findByRole("row", { name: /01001000100/ }));
    const dialog = await screen.findByRole("dialog");
    const hero = dialog.querySelector(".score");
    expect(hero).toHaveClass("score", "score-high");
    expect(hero).toHaveTextContent("60.0");
    const sharedBar = screen.getByText("Wildfire").closest(".contribution")?.querySelector(".bar");
    expect(sharedBar).toHaveClass("bar", "score-high");
    expect(sharedBar).not.toHaveClass("score-low");
    expect(sharedBar?.querySelector("i")?.getAttribute("style")).toMatch(/width:\s*60%/);
    const zeroBar = screen.getByText("Avalanche").closest(".contribution")?.querySelector(".bar");
    expect(screen.getByText("0.0")).toBeVisible();
    expect(zeroBar).toHaveClass("bar", "score-low");
    expect(zeroBar).not.toHaveClass("missing");
    const nullBar = screen.getByText("Tsunami").closest(".contribution")?.querySelector(".bar");
    expect(screen.getByText("No rating")).toBeVisible();
    expect(nullBar).toHaveClass("bar", "missing");
    expect(nullBar).not.toHaveClass("score-low");
    expect(nullBar?.querySelector("i")?.getAttribute("style")).toMatch(/width:\s*0%/);
  });

  it("colors a displayed 20.0 as below even when the raw percentile is 19.96", async () => {
    const summary = {
      ...place,
      place_id: "01001000100",
      name: "01001000100",
      state: "AL",
      place_type: "tract",
      population_2020: 0,
      housing_units_2020: 0,
      risk_score: 19.96,
    };
    const request = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      let body: unknown = { items: [summary], total: 1 };
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
      } else if (url.includes("/api/v1/places/01001000100")) {
        body = {
          summary,
          total_weighted_housing: 0,
          coverage_ratio: 1,
          methodology_notice: "HouseHunter ranks FEMA tracts by published ALR_NPCTL.",
          tract_contributions: [],
          hazard_percentiles: [{ code: "WFIR", label: "Wildfire", percentile: 19.96 }],
          member_tract_count: null,
        };
      }
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    const tablePill = await screen.findByText("20.0");
    expect(tablePill).toHaveClass("pill", "score-below");
    expect(tablePill).not.toHaveClass("score-low");
    expect(tablePill).toHaveAccessibleName("20.0, below typical");
    fireEvent.click(await screen.findByRole("row", { name: /01001000100/ }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.querySelector(".score")).toHaveClass("score-below");
    expect(dialog.querySelector(".score")).toHaveTextContent("20.0");
    const bar = screen.getByText("Wildfire").closest(".contribution")?.querySelector(".bar");
    expect(bar).toHaveClass("bar", "score-below");
    expect(bar).not.toHaveClass("score-low");
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
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("42.0").closest(".score")).toHaveClass("score-typical");
    expect(screen.getByText("Wildfire")).toBeVisible();
    expect(screen.getByText("80.0")).toBeVisible();
    expect(screen.getByText("No rating")).toBeVisible();
    expect(screen.getByText("Wildfire").closest(".contribution")?.querySelector(".bar")).toHaveClass("score-highest");
    expect(screen.getByText("Avalanche").closest(".contribution")?.querySelector(".bar")).toHaveClass("score-low");
    const missingBar = screen.getByText("Tsunami").closest(".contribution")?.querySelector(".bar");
    expect(missingBar).toHaveClass("missing");
    expect(missingBar).not.toHaveClass("score-low");
    expect(missingBar).not.toHaveClass("score-below");
    expect(missingBar).not.toHaveClass("score-typical");
    expect(missingBar).not.toHaveClass("score-high");
    expect(missingBar).not.toHaveClass("score-highest");
    expect(missingBar?.querySelector("i")?.getAttribute("style")).toMatch(/width:\s*0%/);
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

  it("maps an address to tract detail without calling census.gov from the browser", async () => {
    const request = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
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
      } else if (url.includes("/api/v1/lookup")) {
        expect(options?.method).toBe("POST");
        expect(url.startsWith("http") ? new URL(url).host : "127.0.0.1").not.toContain("census.gov");
        expect(url).toContain("/api/v1/lookup");
        body = {
          query: "1 Main St, Autauga, AL",
          matched_address: "1 MAIN ST, AUTAUGA, AL, 36003",
          tract_id: "01001000100",
          detail: {
            summary: tractSummary,
            total_weighted_housing: 0,
            coverage_ratio: 1,
            methodology_notice: "HouseHunter ranks FEMA tracts by published ALR_NPCTL.",
            tract_contributions: [],
            hazard_percentiles: [],
            member_tract_count: null,
          },
        };
      } else if (url.includes("/api/v1/counties?")) {
        body = {
          items: [
            {
              ...place,
              place_id: "01001",
              name: "Autauga",
              state: "AL",
              place_type: "county",
              population_2020: 0,
              housing_units_2020: 0,
              county_fips: "01001",
              county_name: "Autauga",
            },
          ],
          total: 1,
        };
      } else if (url.includes("/api/v1/places/01001000100")) {
        body = {
          summary: tractSummary,
          total_weighted_housing: 0,
          coverage_ratio: 1,
          methodology_notice: "HouseHunter ranks FEMA tracts by published ALR_NPCTL.",
          tract_contributions: [],
          hazard_percentiles: [],
          member_tract_count: null,
        };
      }
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    expect(await screen.findByRole("heading", { name: /lower risk/i })).toBeVisible();
    expect(screen.getByLabelText("Address")).toHaveAttribute("maxLength", "200");
    fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
    expect(request.mock.calls.some(([url]) => String(url).includes("/api/v1/lookup"))).toBe(false);
    fireEvent.change(screen.getByLabelText("Address"), { target: { value: "1 Main St, Autauga, AL" } });
    fireEvent.click(screen.getByRole("button", { name: "Find tract" }));
    expect(await screen.findByRole("heading", { name: "01001000100, AL" })).toBeVisible();
    expect(request.mock.calls.some(([url]) => String(url).includes("census.gov"))).toBe(false);
    expect(request.mock.calls.some(([url]) => String(url).includes("/api/v1/lookup"))).toBe(true);
    await waitFor(() => expect(screen.getByLabelText("State")).toHaveDisplayValue("AL"));
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
