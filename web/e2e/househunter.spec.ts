import { expect, test } from "@playwright/test";

const summary = {
  place_id: "08013012101",
  name: "08013012101",
  state: "CO",
  place_type: "tract",
  population_2020: 0,
  housing_units_2020: 0,
  risk_score: 21.25,
  coverage_status: "complete",
  fema_vintage: "December 2025",
  census_vintage: "n/a",
  county_fips: "08013",
  county_name: "Boulder",
};

const countySummary = {
  place_id: "08013",
  name: "Boulder",
  state: "CO",
  place_type: "county",
  population_2020: 0,
  housing_units_2020: 0,
  risk_score: 18.5,
  coverage_status: "complete",
  fema_vintage: "December 2025",
  census_vintage: "n/a",
  county_fips: "08013",
  county_name: "Boulder",
};

const hazards = [
  { code: "WFIR", label: "Wildfire", percentile: 80.5 },
  { code: "AVLN", label: "Avalanche", percentile: 12 },
  { code: "TSUN", label: "Tsunami", percentile: null },
];

test("prepares, ranks, inspects, and exports", async ({ page }) => {
  let prepared = false;
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/meta") {
      await route.fulfill({
        json: {
          app_version: "1.0.0",
          mutation_token: "test-token",
          reference_assets_ready: true,
          reference_assets_error: null,
          build: prepared
            ? {
                build_id: "national-fixture",
                place_count: 1,
                ranked_place_count: 1,
                source_vintages: {
                  fema: "December 2025",
                },
                scope: { kind: "national", state: null },
              }
            : null,
        },
      });
    } else if (url.pathname === "/api/v1/jobs" && route.request().method() === "POST") {
      expect(route.request().headers()["x-househunter-token"]).toBe("test-token");
      await route.fulfill({
        status: 202,
        json: {
          job_id: "one",
          state: "running",
          progress: 30,
          message: "Building",
          error: null,
        },
      });
    } else if (url.pathname === "/api/v1/jobs/one") {
      prepared = true;
      await route.fulfill({
        json: {
          job_id: "one",
          state: "succeeded",
          progress: 100,
          message: "Complete",
          error: null,
        },
      });
    } else if (url.pathname === "/api/v1/places") {
      if (url.searchParams.has("state")) {
        expect(url.searchParams.get("state")).toBe("CO");
        expect(url.searchParams.get("offset")).toBe("0");
      }
      if (url.searchParams.has("county")) {
        expect(url.searchParams.get("county")).toBe("08013");
      }
      await route.fulfill({ json: { total: 1, items: [summary] } });
    } else if (url.pathname === "/api/v1/counties") {
      await route.fulfill({ json: { total: 1, items: [countySummary] } });
    } else if (url.pathname === "/api/v1/counties/08013") {
      await route.fulfill({
        json: {
          summary: countySummary,
          total_weighted_housing: 0,
          coverage_ratio: 1,
          methodology_notice: "HouseHunter ranks FEMA counties by published county ALR_NPCTL.",
          tract_contributions: [],
          hazard_percentiles: hazards,
          member_tract_count: 12,
        },
      });
    } else if (url.pathname === "/api/v1/places/08013012101") {
      await route.fulfill({
        json: {
          summary,
          total_weighted_housing: 0,
          coverage_ratio: 1,
          methodology_notice: "HouseHunter ranks FEMA tracts by published ALR_NPCTL.",
          tract_contributions: [],
          hazard_percentiles: hazards,
          member_tract_count: null,
        },
      });
    } else if (url.pathname === "/api/v1/lookup") {
      expect(route.request().method()).toBe("POST");
      await route.fulfill({
        json: {
          query: "1 Main St, Boulder, CO",
          matched_address: "1 MAIN ST, BOULDER, CO, 80302",
          tract_id: "08013012101",
          detail: {
            summary,
            total_weighted_housing: 0,
            coverage_ratio: 1,
            methodology_notice: "HouseHunter ranks FEMA tracts by published ALR_NPCTL.",
            tract_contributions: [],
            hazard_percentiles: hazards,
            member_tract_count: null,
          },
        },
      });
    } else if (url.pathname.endsWith("places.csv") || url.pathname.endsWith("counties.csv")) {
      await route.fulfill({
        body: "place_id,name\n08013012101,08013012101\n",
        headers: {
          "Content-Type": "text/csv",
          "Content-Disposition": "attachment; filename=places.csv",
        },
      });
    } else {
      await route.abort();
    }
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Prepare national data" }).click();
  await expect(page.getByRole("heading", { name: "Lower risk, plainly ranked." })).toBeVisible();
  await expect(page.getByLabel("Score color scale, lower is better")).toBeVisible();
  await expect(page.getByText("0–20")).toBeVisible();
  await expect(page.getByText("20–40")).toBeVisible();
  await expect(page.getByText("40–60")).toBeVisible();
  await expect(page.getByText("60–80")).toBeVisible();
  await expect(page.getByText("80–100")).toBeVisible();
  await expect(page.getByText("Very High")).toHaveCount(0);
  await expect(page.getByLabel("State")).toHaveValue("");
  await expect(page.getByLabel("County")).toBeDisabled();
  await page.getByLabel("State").selectOption("CO");
  await expect(page.getByLabel("State")).toHaveValue("CO");
  await expect(page.getByLabel("County")).toBeEnabled();
  await page.getByLabel("County").selectOption("08013");
  await page.getByRole("row", { name: /08013012101/ }).click();
  await expect(page.getByRole("heading", { name: "08013012101, CO" })).toBeVisible();
  await expect(page.getByText("21.3").first()).toHaveClass(/score-below/);
  await expect(page.getByRole("heading", { name: "Published hazard percentiles" })).toBeVisible();
  await expect(page.getByText("Wildfire")).toBeVisible();
  await expect(page.getByText("No rating")).toBeVisible();
  await expect(page.locator(".bar.score-highest")).toBeVisible();
  await expect(page.locator(".bar.score-low")).toBeVisible();
  await expect(page.locator(".bar.missing")).toBeVisible();
  await expect(page.locator(".bar.missing")).not.toHaveClass(/score-low/);
  await expect(page.locator(".bar.missing i")).toHaveAttribute("style", /width:\s*0%/);
  await page.getByRole("button", { name: "Close tract detail" }).first().click();
  await page.getByRole("button", { name: "Counties" }).click();
  await expect(page.getByText(/ranked among counties/i)).toBeVisible();
  await page.getByRole("row", { name: /Boulder/ }).click();
  await expect(page.getByRole("heading", { name: "Boulder, CO" })).toBeVisible();
  await expect(page.getByText("18.5").first()).toHaveClass(/score-low/);
  await expect(page.getByText("Wildfire")).toBeVisible();
  await page.getByRole("button", { name: "View 12 tracts" }).click();
  await expect(page.getByRole("button", { name: "Tracts" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByLabel("County")).toHaveValue("08013");
  await page.getByLabel("Address").fill("1 Main St, Boulder, CO");
  await page.getByRole("button", { name: "Find tract" }).click();
  await expect(page.getByRole("heading", { name: "08013012101, CO" })).toBeVisible();
  await page.getByRole("button", { name: "Close tract detail" }).first().click();
  const downloaded = page.waitForEvent("download");
  await page.getByRole("link", { name: "Tracts CSV" }).click();
  await expect(await downloaded).toBeTruthy();
});
