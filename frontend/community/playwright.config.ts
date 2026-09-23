import { defineConfig } from "@playwright/test";
const ui = `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || 4318}`;
const api = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || 4317}`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: ui,
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "npm run start",
    url: ui,
    reuseExistingServer: false,
    timeout: 60_000,
    env: { OMS_COMMUNITY_API_URL: api, NEXT_TELEMETRY_DISABLED: "1" },
  },
});
