import { test, expect } from "@playwright/test";
import { sourceFixture } from "./source-fixture";

test("an unreserved throttled check stays retryable after reload", async ({ page }) => {
  await sourceFixture(page);
  const api = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || "4317"}`;
  const headers = { "Access-Control-Allow-Origin": `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}` };
  const keys: string[] = [];
  await page.route(`${api}/api/skill-sources/source-1/check`, async route => {
    if (route.request().method() !== "POST") return route.fallback();
    keys.push(route.request().headers()["idempotency-key"]);
    if (keys.length === 2) return route.fulfill({ status: 429, headers: { ...headers, "Retry-After": "60" },
      json: { code: "check_throttled", message: "Check throttled", operation_id: null, retry_after_seconds: 60 } });
    return route.fulfill({ headers, json: { operation_id: `check-${keys.length}`, state: "complete", committed: false, outcomes: [] } });
  });
  await page.goto("/sources/?source=source-1");
  await page.getByRole("button", { name: "Check source now", exact: true }).click();
  await expect.poll(() => keys.length).toBe(1);
  await page.getByRole("button", { name: "Check source now", exact: true }).click();
  await expect(page.getByText(/Try again after 60 seconds/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Recover previous request" })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Pending source requests" })).toHaveCount(0);
  await page.getByRole("button", { name: "Check source now", exact: true }).click();
  await expect.poll(() => keys.length).toBe(3);
  expect(new Set(keys).size).toBe(3);
});
