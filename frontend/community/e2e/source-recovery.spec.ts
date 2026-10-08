import { test, expect } from "@playwright/test";
import { sourceFixture } from "./source-fixture";
const api = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || "4317"}`;
const headers = { "Access-Control-Allow-Origin": `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}` };

test("remounted source reviews recover the original Apply instead of sending a fresh key", async ({ page }) => {
  await sourceFixture(page, { clean: true });
  const writes: { key: string; body: unknown }[] = [];
  await page.route(`${api}/api/skill-updates/update-1/apply`, async route => {
    if (route.request().method() !== "POST") return route.fallback();
    writes.push({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() });
    if (writes.length === 1) return route.abort("failed");
    return route.fallback();
  });
  await page.goto("/import/?github=sources&update=update-1");
  await page.getByRole("button", { name: "Apply whole skill" }).click();
  await expect(page.getByRole("button", { name: "Check result" }).first()).toBeEnabled();
  await page.reload();
  await page.getByRole("button", { name: "Apply whole skill" }).click();
  await expect(page.getByText(/A previous attempt needs recovery/)).toBeVisible();
  expect(writes).toHaveLength(1);
  await page.getByRole("button", { name: "Check result" }).first().click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toEqual(writes[0]);
});

test("private draft recovery requires the exact original input after remount", async ({ page }) => {
  await sourceFixture(page);
  const writes: { key: string; body: unknown }[] = [];
  await page.route(`${api}/api/skill-updates/update-1/draft`, async route => {
    if (route.request().method() !== "POST") return route.fallback();
    writes.push({ key: route.request().headers()["idempotency-key"], body: route.request().postDataJSON() });
    if (writes.length === 1) return route.abort("failed");
    return route.fallback();
  });
  await page.route(`${api}/api/skill-source-operations?*`, route => route.request().method() === "GET"
    ? route.fulfill({ status: 404, headers, json: { code: "operation_not_found", message: "No receipt" } }) : route.fallback());
  const fill = async (value: string) => {
    await page.getByLabel("Decision for section:intro").selectOption("merged_text");
    await page.getByLabel("Merged text for section:intro").fill(value);
  };
  await page.goto("/import/?github=sources&update=update-1");
  await fill("Original private prose");
  await page.getByRole("button", { name: "Save choices", exact: true }).click();
  await expect(page.getByRole("button", { name: "Check result" }).first()).toBeEnabled();
  expect(await page.evaluate(() => JSON.stringify({ ...sessionStorage }))).not.toContain("Original private prose");
  await page.reload();
  await fill("Changed prose");
  await page.getByRole("button", { name: "Save choices", exact: true }).click();
  await page.getByRole("button", { name: "Check result" }).first().click();
  await expect(page.getByText(/No receipt is recorded yet/)).toBeVisible();
  await page.getByRole("checkbox", { name: /exact original input/ }).check();
  await page.getByRole("button", { name: "Save choices", exact: true }).click();
  await expect(page.getByText(/original request identity does not match/)).toBeVisible();
  expect(writes).toHaveLength(1);
  await fill("Original private prose");
  await page.getByRole("checkbox", { name: /exact original input/ }).check();
  await page.getByRole("button", { name: "Save choices", exact: true }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toEqual(writes[0]);
});

test("every bulk batch outcome stays visible after a later batch succeeds", async ({ page }) => {
  const state = await sourceFixture(page, { clean: true });
  state.extraUpdates.push(...Array.from({ length: 100 }, (_, index) => ({ ...structuredClone(state.update), update_id: `extra-${index}`,
    plan: { ...structuredClone(state.update.plan), skill: { tenant_id: "local", skill_id: `skill-${index}` } } })));
  const batches: unknown[] = [];
  await page.route(`${api}/api/skill-updates/bulk-apply`, async route => {
    if (route.request().method() !== "POST") return route.fallback();
    batches.push(route.request().postDataJSON());
    return route.fulfill({ headers, json: { operation_id: `batch-${batches.length}`, state: "complete", committed: true,
      outcomes: batches.length === 1 ? [{ skill_id: "expenses", state: "failed", code: "stale", update_id: "update-1" }]
        : [{ skill_id: "skill-99", state: "applied", code: null, update_id: "extra-99" }] } });
  });
  await page.goto("/import/?github=sources");
  await page.getByRole("button", { name: "Apply all clean updates" }).click();
  await expect(page.getByText(/skill-99: Applied/)).toBeVisible();
  await expect(page.getByText(/expenses: Failed/)).toBeVisible();
  expect(batches).toHaveLength(2);
});

test("recovering an in-flight bulk batch displays its terminal outcomes without replay", async ({ page }) => {
  await sourceFixture(page, { clean: true });
  let writes = 0;
  await page.route(`${api}/api/skill-updates/bulk-apply`, async route => {
    if (route.request().method() !== "POST") return route.fallback();
    writes++;
    return route.fulfill({ headers, json: { operation_id: "bulk-flight", state: "applying", committed: false, outcomes: [] } });
  });
  await page.route(`${api}/api/skill-source-operations/bulk-flight`, route => route.request().method() === "GET" ? route.fulfill({ headers,
    json: { operation_id: "bulk-flight", state: "failed", committed: false, outcomes: [{ skill_id: "expenses", state: "failed", code: "stale", update_id: "update-1" }] } }) : route.fallback());
  await page.goto("/import/?github=sources");
  await page.getByRole("button", { name: "Apply all clean updates" }).click();
  await page.getByRole("button", { name: "Check result" }).first().click();
  await expect(page.getByText(/expenses: Failed/)).toBeVisible();
  expect(writes).toBe(1);
});
