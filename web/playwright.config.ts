import { defineConfig, devices } from "@playwright/test";

const sizes = [
  { name: "phone", viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true },
  { name: "tablet", viewport: { width: 768, height: 1024 }, hasTouch: true, isMobile: true },
  { name: "desktop", viewport: { width: 1280, height: 720 }, hasTouch: false, isMobile: false },
  { name: "wide", viewport: { width: 1600, height: 900 }, hasTouch: false, isMobile: false },
];

export default defineConfig({
  testDir: "./e2e",
  use: { baseURL: "http://127.0.0.1:4173" },
  projects: ["chromium", "webkit"].flatMap((browserName) => sizes.map((size) => ({
    name: `${browserName}-${size.name}`,
    use: {
      ...(browserName === "chromium" ? devices["Desktop Chrome"] : devices["Desktop Safari"]),
      browserName: browserName as "chromium" | "webkit",
      viewport: size.viewport,
      hasTouch: size.hasTouch,
      isMobile: size.isMobile,
    },
  }))),
  webServer: {
    command: "npm run preview -- --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: true,
  },
});
