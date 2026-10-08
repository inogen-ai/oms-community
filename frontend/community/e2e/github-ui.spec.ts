import { test, expect, type Locator, type Page } from "@playwright/test";
import { sourceFixture } from "./source-fixture";

test("removing a source asks first, lists the affected skills and sends the guarded request once", async ({ page }) => {
  const state = await sourceFixture(page);
  await page.goto("/import/?github=sources&source=source-1");
  await page.getByText("Remove source tracking", { exact: true }).click();
  await page.getByRole("button", { name: "Remove source", exact: true }).click();
  const dialog = page.getByRole("alertdialog", { name: "Remove source confirmation" });
  await expect(dialog.getByText("Warning: This action will remove the source from all skills that are part of this repository. Do you still want to remove it?")).toBeVisible();
  await dialog.getByText("Affected skills (1)").click();
  await expect(dialog.getByRole("listitem")).toHaveText(["Expense review"]);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Remove source", exact: true })).toBeFocused();
  expect(state.requests.filter(row => row.method === "POST")).toEqual([]);
  await page.getByRole("button", { name: "Remove source", exact: true }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Yes" }).click();
  await expect.poll(() => state.requests.filter(row => row.path.endsWith("/remove")).length).toBe(1);
  expect(state.requests.find(row => row.path.endsWith("/remove"))!.body).toEqual({ expected_source_generation: 1, expected_generations: [{ skill_id: "expenses", content: 1, binding: 1 }] });
});

for (const [legacy, url, view] of [
  ["/sources/?source=source-1", /\/import\/\?github=sources&source=source-1$/, "Source details"],
  ["/sources/?update=update-1", /\/import\/\?github=sources&update=update-1$/, "GitHub Source updates"],
  ["/sources/?discovery=discovery-1", /\/skills\/\?github=add&discovery=discovery-1$/, "Discovered skill packages"],
] as const) {
  test(`the old link ${legacy} opens its view`, async ({ page }) => {
    await sourceFixture(page);
    await page.goto(legacy);
    await expect(page).toHaveURL(url);
    await expect(page.getByRole("heading", { name: view, exact: true })).toBeVisible();
  });
}

// The view switches change the query string but must not scroll the page to the top.
const scrollY = (page: Page) => page.evaluate(() => window.scrollY);
async function pressKeepingScroll(page: Page, button: Locator) {
  await button.scrollIntoViewIfNeeded();
  if (await scrollY(page) === 0) await page.evaluate(() => window.scrollTo(0, 40));
  const before = await scrollY(page);
  expect(before).toBeGreaterThan(0);
  await button.click();
  return before;
}

test("each update list sits where its decisions are made", async ({ page }) => {
  await sourceFixture(page);
  const github = "After you check a repository, changes it made to your skills appear here for a decision.";
  const local = "Changes found when you re-imported a package appear here for a decision.";
  await page.goto("/inbox/");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Source updates" })).toHaveCount(0);
  await page.goto("/import/");
  const headings = page.getByRole("heading", { level: 2 });
  await expect(headings.filter({ hasText: /^(Review imported changes|Source updates|GitHub Source updates)$/ })).toHaveText(["Review imported changes", "Source updates", "GitHub Source updates"]);
  await expect(page.getByText(local)).toBeVisible();
  await expect(page.getByText("Submit a local package to compare new content.")).toBeVisible();
  await expect(page.getByText(github)).toBeVisible();
  await expect(page.getByText("GitHub source", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Tracked repositories" })).toHaveCount(0);
});

test("View GitHub sources expands the repositories above the GitHub list, with Close inside the card", async ({ page }) => {
  await sourceFixture(page);
  await page.setViewportSize({ width: 1200, height: 500 });
  await page.goto("/import/");
  const view = page.getByRole("button", { name: "View GitHub sources" });
  await expect(view).toHaveAttribute("aria-expanded", "false");
  const opened = await pressKeepingScroll(page, view);
  await expect(page).toHaveURL(/\/import\/\?github=sources$/);
  await expect(view).toHaveAttribute("aria-expanded", "true");
  expect(await scrollY(page)).toBe(opened);
  const card = page.locator(".oms-panel", { has: page.getByRole("heading", { name: "Tracked repositories" }) });
  await expect(card.getByRole("link", { name: "https://github.com/fixture/skills" })).toBeVisible();
  const [cardBox, listBox, closeBox] = await Promise.all([card.boundingBox(), page.getByText("GitHub source", { exact: true }).boundingBox(), card.getByRole("button", { name: "Close" }).boundingBox()]);
  expect(cardBox!.y + cardBox!.height).toBeLessThanOrEqual(listBox!.y);
  expect(closeBox!.y + closeBox!.height).toBeLessThanOrEqual(cardBox!.y + cardBox!.height);
  expect(closeBox!.x).toBeGreaterThanOrEqual(cardBox!.x + 20);
  const closed = await pressKeepingScroll(page, card.getByRole("button", { name: "Close" }));
  await expect(page).toHaveURL(/\/import\/$/);
  await expect(page.getByRole("heading", { name: "Tracked repositories" })).toHaveCount(0);
  expect(await scrollY(page)).toBe(closed);
  await page.goto("/import/?github=sources&source=source-1");
  await expect(page.getByRole("heading", { name: "Source details" })).toBeVisible();
  await expect(view).toHaveAttribute("aria-expanded", "true");
});

test("the import card switches between ZIP, folder and the GitHub add flow", async ({ page }) => {
  await sourceFixture(page);
  await page.setViewportSize({ width: 1200, height: 360 });
  await page.goto("/import/");
  const card = page.locator(".oms-panel", { has: page.getByRole("heading", { name: "Import a skill package" }) });
  await expect(card.getByRole("button")).toContainText(["ZIP package", "Folder", "Add a GitHub source"]);
  await expect(card.getByLabel("Skill ZIP package")).toBeAttached();
  const toGitHub = await pressKeepingScroll(page, card.getByRole("button", { name: "Add a GitHub source" }));
  await expect(page).toHaveURL(/\/import\/\?github=add$/);
  expect(await scrollY(page)).toBe(toGitHub);
  await expect(card.getByLabel("GitHub repository or skill folder URL")).toBeVisible();
  await expect(card.getByLabel("Skill ZIP package")).toHaveCount(0);
  await expect(card.getByRole("button", { name: "Preview local package" })).toHaveCount(0);
  await expect(card.getByText("Upload a folder or ZIP for a retained preview")).toBeVisible();
  const toZip = await pressKeepingScroll(page, card.getByRole("button", { name: "ZIP package" }));
  await expect(page).toHaveURL(/\/import\/$/);
  expect(await scrollY(page)).toBe(toZip);
  await expect(card.getByLabel("Skill ZIP package")).toBeAttached();
  await expect(card.getByLabel("GitHub repository or skill folder URL")).toHaveCount(0);
  await page.goto("/import/?github=add");
  await expect(card.getByLabel("GitHub repository or skill folder URL")).toBeVisible();
});

test("a linked skill carries the GitHub mark with its repository, an unlinked one does not", async ({ page }) => {
  const state = await sourceFixture(page);
  state.skills.push({ ...state.skills[0], id: "local-only", name: "Local only" });
  // A long name wraps against the card's right padding, so the heading would run under the mark without it.
  state.skills[0] = { ...state.skills[0], name: "Expense review ".repeat(8).trim() };
  await page.goto("/skills/");
  const card = page.locator(".skill-card--linked");
  const mark = card.getByRole("img", { name: "GitHub: https://github.com/fixture/skills" });
  await expect(mark).toBeVisible();
  expect(await mark.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true);
  await expect(page.locator(".skill-card", { has: page.getByRole("heading", { name: "Local only" }) }).getByRole("img")).toHaveCount(0);
  // The mark and its clear space stay inside the card and clear of the heading.
  const [markBox, cardBox, headingBox] = await Promise.all([mark.boundingBox(), card.boundingBox(), card.getByRole("heading").boundingBox()]);
  expect(markBox!.x).toBeGreaterThanOrEqual(cardBox!.x); expect(markBox!.y).toBeGreaterThanOrEqual(cardBox!.y);
  expect(markBox!.x + markBox!.width).toBeLessThanOrEqual(cardBox!.x + cardBox!.width);
  const apart = markBox!.x >= headingBox!.x + headingBox!.width || markBox!.y >= headingBox!.y + headingBox!.height || markBox!.y + markBox!.height <= headingBox!.y;
  expect(apart).toBe(true);
});

test("a linked skill's GitHub panel holds every option", async ({ page }) => {
  await sourceFixture(page);
  await page.goto("/skills/?skill=expenses");
  await page.getByRole("button", { name: "GitHub", exact: true }).click();
  const panel = page.getByRole("region", { name: "GitHub" });
  for (const name of ["Check source now", "Check this skill now", "Change tracked ref or folder"]) await expect(panel.getByRole("button", { name })).toBeVisible();
  for (const name of ["Change repository location", "Remove source tracking", "Unlink GitHub source"]) await expect(panel.getByText(name, { exact: true })).toBeVisible();
});

test("the GitHub button waits for the skill list instead of guessing whether the skill is linked", async ({ page }) => {
  await sourceFixture(page);
  const api = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || "4317"}`;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(`${api}/api/skills`, async route => { if (route.request().method() === "GET") await gate; await route.fallback(); });
  await page.goto("/skills/?skill=expenses");
  await expect(page.getByRole("heading", { name: "Expense review", level: 1 })).toBeVisible();
  await expect(page.getByRole("button", { name: "Link to GitHub repository" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "GitHub", exact: true })).toHaveCount(0);
  release();
  await expect(page.getByRole("button", { name: "GitHub", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Link to GitHub repository" })).toHaveCount(0);
});

test("Open tracked source on a skill leads to that repository on the Import page", async ({ page }) => {
  await sourceFixture(page);
  await page.goto("/skills/?skill=expenses");
  await page.getByRole("button", { name: "GitHub", exact: true }).click();
  await page.getByRole("link", { name: "Open tracked source" }).click();
  await expect(page).toHaveURL(/\/import\/\?github=sources&source=source-1$/);
  await expect(page.getByRole("heading", { name: "Source details" })).toBeVisible();
});

test("the Skills header offers Create skill, Upload skills and Add a GitHub source, with no menu; old GitHub-sources links go to Import", async ({ page }) => {
  await sourceFixture(page);
  await page.goto("/skills/");
  const header = page.locator(".page-heading");
  await expect(header.getByRole("button")).toHaveText(["Create skill", "Upload skills", "Add a GitHub source"]);
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(header.getByRole("button", { name: "Add a GitHub source" }).getByRole("img", { includeHidden: true })).toHaveCount(1);
  await page.setViewportSize({ width: 1200, height: 300 });
  const toAdd = await pressKeepingScroll(page, header.getByRole("button", { name: "Add a GitHub source" }));
  await expect(page).toHaveURL(/\/skills\/\?github=add$/);
  expect(await scrollY(page)).toBe(toAdd);
  await expect(page.getByRole("heading", { name: "Add a GitHub source" })).toBeVisible();
  await page.goto("/skills/?github=sources&source=source-1");
  await expect(page).toHaveURL(/\/import\/\?github=sources&source=source-1$/);
  await expect(page.getByRole("heading", { name: "Source details" })).toBeVisible();
});
