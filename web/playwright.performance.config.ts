import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e-performance",
  timeout: 180_000,
  workers: 1,
  retries: 0,
  reporter: "line",
  use: {
    baseURL: "http://127.0.0.1:8765",
    viewport: { width: 1600, height: 900 },
    deviceScaleFactor: 2,
    trace: "retain-on-failure",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], browserName: "chromium", viewport: { width: 1600, height: 900 }, deviceScaleFactor: 2 } },
    { name: "webkit", use: { ...devices["Desktop Safari"], browserName: "webkit", viewport: { width: 1600, height: 900 }, deviceScaleFactor: 2 } },
  ],
  webServer: {
    command: "../scripts/dev --no-open --port 8765",
    url: "http://127.0.0.1:8765",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
