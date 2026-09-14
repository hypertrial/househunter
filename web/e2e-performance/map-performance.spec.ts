import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { gzipSync } from "node:zlib";

const EXPECTED_TRACTS = 85_154;
const TARGETS = {
  gestureP95: 17,
  settleP95: 100,
  pickP95: 4,
  coldDetail: 500,
  coldInteractive: 1_500,
  longTask: 50,
  decodedScores: 3_500_000,
  gzipScores: 1_100_000,
};

type Profile = {
  name: string;
  start?: number;
  duration?: number;
  recordedAt?: number;
  details?: Record<string, unknown>;
  cameraGeneration?: number;
};

function percentile(values: number[], fraction: number): number {
  if (!values.length) return Infinity;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.ceil(sorted.length * fraction) - 1)];
}

test("canonical map interaction performance gates", async ({ page, browser }, testInfo) => {
  await page.addInitScript(() => {
    (window as Window & { __HOUSEHUNTER_LONG_TASKS__?: number[] }).__HOUSEHUNTER_LONG_TASKS__ = [];
    try {
      new PerformanceObserver((list) => {
        const values = (window as Window & { __HOUSEHUNTER_LONG_TASKS__?: number[] }).__HOUSEHUNTER_LONG_TASKS__!;
        for (const entry of list.getEntries()) values.push(entry.duration);
      }).observe({ type: "longtask", buffered: true });
    } catch { /* WebKit has no Long Task observer. */ }
  });

  const coldStarted = Date.now();
  await page.goto("/?profile-map", { waitUntil: "domcontentloaded" });
  await expect(page.locator(".build-pill")).toContainText("interactive", { timeout: 60_000 });
  const coldInteractive = Date.now() - coldStarted;

  const scoreText = await page.evaluate(async () => {
    const response = await fetch("/api/v2/map/scores?level=tract");
    return response.text();
  });
  const decodedScores = Buffer.byteLength(scoreText);
  const gzipScores = gzipSync(Buffer.from(scoreText)).byteLength;

  const map = page.getByRole("img", { name: /Focusable USA tract/ });
  await map.focus();
  for (let index = 0; index < 4; index += 1) await page.keyboard.press("+");

  await page.waitForFunction(() => (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
    .some((entry) => entry.name === "detail-ready"), undefined, { timeout: 30_000 });
  const requestedDetails = await page.evaluate(() => {
    const prefetch = (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
      .find((entry) => entry.name === "detail-prefetch");
    return Number((prefetch?.details as { requests?: number } | undefined)?.requests || 0);
  });
  await page.waitForFunction((count) => (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
    .filter((entry) => entry.name === "detail-ready").length >= count, requestedDetails, { timeout: 60_000 });
  await page.waitForFunction(() => {
    const profiles = window.__HOUSEHUNTER_MAP_PROFILE__ || [];
    const lastDetail = profiles.filter((entry) => entry.name === "detail-ready").at(-1)?.recordedAt;
    const lastCommit = profiles.filter((entry) => entry.name === "bitmap-commit").at(-1)?.recordedAt;
    return typeof lastDetail === "number" && typeof lastCommit === "number" && lastCommit >= lastDetail;
  }, undefined, { timeout: 10_000 });
  await page.waitForTimeout(350);
  const initialProfiles = await page.evaluate(() => window.__HOUSEHUNTER_MAP_PROFILE__ || []) as Profile[];
  expect(initialProfiles.some((entry) => entry.name === "scores-ready"
    && entry.details?.count === EXPECTED_TRACTS)).toBe(true);
  const detailLoaded = initialProfiles.find((entry) => entry.name === "detail-loaded");
  const detailReady = initialProfiles.find((entry) => entry.name === "detail-ready");
  const coldDetail = detailLoaded?.start !== undefined && detailReady?.recordedAt !== undefined
    ? detailReady.recordedAt - detailLoaded.start
    : Infinity;

  const bounds = await map.boundingBox();
  if (!bounds) throw new Error("Canonical map viewport is unavailable");

  await page.evaluate(async () => {
    const viewport = document.querySelector<HTMLElement>(".map-viewport");
    if (!viewport) throw new Error("Map viewport is unavailable");
    const bounds = viewport.getBoundingClientRect();
    const startX = bounds.left + bounds.width * 0.5;
    const startY = bounds.top + bounds.height * 0.5;
    viewport.dispatchEvent(new MouseEvent("mousedown", {
      bubbles: true, button: 0, buttons: 1, clientX: startX, clientY: startY, view: window,
    }));
    for (let step = 1; step <= 90; step += 1) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      window.dispatchEvent(new MouseEvent("mousemove", {
        bubbles: true, button: 0, buttons: 1,
        clientX: startX + bounds.width * 0.12 * step / 90,
        clientY: startY + bounds.height * 0.06 * step / 90,
        view: window,
      }));
    }
    window.dispatchEvent(new MouseEvent("mouseup", {
      bubbles: true, button: 0, buttons: 0,
      clientX: startX + bounds.width * 0.12,
      clientY: startY + bounds.height * 0.06,
      view: window,
    }));
  });
  await page.waitForTimeout(250);

  const warmDetailTransitions: number[] = [];
  for (let index = 0; index < 33; index += 1) {
    const before = await page.evaluate(() => (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
      .filter((entry) => entry.name === "camera-settle").length);
    const picksBefore = await page.evaluate(() => (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
      .filter((entry) => entry.name === "pick-roundtrip").length);
    if (index < 12) {
      // Cross the detail display threshold in both directions. This forces a
      // cached raster settle instead of measuring only the translated-frame reuse path.
      await page.keyboard.press(index % 2 ? "+" : "-");
    } else {
      const direction = index % 2 ? -8 : 8;
      await page.mouse.move(bounds.x + bounds.width * 0.5, bounds.y + bounds.height * 0.5);
      await page.mouse.down();
      await page.mouse.move(bounds.x + bounds.width * 0.5 + direction, bounds.y + bounds.height * 0.5, { steps: 2 });
      await page.mouse.up();
    }
    // Request an exact pick while the worker is processing the camera settle.
    // Sequential post-settle picks can conceal starvation behind raster work.
    await page.mouse.move(
      bounds.x + bounds.width * (0.38 + (index % 7) * 0.035),
      bounds.y + bounds.height * (0.38 + (index % 5) * 0.035),
    );
    await page.waitForFunction((count) => (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
      .filter((entry) => entry.name === "camera-settle").length > count, before, { timeout: 10_000 });
    if (index < 12 && index % 2 === 1) {
      const duration = await page.evaluate(() => (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
        .filter((entry) => entry.name === "camera-settle").at(-1)?.duration);
      if (typeof duration === "number") warmDetailTransitions.push(duration);
    }
    await page.waitForFunction((count) => (window.__HOUSEHUNTER_MAP_PROFILE__ || [])
      .filter((entry) => entry.name === "pick-roundtrip").length > count, picksBefore, { timeout: 5_000 });
  }

  const measured = await page.evaluate(() => ({
    profiles: window.__HOUSEHUNTER_MAP_PROFILE__ || [],
    longTasks: (window as Window & { __HOUSEHUNTER_LONG_TASKS__?: number[] }).__HOUSEHUNTER_LONG_TASKS__ || [],
  })) as { profiles: Profile[]; longTasks: number[] };
  const durations = (name: string) => measured.profiles
    .filter((entry) => entry.name === name && typeof entry.duration === "number")
    .map((entry) => entry.duration as number);
  const gestureFrames = durations("gesture-frame").slice(3);
  const settles = durations("camera-settle").slice(3);
  const picks = durations("pick-roundtrip").slice(3);
  const evidence = {
    revision: execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim(),
    dirty: execFileSync("git", ["status", "--porcelain"], { encoding: "utf8" }).trim().length > 0,
    browser: testInfo.project.name,
    browserVersion: browser.version(),
    viewport: { width: 1600, height: 900, deviceScaleFactor: 2 },
    featureCount: EXPECTED_TRACTS,
    samples: {
      gestureFrames: gestureFrames.length,
      settles: settles.length,
      picks: picks.length,
      warmDetailTransitions: warmDetailTransitions.length,
    },
    milliseconds: {
      coldInteractive,
      coldDetail,
      gestureMedian: percentile(gestureFrames, 0.5),
      gestureP95: percentile(gestureFrames, 0.95),
      gestureMax: Math.max(...gestureFrames),
      settleMedian: percentile(settles, 0.5),
      settleP95: percentile(settles, 0.95),
      settleMax: Math.max(...settles),
      pickMedian: percentile(picks, 0.5),
      pickP95: percentile(picks, 0.95),
      pickMax: Math.max(...picks),
      longTaskMax: measured.longTasks.length ? Math.max(...measured.longTasks) : null,
      warmDetailMedian: percentile(warmDetailTransitions, 0.5),
      warmDetailP95: percentile(warmDetailTransitions, 0.95),
      warmDetailMax: Math.max(...warmDetailTransitions),
    },
    pipeline: Object.fromEntries(["scores-parsed", "topology-parsed", "projection-ready", "bitmap-ready"]
      .map((name) => [name, initialProfiles
        .filter((entry) => entry.name === name && typeof entry.duration === "number")
        .map((entry) => entry.duration)])),
    payloadBytes: { decodedScores, gzipScores },
  };
  const evidencePath = testInfo.outputPath("map-performance.json");
  writeFileSync(evidencePath, JSON.stringify(evidence, null, 2));
  await testInfo.attach("map-performance.json", { path: evidencePath, contentType: "application/json" });

  expect(gestureFrames.length).toBeGreaterThanOrEqual(30);
  expect(settles.length).toBeGreaterThanOrEqual(30);
  expect(picks.length).toBeGreaterThanOrEqual(30);
  expect(warmDetailTransitions.length).toBeGreaterThanOrEqual(5);
  expect(percentile(gestureFrames, 0.95)).toBeLessThanOrEqual(TARGETS.gestureP95);
  expect(percentile(settles, 0.95)).toBeLessThanOrEqual(TARGETS.settleP95);
  expect(percentile(picks, 0.95)).toBeLessThanOrEqual(TARGETS.pickP95);
  expect(coldDetail).toBeLessThanOrEqual(TARGETS.coldDetail);
  expect(coldInteractive).toBeLessThanOrEqual(TARGETS.coldInteractive);
  if (testInfo.project.name === "chromium") {
    expect(Math.max(0, ...measured.longTasks)).toBeLessThanOrEqual(TARGETS.longTask);
  } else {
    expect(percentile(gestureFrames, 0.95)).toBeLessThanOrEqual(TARGETS.gestureP95);
  }
  expect(decodedScores).toBeLessThanOrEqual(TARGETS.decodedScores);
  expect(gzipScores).toBeLessThanOrEqual(TARGETS.gzipScores);
});
