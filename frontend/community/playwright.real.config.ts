import { defineConfig } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";

// The caller may supply the interpreter of a clean installed core-wheel venv.
const python = process.env.OMS_CORE_PYTHON || resolve(__dirname, "../../.venv/bin/python");
process.env.OMS_BROWSER_TEST_DATA ||= mkdtempSync(join(tmpdir(), "oms-community-browser-"));
const quote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
const ui = `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || 4318}`;
const api = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || 4317}`;

export default defineConfig({
  testDir: "./e2e-real",
  timeout: 60_000,
  workers: 1,
  use: { baseURL: ui, headless: true, trace: "retain-on-failure", screenshot: "only-on-failure" },
  webServer: [
    {
      command: `${quote(python)} ${quote(resolve(__dirname, "scripts/browser_api.py"))}`,
      url: `${api}/api/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { OMS_BROWSER_TEST_DATA: process.env.OMS_BROWSER_TEST_DATA, PYTHONPATH: "" },
    },
    {
      command: "npm run start",
      url: ui,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { OMS_COMMUNITY_API_URL: api },
    },
  ],
});
