import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App, { scoreLabel } from "./App";
import type { PlaceSummary } from "./types";

const place = { risk_score: 42.04, coverage_status: "complete" } as PlaceSummary;

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
            total: 1,
          };
      return new Response(JSON.stringify(body), { status: 200 });
    });
    vi.stubGlobal("fetch", request);
    render(<App />);
    expect(await screen.findByRole("heading", { name: /lower risk/i })).toBeVisible();
    expect(screen.getByLabelText("Search")).toBeVisible();
    expect(screen.getByLabelText("Include incomplete")).not.toBeChecked();
    const riskHeader = screen.getByRole("columnheader", { name: /risk score/i });
    expect(riskHeader).toHaveAttribute("aria-sort", "ascending");
    fireEvent.click(screen.getByRole("button", { name: /risk score/i }));
    await waitFor(() =>
      expect(request.mock.calls.some(([url]) => String(url).includes("direction=desc"))).toBe(true),
    );
    expect(riskHeader).toHaveAttribute("aria-sort", "descending");
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
