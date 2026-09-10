import { expect, test } from "@playwright/test";

const summary = {
  place_id: "0807850",
  name: "Boulder",
  state: "CO",
  place_type: "city",
  population_2020: 108250,
  housing_units_2020: 48000,
  risk_score: 21.25,
  coverage_status: "complete",
  fema_vintage: "December 2025",
  census_vintage: "2020 Census",
};

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
                  census: "2020",
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
      await route.fulfill({ json: { total: 1, items: [summary] } });
    } else if (url.pathname === "/api/v1/places/0807850") {
      await route.fulfill({
        json: {
          summary,
          total_weighted_housing: 48000,
          coverage_ratio: 1,
          methodology_notice: "HouseHunter aggregation, not a property assessment.",
          tract_contributions: [
            {
              tract_id: "08013012101",
              housing_units: 48000,
              housing_weight: 1,
              fema_percentile: 21.25,
              weighted_contribution: 21.25,
            },
          ],
        },
      });
    } else if (url.pathname.endsWith("places.csv")) {
      await route.fulfill({
        body: "place_id,name\n0807850,Boulder\n",
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
  await page.getByRole("row", { name: /Boulder/ }).click();
  await expect(page.getByRole("heading", { name: "Boulder, CO" })).toBeVisible();
  await page.getByRole("button", { name: "Close place detail" }).first().click();
  const downloaded = page.waitForEvent("download");
  await page.getByRole("link", { name: "CSV" }).click();
  await expect(await downloaded).toBeTruthy();
});
