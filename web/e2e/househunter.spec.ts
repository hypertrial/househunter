import { expect, test, type Page, type Route } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { geoAlbersUsa } from "d3-geo";
import { feature as topoFeature } from "topojson-client";

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
  community_conditions_group: 2, community_conditions_geography: "county", chrr_release_year: 2025,
  mountain_magnitude: 2.3213, mountain_magnitude_version: "mountain_magnitude_v2", mountain_pipeline_version: "mountain_pipeline_v1",
  relief_5km_m: 450, relief_10km_m: 700, relief_20km_m: 1200, relief_40km_m: 1800, relief_20km_pct: 88,
  rugged_fraction_20km: 0.65, rugged_pct: 85, public_mountain_access_raw: 25, public_mountain_access_pct: 75,
  open_mountain_km2_5: 2, open_mountain_km2_15: 7, open_mountain_km2_30: 16,
  restricted_mountain_km2_30: 1, closed_mountain_km2_30: 2, unknown_mountain_km2_30: 1,
  nearest_mountain_trail_km: 3.5, mountain_trail_km_10: 4, mountain_trail_km_25: 9,
  trail_access_raw: 6, trail_access_pct: 70, mountain_population_coverage: 1,
  mountain_coverage_status: "complete",
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

const exactPickPolygons = {
  type: "Topology",
  objects: { geography: { type: "GeometryCollection", geometries: [
    {
      type: "Polygon", id: "08013012101", properties: {
        place_id: "08013012101", state: "CO", county_fips: "08013", name: "Polygon with hole",
      }, arcs: [[0], [1]],
    },
    {
      type: "Polygon", id: "08013012102", properties: {
        place_id: "08013012102", state: "CO", county_fips: "08013", name: "Later overlap",
      }, arcs: [[2]],
    },
  ] } },
  arcs: [
    [[-109, 41], [-102, 41], [-102, 37], [-109, 37], [-109, 41]],
    [[-106.4, 39.6], [-106.4, 38.4], [-104.6, 38.4], [-104.6, 39.6], [-106.4, 39.6]],
    [[-108.5, 40.5], [-107, 40.5], [-107, 38], [-108.5, 38], [-108.5, 40.5]],
  ],
};

const exposedAreaStates = {
  type: "Topology",
  objects: { geography: { type: "GeometryCollection", geometries: [
    { type: "Polygon", id: "CO", properties: { state: "CO", name: "Colorado", label: [-105.5, 39] }, arcs: [[0]] },
    { type: "Polygon", id: "CA", properties: { state: "CA", name: "California", label: [-119, 37] }, arcs: [[1]] },
  ] } },
  arcs: [
    [[-109, 41], [-102, 41], [-102, 37], [-109, 37], [-109, 41]],
    [[-124, 42], [-114, 42], [-114, 32], [-124, 32], [-124, 42]],
  ],
};

const exposedCaliforniaIds = Array.from({ length: 1_024 }, (_, index) =>
  `06001${String(index).padStart(6, "0")}`);
const exposedAreaTracts = {
  type: "Topology",
  objects: { geography: { type: "GeometryCollection", geometries: [
    {
      type: "Polygon", id: summary.place_id, properties: {
        place_id: summary.place_id, state: "CO", county_fips: "08013", name: summary.name,
      }, arcs: [[0]],
    },
    ...exposedCaliforniaIds.map((id) => ({
      type: "Polygon", id, properties: {
        place_id: id, state: "CA", county_fips: "06001", name: `California ${id}`,
      }, arcs: [[1]],
    })),
  ] } },
  arcs: exposedAreaStates.arcs,
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

async function installRoutes(
  page: Page,
  initiallyPrepared = true,
  failDetailOnce = false,
  detailDelay: number | Promise<void> = 0,
) {
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
      if (name === "tracts-co.topojson.gz" && detailDelay) {
        if (typeof detailDelay === "number") {
          await new Promise((resolve) => setTimeout(resolve, detailDelay));
        } else {
          await detailDelay;
        }
      }
      await route.fulfill({ json: name === "tracts-co.topojson.gz" ? detailPolygon : polygon });
    }
  });
  await page.route("**/api/v2/**", async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v2/meta") {
      await route.fulfill({ json: {
        app_version: "2.0.0", mutation_token: "test-token", reference_assets_ready: true,
        reference_assets_error: null, build: prepared ? build : null,
        map_assets: { ready: true, error: null, schema_version: 1, release: "v1.20", manifest_url: "/map-assets/manifest.json" },
      } });
    } else if (url.pathname === "/api/v2/jobs" && route.request().method() === "POST") {
      expect(route.request().headers()["x-househunter-token"]).toBe("test-token");
      await route.fulfill({ status: 202, json: { job_id: "one", state: "running", progress: 30, message: "Building", error: null } });
    } else if (url.pathname === "/api/v2/jobs/one") {
      prepared = true;
      await route.fulfill({ json: { job_id: "one", state: "succeeded", progress: 100, message: "Complete", error: null } });
    } else if (url.pathname === "/api/v2/map/scores") {
      const county = url.searchParams.get("level") === "county";
      await route.fulfill({ json: { schema_version: 3, build_id: build.build_id, level: county ? "county" : "tract", scope: build.scope, columns: { place_id: [county ? "08013" : "08013012101"], risk_score: [county ? 18.5 : 21.25], community_conditions_group: [2], mountain_magnitude: [2.3213] } } });
    } else if (url.pathname === "/api/v2/places") {
      await route.fulfill({ json: { total: 1, items: [summary] } });
    } else if (url.pathname === "/api/v2/counties") {
      await route.fulfill({ json: { total: 1, items: [countySummary] } });
    } else if (url.pathname === "/api/v2/places/08013012101") {
      await route.fulfill({ json: detail() });
    } else if (url.pathname === "/api/v2/counties/08013") {
      await route.fulfill({ json: detail(countySummary) });
    } else if (url.pathname === "/api/v2/lookup") {
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
  const detailDrawer = page.getByRole("dialog", { name: "Tract detail" });
  await expect(detailDrawer).toContainText("Wildfire");
  await expect(detailDrawer).toContainText("No rating");
  const scoreValue = detailDrawer.locator('.metric-card[aria-label="FEMA risk"] > span');
  await expect(scoreValue).toHaveCSS("color", "rgb(185, 175, 82)");
  const wildfireBar = detailDrawer.locator(".contribution", { hasText: "Wildfire" }).locator(".bar i");
  await expect(wildfireBar).toHaveCSS("background-color", "rgb(193, 99, 65)");
});

test("renders Community Conditions as an independent county-level layer", async ({ page }) => {
  await installRoutes(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Community Conditions" }).click();
  await expect(page).toHaveURL(/metric=community-conditions/);
  await expect(page.getByLabel("Continuous Community Conditions color scale, Group 1 is healthiest"))
    .toContainText("county-level clusters, not percentiles");
  await page.getByRole("button", { name: "Best / Worst" }).click();
  const panel = page.getByRole("region", { name: "Best and worst Community Conditions" });
  await expect(panel.getByRole("heading", { name: "Best present · Group 2 · 1 counties" }))
    .toBeVisible();
  await panel.getByRole("button", { name: /Boulder.*Group 2 of 10/ }).first().click();
  const drawer = page.getByRole("dialog", { name: "County detail" });
  await expect(page.getByRole("button", { name: "Tracts", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(
    drawer.getByRole("region", { name: "Community Conditions", exact: true }),
  ).toHaveClass(/active/);
  await expect(drawer).toContainText("County-level");
});

test("renders and filters the independent Mountain Magnitude layer", async ({ page }) => {
  await installRoutes(page);
  await page.goto("/");
  await page.getByRole("button", { name: "Mountain Magnitude" }).click();
  await expect(page).toHaveURL(/metric=mountain/);
  await expect(page.getByLabel(/Continuous Mountain Magnitude color scale/))
    .toContainText("not property-specific");
  await page.getByRole("button", { name: /Filters/ }).click();
  await page.getByLabel("Minimum Mountain Magnitude").fill("2.3");
  await page.getByRole("button", { name: "Apply" }).click();
  await expect(page).toHaveURL(/mountain_magnitude_min=2\.3/);
  await page.getByRole("button", { name: "Lowest / Highest" }).click();
  await page.getByRole("button", { name: /Census tract 121.01.*M2\.32/ }).first().click();
  const drawer = page.getByRole("dialog", { name: "Tract detail" });
  await expect(drawer.getByRole("region", { name: "Mountain Magnitude", exact: true })).toHaveClass(/active/);
  await drawer.getByText("Mountain Magnitude breakdown").click();
  await expect(drawer).toContainText("1,200 m");
  await expect(drawer).toContainText("does not measure property-specific views");
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
  await expect(page.getByText(/Do not submit confidential addresses/i)).toBeVisible();
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
  const canvas = page.getByRole("img", { name: /Focusable USA tract/ });
  await expect(page.getByRole("alert")).toContainText("Detailed tract request failed");
  await expect.poll(() => regionalRequests.length).toBe(1);
  await canvas.focus();
  await page.keyboard.press("+");
  await page.keyboard.press("-");
  await page.keyboard.press("+");
  await expect(page.getByRole("alert")).toContainText("Detailed tract request failed");
  await page.waitForTimeout(250);
  expect(regionalRequests).toHaveLength(1);
  await page.getByRole("button", { name: "Retry detail" }).click();
  await expect(page.locator(".build-pill")).toContainText("interactive");
  await expect.poll(() => regionalRequests.length).toBe(2);
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
  await expect(page.getByLabel("Continuous score color scale, lower is better"))
    .toContainText("not property-level risk");
  const accessibility = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(accessibility.violations).toEqual([]);
});

test("commits a correctly sized frame after a live resize", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.endsWith("-wide"), "Resize projection runs once per browser engine");
  await installRoutes(page);
  await page.goto("/#level=tract&cx=0.5&cy=0.5&z=1");
  const canvas = page.locator(".map-presentation");
  await expect(canvas).toHaveAttribute("data-snapshot-id", /\d+/);
  const before = await canvas.evaluate((node) => ({
    snapshot: node.dataset.snapshotId,
    width: parseFloat(node.style.width),
  }));

  await page.setViewportSize({ width: 900, height: 700 });

  await expect.poll(() => canvas.getAttribute("data-snapshot-id")).not.toBe(before.snapshot);
  expect(parseFloat(await canvas.evaluate((node) => node.style.width))).toBeLessThan(before.width);
});

test("pans with pointer-only input without duplicating mouse drag", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.endsWith("-wide"), "Pointer fallback runs once per browser engine");
  await installRoutes(page);
  await page.goto("/#level=tract&cx=0.5&cy=0.5&z=2");
  const map = page.locator(".map-viewport");
  await expect(page.locator(".map-presentation")).toHaveAttribute("data-snapshot-id", /\d+/);
  const transform = () => map.evaluate((node) => {
    const value = (node as HTMLElement & { __zoom: { k: number; x: number; y: number } }).__zoom;
    return { k: value.k, x: value.x, y: value.y };
  });
  const before = await transform();

  await map.evaluate((node) => {
    const viewport = node as HTMLElement;
    viewport.setPointerCapture = () => undefined;
    viewport.releasePointerCapture = () => undefined;
    const bounds = viewport.getBoundingClientRect();
    const x = bounds.left + bounds.width / 2;
    const y = bounds.top + bounds.height / 2;
    viewport.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true, pointerId: 17, pointerType: "mouse", isPrimary: true,
      button: 0, buttons: 1, clientX: x, clientY: y,
    }));
    viewport.dispatchEvent(new PointerEvent("pointermove", {
      bubbles: true, pointerId: 17, pointerType: "mouse", isPrimary: true,
      button: -1, buttons: 1, clientX: x + 100, clientY: y + 40,
    }));
    viewport.dispatchEvent(new PointerEvent("pointerup", {
      bubbles: true, pointerId: 17, pointerType: "mouse", isPrimary: true,
      button: 0, buttons: 0, clientX: x + 100, clientY: y + 40,
    }));
  });
  await expect.poll(async () => (await transform()).x).toBeCloseTo(before.x + 100, 4);

  const pointerResult = await transform();
  await map.evaluate((node) => {
    const viewport = node as HTMLElement;
    const bounds = viewport.getBoundingClientRect();
    const x = bounds.left + bounds.width / 2;
    const y = bounds.top + bounds.height / 2;
    viewport.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true, pointerId: 18, pointerType: "mouse", isPrimary: true, ctrlKey: true,
      button: 0, buttons: 1, clientX: x, clientY: y,
    }));
    viewport.dispatchEvent(new PointerEvent("pointermove", {
      bubbles: true, pointerId: 18, pointerType: "mouse", isPrimary: true, ctrlKey: true,
      button: -1, buttons: 1, clientX: x + 60, clientY: y + 30,
    }));
    viewport.dispatchEvent(new PointerEvent("pointerup", {
      bubbles: true, pointerId: 18, pointerType: "mouse", isPrimary: true, ctrlKey: true,
      button: 0, buttons: 0, clientX: x + 60, clientY: y + 30,
    }));
  });
  expect((await transform()).x).toBeCloseTo(pointerResult.x, 4);

  const bounds = await map.boundingBox();
  if (!bounds) throw new Error("Map viewport has no bounds");
  await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
  await page.mouse.down();
  await page.mouse.move(bounds.x + bounds.width / 2 + 80, bounds.y + bounds.height / 2 + 30);
  await page.mouse.up();
  await expect.poll(async () => (await transform()).x).toBeCloseTo(pointerResult.x + 80, 4);
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toHaveCount(0);
});

test("retains detail whose projection is cancelled by resize", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.endsWith("-wide"), "Detail cancellation runs once per browser engine");
  await installRoutes(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let requests = 0;
  const ids = Array.from({ length: 12000 }, (_, index) => `08013${String(index).padStart(6, "0")}`);
  const manyDetails = {
    type: "Topology",
    objects: { geography: { type: "GeometryCollection", geometries: ids.map((id) => ({
      type: "Polygon", id, properties: { place_id: id, state: "CO", county_fips: "08013", name: id }, arcs: [[0]],
    })) } },
    arcs: detailPolygon.arcs,
  };
  await page.route("**/map-assets/tracts-co.topojson.gz", async (route) => {
    requests += 1;
    await gate;
    await route.fulfill({ json: manyDetails });
  });
  await page.goto("/?profile-map#level=tract&cx=0.5&cy=0.5&z=5");
  await expect.poll(() => requests).toBe(1);
  release();
  await expect.poll(() => page.evaluate(() => (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
    .some((entry) => entry.name === "detail-loaded"))).toBe(true);
  for (let index = 0; index < 12; index += 1) {
    await page.setViewportSize({ width: 1200 + index % 2, height: 700 + index % 2 });
  }

  await expect.poll(() => page.evaluate(() => (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
    .some((entry) => entry.name === "detail-ready")), { timeout: 10000 }).toBe(true);
  expect(requests).toBe(1);
});

test("deduplicates regional geometry while repeated zooms settle", async ({ page }, testInfo) => {
  const regionalRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("tracts-co.topojson.gz")) regionalRequests.push(request.url());
  });
  let releaseDetail!: () => void;
  const detailGate = new Promise<void>((resolve) => { releaseDetail = resolve; });
  await installRoutes(page, true, false, detailGate);
  const detailLoaded = page.waitForResponse((response) => response.url().includes("tracts-co.topojson.gz"));
  await page.goto("/?profile-map#level=tract&cx=0.5&cy=0.5&z=1");
  await expect(page.locator(".build-pill")).toContainText("interactive");
  const canvas = page.getByRole("img", { name: /Focusable USA tract/ });
  const presentation = page.locator(".map-presentation");
  const nationalFrame = await presentation.screenshot();
  await canvas.focus();
  await page.keyboard.press("+");
  await page.keyboard.press("+");
  await page.keyboard.press("+");
  await expect.poll(() => regionalRequests.length).toBe(1);
  const loadingFrame = await presentation.screenshot();
  expect(loadingFrame.equals(nationalFrame)).toBe(false);
  await page.waitForTimeout(250);
  expect(await page.evaluate(() => (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
    .some((entry) => entry.name === "detail-ready"))).toBe(false);
  await expect(presentation).toBeVisible();
  expect(regionalRequests).toHaveLength(1);
  await page.keyboard.press("+");
  await page.keyboard.press("-");
  await page.keyboard.press("+");
  await page.keyboard.press("-");
  await page.waitForTimeout(250);
  expect(regionalRequests).toHaveLength(1);
  releaseDetail();
  await detailLoaded;
  await expect(page.locator(".build-pill")).toContainText("interactive", { timeout: 15_000 });
  const canvasBounds = await canvas.boundingBox();
  if (!canvasBounds) throw new Error("Map canvas has no bounds");
  if (!testInfo.project.name.includes("phone") && !testInfo.project.name.includes("tablet")) {
    await page.mouse.move(canvasBounds.x + canvasBounds.width / 2, canvasBounds.y + canvasBounds.height / 2);
    await expect(page.locator(".map-tooltip")).toBeVisible();
  }
  await canvas.click({ position: { x: canvasBounds.width / 2, y: canvasBounds.height / 2 } });
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toBeVisible();
  await page.getByRole("button", { name: "Close tract detail" }).click();
  await page.getByRole("button", { name: "Reset map" }).click();
  await expect(page.locator(".build-pill")).toContainText("interactive");
  await expect(presentation).toHaveCSS("transform", "matrix(1, 0, 0, 1, 0, 0)");
  expect(regionalRequests).toHaveLength(1);
});

test("picks polygon holes exactly and resolves overlaps in reverse source order", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.endsWith("-wide"), "Exact worker picking runs once per browser engine");
  await installRoutes(page);
  const selected: string[] = [];
  await page.route("**/map-assets/tracts.topojson.gz", (route) => route.fulfill({ json: exactPickPolygons }));
  await page.route("**/api/v2/map/scores**", (route) => route.fulfill({ json: {
    schema_version: 3, build_id: build.build_id, level: "tract", scope: build.scope,
    columns: {
      place_id: ["08013012101", "08013012102"], risk_score: [21.25, 35],
      community_conditions_group: [2, 2], mountain_magnitude: [2.3213, 2.3213],
    },
  } }));
  await page.route("**/api/v2/places/08013012102", (route) => {
    selected.push("08013012102");
    return route.fulfill({ json: detail({
      ...summary, place_id: "08013012102", name: "Later overlap",
    }) });
  });
  await page.goto("/#level=tract&cx=0.5&cy=0.5&z=1");
  await expect(page.locator(".build-pill")).toContainText("2 tracts interactive");
  const map = page.getByRole("img", { name: /Focusable USA tract/ });
  const bounds = await map.boundingBox();
  if (!bounds) throw new Error("Map viewport has no bounds");
  const stateCollection = topoFeature(
    states as Parameters<typeof topoFeature>[0],
    states.objects.geography as Parameters<typeof topoFeature>[1],
  );
  const projection = geoAlbersUsa().fitExtent(
    [[24, 64], [Math.max(25, bounds.width - 24), Math.max(65, bounds.height - 112)]],
    stateCollection,
  );
  const screenPoint = (coordinates: [number, number]) => {
    const projected = projection(coordinates);
    if (!projected) throw new Error("Fixture point could not be projected");
    return { x: projected[0], y: projected[1] };
  };

  const hole = screenPoint([-105.5, 39]);
  await map.click({ position: hole });
  await page.waitForTimeout(100);
  expect(selected).toEqual([]);
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toHaveCount(0);

  const overlap = screenPoint([-107.75, 39]);
  await page.mouse.move(bounds.x + overlap.x, bounds.y + overlap.y);
  await expect(page.locator(".map-tooltip")).toContainText("Later overlap");
  await map.click({ position: overlap });
  await expect.poll(() => selected).toEqual(["08013012102"]);
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toContainText("Later overlap");
});

test("picks a newly exposed state before its replacement bitmap commits", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.endsWith("-wide"), "Live transformed picking runs once per browser engine");
  await installRoutes(page);
  const selectedId = exposedCaliforniaIds.at(-1)!;
  await page.route("**/map-assets/states.topojson.gz", (route) => route.fulfill({ json: exposedAreaStates }));
  await page.route("**/map-assets/tracts.topojson.gz", (route) => route.fulfill({ json: exposedAreaTracts }));
  await page.route("**/api/v2/map/scores**", (route) => route.fulfill({ json: {
    schema_version: 3, build_id: build.build_id, level: "tract", scope: build.scope,
    columns: {
      place_id: [...exposedCaliforniaIds, summary.place_id],
      risk_score: [...exposedCaliforniaIds.map(() => 35), summary.risk_score],
      community_conditions_group: [...exposedCaliforniaIds.map(() => 2), 2],
      mountain_magnitude: [...exposedCaliforniaIds.map(() => 1.5), summary.mountain_magnitude],
    },
  } }));
  await page.route(`**/api/v2/places/${selectedId}`, (route) => route.fulfill({ json: detail({
    ...summary, place_id: selectedId, name: `California ${selectedId}`, state: "CA", county_fips: "06001",
  }) }));
  await page.goto(`/?profile-map#level=tract&place=${summary.place_id}`);
  await expect(page.locator(".build-pill")).toContainText("interactive", { timeout: 15_000 });
  const map = page.getByRole("img", { name: /Focusable USA tract/ });
  const presentation = page.locator(".map-presentation");
  await expect(page.getByRole("dialog", { name: "Tract detail" })).toBeVisible();
  await page.getByRole("button", { name: "Close tract detail" }).click();
  await expect.poll(async () => Number((await page.evaluate(() =>
    (document.querySelector(".map-viewport") as HTMLElement & { __zoom?: { k: number } }).__zoom?.k)) || 0))
    .toBeGreaterThan(2);

  const bounds = await map.boundingBox();
  if (!bounds) throw new Error("Map viewport has no bounds");
  const stateCollection = topoFeature(
    exposedAreaStates as Parameters<typeof topoFeature>[0],
    exposedAreaStates.objects.geography as Parameters<typeof topoFeature>[1],
  );
  const projection = geoAlbersUsa().fitExtent(
    [[24, 64], [Math.max(25, bounds.width - 24), Math.max(65, bounds.height - 112)]],
    stateCollection,
  );
  const california = projection([-119, 37]);
  if (!california) throw new Error("California fixture could not be projected");
  const beforeSnapshot = await presentation.getAttribute("data-snapshot-id");
  await page.evaluate(({ projectedX, projectedY }) => {
    const viewport = document.querySelector<HTMLElement>(".map-viewport")!;
    const bounds = viewport.getBoundingClientRect();
    const transform = (viewport as HTMLElement & { __zoom: { k: number; x: number; y: number } }).__zoom;
    const centerX = bounds.left + bounds.width / 2;
    const centerY = bounds.top + bounds.height / 2;
    const dx = bounds.width / 2 - (projectedX * transform.k + transform.x);
    const dy = bounds.height / 2 - (projectedY * transform.k + transform.y);
    viewport.dispatchEvent(new MouseEvent("mousedown", {
      bubbles: true, button: 0, buttons: 1, clientX: centerX, clientY: centerY, view: window,
    }));
    window.dispatchEvent(new MouseEvent("mousemove", {
      bubbles: true, button: 0, buttons: 1, clientX: centerX + dx, clientY: centerY + dy, view: window,
    }));
    viewport.dispatchEvent(new PointerEvent("pointerdown", {
      bubbles: true, pointerId: 91, button: 0, buttons: 1, clientX: centerX, clientY: centerY,
    }));
    viewport.dispatchEvent(new PointerEvent("pointerup", {
      bubbles: true, pointerId: 91, button: 0, buttons: 0, clientX: centerX, clientY: centerY,
    }));
    window.dispatchEvent(new MouseEvent("mouseup", {
      bubbles: true, button: 0, buttons: 0, clientX: centerX + dx, clientY: centerY + dy, view: window,
    }));
  }, { projectedX: california[0], projectedY: california[1] });

  await expect(page.getByRole("dialog", { name: "Tract detail" })).toContainText(`California ${selectedId}`);
  const requestSnapshot = await page.evaluate(() => String((window.__HOUSEHUNTER_MAP_PROFILE__ || [])
    .filter((entry) => entry.name === "pick-request" && entry.mode === "activate").at(-1)?.snapshotId));
  expect(requestSnapshot).toBe(beforeSnapshot);
});

test("stages a non-interactive state outline before full geography is ready", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.endsWith("-wide"), "Worker staging runs once per browser engine");
  await installRoutes(page);
  await page.route("**/map-assets/tracts.topojson.gz", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 750));
    await route.fulfill({ json: polygon });
  });
  await page.goto("/");
  const presentation = page.locator(".map-presentation");
  await expect(page.locator(".build-pill")).toContainText("Drawing map outline");
  await expect.poll(() => presentation.getAttribute("data-snapshot-id")).not.toBeNull();
  const outlineSnapshot = await presentation.getAttribute("data-snapshot-id");
  await expect(page.locator(".build-pill")).not.toContainText("interactive");
  await expect(page.locator(".map-tooltip")).toHaveCount(0);

  await expect(page.locator(".build-pill")).toContainText("1 tracts interactive");
  await expect.poll(() => presentation.getAttribute("data-snapshot-id")).not.toBe(outlineSnapshot);
});

test("keeps the DPR backing raster within the bounded overscan budget", async ({ page }) => {
  await installRoutes(page);
  await page.goto("/");
  await expect(page.locator(".build-pill")).toContainText("interactive");
  const sizes = await page.evaluate(() => {
    const viewport = document.querySelector<HTMLElement>(".map-viewport")!;
    const canvas = document.querySelector<HTMLCanvasElement>(".map-presentation")!;
    const viewportBounds = viewport.getBoundingClientRect();
    return {
      viewportWidth: viewportBounds.width,
      viewportHeight: viewportBounds.height,
      cssWidth: Number.parseFloat(canvas.style.width),
      cssHeight: Number.parseFloat(canvas.style.height),
      pixelWidth: canvas.width,
      pixelHeight: canvas.height,
      devicePixelRatio: window.devicePixelRatio,
    };
  });
  const padding = Math.min(128, Math.max(48,
    Math.round(Math.min(sizes.viewportWidth, sizes.viewportHeight) * 0.1)));
  expect(sizes.cssWidth).toBe(sizes.viewportWidth + padding * 2);
  expect(sizes.cssHeight).toBe(sizes.viewportHeight + padding * 2);
  expect(sizes.pixelWidth).toBe(Math.round(sizes.cssWidth * Math.min(sizes.devicePixelRatio, 2)));
  expect(sizes.pixelHeight).toBe(Math.round(sizes.cssHeight * Math.min(sizes.devicePixelRatio, 2)));
});

test("cancels an obsolete metric paint when returning to a cached metric", async ({ page }) => {
  await installRoutes(page);
  await page.goto("/?profile-map");
  await expect(page.locator(".build-pill")).toContainText("interactive");
  const canvas = page.locator(".map-presentation");
  await expect(canvas).toBeVisible();

  await page.getByRole("button", { name: "Mountain Magnitude" }).click();
  await page.getByRole("button", { name: "FEMA Risk" }).click();
  await expect(page.locator(".build-pill")).toContainText("interactive");
  await page.waitForTimeout(350);

  await expect(page.getByRole("button", { name: "FEMA Risk" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByLabel("Continuous score color scale, lower is better")).toBeVisible();
  await expect(page.getByLabel(/Continuous Mountain Magnitude color scale/)).toHaveCount(0);
  expect(await page.evaluate(() => (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
    .filter((entry) => entry.name === "bitmap-commit" || entry.name === "bitmap-reuse-commit")
    .at(-1)?.metric)).toBe("fema");
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

  const canvas = page.getByRole("img", { name: /Focusable USA tract/ });
  const canvasBounds = await canvas.boundingBox();
  if (!canvasBounds) throw new Error("Map canvas has no bounds");
  await canvas.click({ position: { x: canvasBounds.width / 2, y: canvasBounds.height / 2 } });
  const drawer = page.getByRole("dialog", { name: "Tract detail" });
  await expect(drawer).toBeVisible();
  await expect(drawer.locator(".metric-card")).toHaveCount(3);
  const detailLayout = await drawer.evaluate((element) => {
    const drawerBounds = element.getBoundingClientRect();
    const cards = [...element.querySelectorAll<HTMLElement>(".metric-card")]
      .map((card) => card.getBoundingClientRect());
    return {
      left: drawerBounds.left,
      right: drawerBounds.right,
      bottom: drawerBounds.bottom,
      height: drawerBounds.height,
      cardLefts: cards.map((card) => card.left),
      cardTops: cards.map((card) => card.top),
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
    };
  });
  expect(detailLayout.left).toBe(0);
  expect(detailLayout.right).toBe(320);
  expect(detailLayout.bottom).toBe(700);
  expect(detailLayout.height).toBeLessThanOrEqual(0.61 * 700 + 1);
  expect(new Set(detailLayout.cardLefts).size).toBe(1);
  expect(detailLayout.cardTops).toEqual([...detailLayout.cardTops].sort((a, b) => a - b));
  expect(detailLayout.scrollWidth).toBeLessThanOrEqual(detailLayout.clientWidth);
  await page.getByRole("button", { name: "Close tract detail" }).click();
  await expect(canvas).toBeFocused();

  await more.click();
  await expect(more).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("button", { name: "Export snapshot" }).click();
  await expect(page.getByRole("navigation", { name: "Exports" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Tracts CSV" })).toBeFocused();
  await expect(more).toHaveAttribute("aria-expanded", "true");
  await expect(more).toHaveAttribute("aria-controls", "exports-panel");
  await more.click();
  await expect(page.getByRole("navigation", { name: "Exports" })).toHaveCount(0);
  await expect(more).toHaveAttribute("aria-expanded", "false");
  await expect(more).toBeFocused();
  await more.click();
  await page.getByRole("button", { name: "About this map" }).click();
  await expect(page.getByRole("region", { name: "About this map" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "About this map" })).toBeFocused();
  await expect(more).toHaveAttribute("aria-expanded", "true");
  await expect(more).toHaveAttribute("aria-controls", "info-panel");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("region", { name: "About this map" })).toHaveCount(0);
  await expect(more).toBeFocused();
});
