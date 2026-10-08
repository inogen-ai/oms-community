import { test, expect } from "@playwright/test";
import { sourceFixture } from "./source-fixture";

test("a local re-import is reviewed on Import review, reached from the upload result, and not under GitHub sources", async ({ page }) => {
  const state = await sourceFixture(page, { localReview: true });
  await page.goto("/import/");
  await page.getByLabel("Skill ZIP package").setInputFiles({ name: "bundle.zip", mimeType: "application/zip", buffer: Buffer.from("fixture") });
  await page.getByRole("button", { name: "Preview local package" }).click();
  await page.getByLabel("Destination for bundle").selectOption("expenses");
  await page.getByRole("button", { name: "Apply selected local packages" }).click();
  await page.getByRole("link", { name: "Review the update for expenses" }).click();
  await expect(page).toHaveURL(/\/import\/\?update=update-local$/);
  await expect(page.getByLabel("Decision for section:intro")).toBeVisible();
  await expect(page.getByText("Changes found when you re-imported a package appear here for a decision.")).toBeVisible();
  await expect(page.getByText("Local package", { exact: true })).toBeVisible();
  // GitHub cards stay under View GitHub sources, and the local card is not listed in that view.
  await page.goto("/import/?github=sources");
  const githubView = page.getByRole("region", { name: "GitHub source updates" });
  await expect(githubView.getByText("GitHub source", { exact: true })).toBeVisible();
  await expect(githubView.getByText("Local package", { exact: true })).toHaveCount(0);
  await expect(githubView.getByText("Changes found when you re-imported a package appear here for a decision.")).toHaveCount(0);
  await page.goto("/import/?update=update-local");
  await page.getByRole("button", { name: "Skip this version" }).click();
  await expect.poll(() => state.localUpdate.status).toBe("skipped");
  expect(state.update.status).toBe("open");
  expect(state.requests.filter(row => row.path.endsWith("/skip")).map(row => row.path)).toEqual(["/api/skill-updates/update-local/skip"]);
});

test("a legacy link to a local update points to where it is reviewed", async ({ page }) => {
  await sourceFixture(page, { localReview: true });
  await page.goto("/sources/?update=update-local");
  await expect(page).toHaveURL(/import\/\?github=sources&update=update-local$/);
  await page.getByRole("link", { name: "Open it where it belongs" }).click();
  await expect(page).toHaveURL(/\/import\/\?update=update-local$/);
  await expect(page.getByLabel("Decision for section:intro")).toBeVisible();
});

test("source history stays in the History tab of an unlinked skill", async ({ page }) => {
  await sourceFixture(page, { unlinked: true, undo: true });
  await page.goto("/skills/?skill=expenses");
  await page.getByRole("tab", { name: "History", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Source and attachment history" })).toBeVisible();
});

test("a linked skill keeps source history in its GitHub panel only", async ({ page }) => {
  await sourceFixture(page, { undo: true });
  await page.goto("/skills/?skill=expenses");
  await page.getByRole("tab", { name: "History", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Saved versions", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Source and attachment history" })).toHaveCount(0);
  await page.getByRole("button", { name: "GitHub", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Source and attachment history" })).toBeVisible();
});
