import { expect, test, type Page, type Route } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const build = {
  build_id: "national-fixture", place_count: 1, ranked_place_count: 1,
  county_count: 1, ranked_county_count: 1,
  source_vintages: { fema: "December 2025", fema_counties: "December 2025" },
  scope: { kind: "national", state: null },
};

const summary = {
  place_id: "08013012101", name: "Census tract 121.01", state: "CO", place_type: "tract",
  population_2020: 0, housing_units_2020: 0, risk_score: 21.25, coverage_status: "complete",
  fema_vintage: "December 2025", census_vintage: "n/a", county_fips: "08013", county_name: "Boulder",
};

const countySummary = {
  ...summary, place_id: "08013", name: "Boulder", place_type: "county", risk_score: 18.5,
};

const hazards = [
  { code: "WFIR", label: "Wildfire", percentile: 80.5 },
  { code: "AVLN", label: "Avalanche", percentile: 12 },
  { code: "TSUN", label: "Tsunami", percentile: null },
];

const detail = (item = summary) => ({
  summary: item, total_weighted_housing: 0, coverage_ratio: 1,
  methodology_notice: "Published FEMA ALR_NPCTL; not property-level risk.",
  tract_contributions: [], hazard_percentiles: hazards,
  member_tract_count: item.place_type === "county" ? 12 : null,
});

const polygon = {
  type: "Topology",
  objects: { geography: { type: "GeometryCollection", geometries: [{
    type: "Polygon", id: "08013012101", properties: {
      place_id: "08013012101", state: "CO", county_fips: "08013", name: "Census tract 121.01",
    }, arcs: [[0]],
  }] } },
  arcs: [[[-109, 41], [-102, 41], [-102, 37], [-109, 37], [-109, 41]]],
};

const detailPolygon = {
  ...polygon,
  arcs: [[
    [-109, 41], [-102, 41], [-102, 39.3], [-103.5, 39.3], [-103.5, 38.7],
    [-102, 38.7], [-102, 37], [-109, 37], [-109, 41],
  ]],
};

const countyPolygon = {
  ...polygon,
  objects: { geography: { type: "GeometryCollection", geometries: [{
    type: "Polygon", id: "08013", properties: {
      place_id: "08013", state: "CO", county_fips: "08013", name: "Boulder",
    }, arcs: [[0]],
  }] } },
};

const states = {
  type: "Topology",
  objects: { geography: { type: "GeometryCollection", geometries: [{
    type: "LineString", id: "CO", properties: { state: "CO", name: "Colorado", label: [-105.5, 39] }, arcs: [0],
  }] } },
  arcs: [[[-109, 41], [-102, 41], [-102, 37], [-109, 37], [-109, 41]]],
};

const manifest = {
  schema_version: 1, release: "v1.20", sources: {}, initial_compressed_size: 100,
  files: [
    { key: "states-national", filename: "states.topojson.gz", level: "state", lod: "national", jurisdiction: null, feature_count: 1, bounds: [], compressed_size: 1, sha256: "a" },
    { key: "tracts-national", filename: "tracts.topojson.gz", level: "tract", lod: "national", jurisdiction: null, feature_count: 1, bounds: [], compressed_size: 1, sha256: "b" },
    { key: "counties-national", filename: "counties.topojson.gz", level: "county", lod: "national", jurisdiction: null, feature_count: 1, bounds: [], compressed_size: 1, sha256: "c" },
    { key: "tracts-co", filename: "tracts-co.topojson.gz", level: "tract", lod: "detail", jurisdiction: "CO", feature_count: 1, bounds: [], compressed_size: 1, sha256: "d" },
  ],
};

async function installRoutes(page: Page, initiallyPrepared = true, failDetailOnce = false, detailDelayMs = 0) {
  let prepared = initiallyPrepared;
  let detailFailed = false;
  await page.route("**/map-assets/**", async (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop();
    if (name === "manifest.json") await route.fulfill({ json: manifest });
    else if (name === "states.topojson.gz") await route.fulfill({ json: states });
    else if (name === "counties.topojson.gz") await route.fulfill({ json: countyPolygon });
    else if (name === "tracts-co.topojson.gz" && failDetailOnce && !detailFailed) {
      detailFailed = true;
      await route.fulfill({ status: 503, json: { detail: "corrupt detail asset" } });
    } else {
      if (name === "tracts-co.topojson.gz" && detailDelayMs) {
        await new Promise((resolve) => setTimeout(resolve, detailDelayMs));
      }
      await route.fulfill({ json: name === "tracts-co.topojson.gz" ? detailPolygon : polygon });
    }
  });
  await page.route("**/api/v1/**", async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/meta") {
      await route.fulfill({ json: {
        app_version: "1.0.0", mutation_token: "test-token", reference_assets_ready: true,
        reference_assets_error: null, build: prepared ? build : null,
        map_assets: { ready: true, error: null, schema_version: 1, release: "v1.20", manifest_url: "/map-assets/manifest.json" },
      } });
    } else if (url.pathname === "/api/v1/jobs" && route.request().method() === "POST") {
      expect(route.request().headers()["x-househunter-token"]).toBe("test-token");
      await route.fulfill({ status: 202, json: { job_id: "one", state: "running", progress: 30, message: "Building", error: null } });
    } else if (url.pathname === "/api/v1/jobs/one") {
      prepared = true;
      await route.fulfill({ json: { job_id: "one", state: "succeeded", progress: 100, message: "Complete", error: null } });
    } else if (url.pathname === "/api/v1/map/scores") {
      const county = url.searchParams.get("level") === "county";
      await route.fulfill({ json: { schema_version: 1, build_id: build.build_id, level: county ? "county" : "tract", scope: build.scope, rows: [{ place_id: county ? "08013" : "08013012101", risk_score: county ? 18.5 : 21.25, coverage_status: "complete" }] } });
    } else if (url.pathname === "/api/v1/places") {
      await route.fulfill({ json: { total: 1, items: [summary] } });
    } else if (url.pathname === "/api/v1/counties") {
      await route.fulfill({ json: { total: 1, items: [countySummary] } });
    } else if (url.pathname === "/api/v1/places/08013012101") {
      await route.fulfill({ json: detail() });
    } else if (url.pathname === "/api/v1/counties/08013") {
      await route.fulfill({ json: detail(countySummary) });
    } else if (url.pathname === "/api/v1/lookup") {
      const posted = route.request().postDataJSON() as { candidate_id?: string };
      if (posted.candidate_id) await route.fulfill({ json: {
        status: "resolved", query: "1 Main St", matched_address: "Main Street, Boulder, CO", tract_id: summary.place_id,
        provider: "nominatim", precision: "street", approximate: true, attribution: "© OpenStreetMap contributors", detail: detail(),
      } });
      else await route.fulfill({ json: {
        status: "confirmation_required", query: "1 Main St", message: "Census had no house-level match. Confirm this approximate street match.",
        attribution: "© OpenStreetMap contributors", candidates: [{ candidate_id: "candidate-1", matched_address: "Main Street, Boulder, CO", precision: "street" }],
      } });
    } else if (url.pathname.includes("/exports/")) {
      await route.fulfill({ body: "place_id,name\n08013012101,Census tract 121.01\n", headers: { "Content-Type": "text/csv", "Content-Disposition": "attachment; filename=export.csv" } });
    } else await route.abort();
  });
}

test("keeps preparation and retained workflows inside the map shell", async ({ page }) => {
  await installRoutes(page, false);
  await page.goto("/");
  await expect(page.locator("canvas")).toBeVisible();
  await page.getByRole("button", { name: "Prepare national data" }).click();
  await expect(page.getByRole("button", { name: "Tracts" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("table")).toHaveCount(0);
  await page.getByRole("button", { name: /Filters/ }).click();
  await page.getByLabel("State").selectOption("CO");
  await expect(page.getByLabel("County")).toBeEnabled();
  await page.getByLabel("County").selectOption("08013");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page).toHaveURL(/state=CO/);
  await expect(page).toHaveURL(/county=08013/);
  await page.getByRole("button", { name: "Counties" }).click();
  await expect(page.getByRole("button", { name: "Counties" })).toHaveAttribute("aria-pressed", "true");
  await page.goBack();
  await expect(page.getByRole("button", { name: "Tracts" })).toHaveAttribute("aria-pressed", "true");
  await expect(page).toHaveURL(/county=08013/);
  await page.getByRole("button", { name: "Lowest / Highest" }).click();
  await expect(page.getByRole("heading", { name: "Lowest" })).toBeVisible();
  await page.getByRole("button", { name: /Census tract 121.01/ }).first().click();
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toContainText("Wildfire");
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toContainText("No rating");
});

test("uses explicit address confirmation and never calls a geocoder from the browser", async ({ page }) => {
  await installRoutes(page);
  const external: string[] = [];
  page.on("request", (request) => {
    if (/census\.gov|openstreetmap|nominatim/i.test(request.url())) external.push(request.url());
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByText(/through this loopback server/i)).toBeVisible();
  await page.getByLabel("Street address").fill("1 Main St, Boulder, CO");
  await page.getByRole("button", { name: "Find tract" }).click();
  await expect(page.getByRole("region", { name: "Approximate street match" })).toBeVisible();
  await page.getByRole("button", { name: /Use approximate street location/ }).click();
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toBeVisible();
  expect(external).toEqual([]);
  expect(await page.locator("[class*=marker]").count()).toBe(0);
});

test("is keyboard operable and never overflows the viewport", async ({ page }, testInfo) => {
  await installRoutes(page, true, true);
  const regionalRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("tracts-co.topojson.gz")) regionalRequests.push(request.url());
  });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/#level=tract&cx=0.5&cy=0.5&z=5");
  const canvas = page.locator("canvas");
  await expect(page.getByRole("alert")).toContainText("Detailed tract request failed");
  await page.getByRole("button", { name: "Retry map" }).click();
  await expect(page.locator(".build-pill")).toContainText("interactive");
  await expect.poll(() => regionalRequests.length).toBeGreaterThan(1);
  await page.getByRole("button", { name: "Reset map" }).click();
  await expect(page).toHaveURL(/z=1\.000/);
  await expect(page.locator(".build-pill")).toContainText("interactive");
  const bounds = await canvas.boundingBox();
  if (!bounds) throw new Error("Map canvas has no bounds");
  await canvas.focus();
  const isPhone = testInfo.project.name.includes("phone");
  if (!isPhone) {
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    await expect(page.locator(".map-tooltip")).toBeVisible();
  }
  const position = { x: bounds.width / 2, y: bounds.height / 2 };
  if (testInfo.project.name.includes("phone") || testInfo.project.name.includes("tablet")) await canvas.tap({ position });
  else await canvas.click({ position });
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toBeVisible();
  await page.getByRole("button", { name: "Close tract detail" }).click();
  await expect(canvas).toBeFocused();
  await page.getByRole("button", { name: "Reset map" }).click();
  await expect(page.locator(".build-pill")).toContainText("interactive");
  await canvas.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toBeVisible();
  await page.getByRole("button", { name: "Close tract detail" }).click();
  await expect(canvas).toBeFocused();
  await page.getByRole("button", { name: "Reset map" }).click();
  await expect(page.locator(".build-pill")).toContainText("interactive");
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width / 2 + 24, bounds.y + bounds.height / 2);
  await page.mouse.up();
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toHaveCount(0);
  await canvas.focus();
  await page.keyboard.press("ArrowRight");
  for (let index = 0; index < 4; index += 1) await page.keyboard.press("+");
  await expect.poll(() => Number(new URL(page.url()).hash.match(/(?:^|&)z=([\d.]+)/)?.[1] ?? 0)).toBeGreaterThan(5);
  await expect.poll(() => regionalRequests.length).toBeGreaterThan(0);
  await page.getByRole("button", { name: "Search" }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("Street address")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Search" })).toBeFocused();
  if (testInfo.project.name.includes("desktop") || testInfo.project.name.includes("wide")) {
    await page.setViewportSize({ width: Math.floor(bounds.width / 2), height: Math.floor(bounds.height / 2) });
  }
  const dimensions = await page.evaluate(() => ({ page: document.documentElement.scrollWidth, viewport: innerWidth }));
  expect(dimensions.page).toBe(dimensions.viewport);
  await expect(page.getByLabel(/Focusable USA tract risk map/)).toBeVisible();
  await expect(page.getByLabel("Score color scale, lower is better")).toContainText("not property-level risk");
  const accessibility = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(accessibility.violations).toEqual([]);
});

test("deduplicates regional geometry while repeated zooms settle", async ({ page }) => {
  const regionalRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("tracts-co.topojson.gz")) regionalRequests.push(request.url());
  });
  await installRoutes(page, true, false, 1_000);
  const detailLoaded = page.waitForResponse((response) => response.url().includes("tracts-co.topojson.gz"));
  await page.goto("/#level=tract&cx=0.5&cy=0.5&z=1");
  await expect(page.locator(".build-pill")).toContainText("interactive");
  const canvas = page.locator("canvas");
  const nationalFrame = await canvas.evaluate((element) => (element as HTMLCanvasElement).toDataURL());
  await canvas.focus();
  await page.keyboard.press("+");
  await page.keyboard.press("+");
  await page.keyboard.press("+");
  expect(regionalRequests).toHaveLength(0);
  await page.keyboard.press("+");
  await expect.poll(() => regionalRequests.length).toBe(1);
  const loadingFrame = await canvas.evaluate((element) => (element as HTMLCanvasElement).toDataURL());
  const loadingColors = await canvas.evaluate((element) => {
    const target = element as HTMLCanvasElement;
    const context = target.getContext("2d")!;
    const colors = new Set<string>();
    for (let y = 0; y < target.height; y += 24) {
      for (let x = 0; x < target.width; x += 24) {
        colors.add([...context.getImageData(x, y, 1, 1).data].join(","));
      }
    }
    return colors.size;
  });
  expect(loadingColors).toBeGreaterThan(2);
  await page.waitForTimeout(250);
  expect(await canvas.evaluate((element) => (element as HTMLCanvasElement).toDataURL())).toBe(loadingFrame);
  await page.keyboard.press("+");
  await page.keyboard.press("-");
  await page.keyboard.press("+");
  await page.keyboard.press("-");
  await detailLoaded;
  await expect(page.locator(".build-pill")).toContainText("interactive");
  expect(await canvas.evaluate((element) => (element as HTMLCanvasElement).toDataURL())).not.toBe(loadingFrame);
  const canvasBounds = await canvas.boundingBox();
  if (!canvasBounds) throw new Error("Map canvas has no bounds");
  await canvas.click({ position: { x: canvasBounds.width / 2, y: canvasBounds.height / 2 } });
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toBeVisible();
  await page.getByRole("button", { name: "Close tract detail" }).click();
  await page.getByRole("button", { name: "Reset map" }).click();
  await expect(page.locator(".build-pill")).toContainText("interactive");
  expect(await canvas.evaluate((element) => (element as HTMLCanvasElement).toDataURL())).toBe(nationalFrame);
  expect(regionalRequests).toHaveLength(1);
});

test("keeps narrow map actions, status, and controls fully usable", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 700 });
  await installRoutes(page);
  await page.goto("/#level=tract&cx=0.5&cy=0.5&z=1");
  await expect(page.locator(".build-pill")).toContainText("interactive");
  await expect(page.locator(".build-pill")).toBeVisible();
  await expect(page.getByRole("button", { name: "Exports" })).toBeHidden();
  await expect(page.getByRole("button", { name: "Information" })).toBeHidden();
  const more = page.getByRole("button", { name: "More" });
  await expect(more).toBeVisible();
  await expect(more).toHaveAttribute("aria-expanded", "false");
  const layout = await page.evaluate(() => {
    const rect = (selector: string) => document.querySelector(selector)!.getBoundingClientRect().toJSON();
    const actions = document.querySelector(".dock-actions")!;
    return {
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: innerWidth,
      actionsWidth: actions.scrollWidth,
      actionsViewport: actions.clientWidth,
      dock: rect(".top-dock"),
      status: rect(".build-pill"),
      controls: rect(".map-zoom"),
      legend: rect(".legend"),
    };
  });
  expect(layout.pageWidth).toBe(layout.viewportWidth);
  expect(layout.actionsWidth).toBeLessThanOrEqual(layout.actionsViewport);
  expect(layout.controls.top).toBeGreaterThanOrEqual(layout.dock.bottom);
  expect(layout.controls.bottom).toBeLessThan(layout.legend.top);
  expect(layout.controls.right <= layout.status.left || layout.status.right <= layout.controls.left).toBe(true);
  await more.click();
  await expect(more).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("button", { name: "Export snapshot" }).click();
  await expect(page.getByRole("navigation", { name: "Exports" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("navigation", { name: "Exports" })).toHaveCount(0);
  await expect(more).toBeFocused();
});
