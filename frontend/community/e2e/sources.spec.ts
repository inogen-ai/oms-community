import { test, expect, type Page } from "@playwright/test";
import { sourceFixture } from "./source-fixture";
import type { SourceOperation } from "@inogen/oms-client";

const openAddSource = async (page: Page) => {
  await page.goto("/skills/");
  await page.getByRole("button", { name: "Add a GitHub source", exact: true }).click();
};
const github = (page: Page) => page.getByRole("button", { name: "GitHub", exact: true });

test("source controls are hidden when the capability is disabled", async ({ page }) => {
  await sourceFixture(page, { enabled: false });
  await page.goto("/sources/");
  await expect(page).toHaveURL(/\/import\/\?github=sources$/);
  await expect(page.getByText("Source tracking is unavailable on this server.")).toBeVisible();
  await expect(page.getByRole("navigation").getByRole("link", { name: "Sources", exact: true })).toHaveCount(0);
  await page.goto("/skills/");
  await expect(page.getByRole("button", { name: "Create skill" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Add a GitHub source" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "GitHub", exact: true })).toHaveCount(0);
});

test("a copied source deep link survives reload and Check now carries source guards", async ({ page }) => {
  const state = await sourceFixture(page, { blocked: true });
  await page.goto("/import/?github=sources&source=source-1");
  await expect(page.getByRole("heading", { name: "Source details" })).toBeVisible();
  await expect(page.getByText("Retry needed", { exact: true }).first()).toBeVisible();
  await page.reload();
  await expect(page.getByText("Retry needed", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "Check source now", exact: true }).click();
  await expect.poll(() => state.requests.filter(row => row.path.endsWith("/check")).length).toBe(1);
  const request = state.requests.find(row => row.path.endsWith("/check"))!;
  expect(request.body).toEqual({ expected_source_generation: 1, skill_ids: ["expenses"] });
  expect(request.key).toBeTruthy();
  expect(state.violations).toEqual([]);
});

test("a check result lists its outcomes without install counts", async ({ page }) => {
  await sourceFixture(page);
  await page.goto("/import/?github=sources&source=source-1");
  await page.getByRole("button", { name: "Check source now", exact: true }).click();
  await expect(page.getByText("expenses: Awaiting review", { exact: true })).toBeVisible();
  await expect(page.getByText(/held for review|\d+ installed/)).toHaveCount(0);
});

test("repository discovery starts unselected and preserves local installation names", async ({ page }) => {
  const state = await sourceFixture(page);
  await openAddSource(page);
  await page.getByLabel("GitHub repository or skill folder URL").fill("https://github.com/fixture/skills");
  await page.getByRole("button", { name: "Discover packages", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Discovered skill packages" })).toBeVisible();
  await expect(page.getByLabel("Select expenses", { exact: true })).not.toBeChecked();
  await expect(page.getByLabel("Select orders", { exact: true })).not.toBeChecked();
  await page.getByLabel("Select orders", { exact: true }).check();
  await page.getByText("Edit names and areas").click();
  await page.getByLabel("Local name for orders").fill("Local purchase guidance");
  await page.getByRole("button", { name: "Install selected skills" }).click();
  await expect.poll(() => state.requests.filter(row => row.path === "/api/skill-source-installations").length).toBe(1);
  expect(state.requests.find(row => row.path === "/api/skill-source-installations")!.body).toEqual({ discovery_id: "discovery-1", selections: [{ package_path: "orders", local_name: "Local purchase guidance", domain: "general" }] });
});

// The name box carries its skill ID under it; the domain box has nothing below it. Both boxes still line up.
test("a selected package's local name and domain boxes line up", async ({ page }) => {
  await sourceFixture(page);
  await openAddSource(page);
  await page.getByLabel("GitHub repository or skill folder URL").fill("https://github.com/fixture/skills");
  await page.getByRole("button", { name: "Discover packages", exact: true }).click();
  await page.getByLabel("Select orders", { exact: true }).check();
  await page.getByText("Edit names and areas").click();
  const name = (await page.getByLabel("Local name for orders").boundingBox())!;
  const domain = (await page.getByLabel("Domain for orders").boundingBox())!;
  expect(Math.abs(name.y - domain.y)).toBeLessThanOrEqual(1);
  expect(Math.abs(name.height - domain.height)).toBeLessThanOrEqual(1);
});

test("edited text is saved and applied in one action, and a lost Apply response recovers the same request", async ({ page }) => {
  const state = await sourceFixture(page, { loseApply: true });
  await page.goto("/import/?github=sources&update=update-1");
  await page.getByLabel("Edit the text for section:intro").check();
  // Exact: a substring match would also find the "Edit the text for section:intro" radio.
  await page.getByLabel("Text for section:intro", { exact: true }).fill("Retain receipts and verify each order.");
  await page.getByRole("button", { name: "Apply update" }).click();
  await expect.poll(() => state.decisions).toEqual(["apply"]);
  const draftAt = state.requests.findIndex(row => row.path.endsWith("/draft"));
  expect(draftAt).toBeGreaterThanOrEqual(0);
  expect(draftAt).toBeLessThan(state.requests.findIndex(row => row.path.endsWith("/apply")));
  await page.reload();
  await expect(page.getByText("A previous request did not confirm. Your browser sent it but did not get the answer. Check its result before trying again; checking does not start a new request.")).toBeVisible();
  await page.getByRole("button", { name: "Check result" }).click();
  await expect(page.getByRole("button", { name: "Check result" })).toHaveCount(0);
  const applies = state.requests.filter(row => row.path.endsWith("/apply"));
  expect(applies).toHaveLength(2);
  expect(applies[0].key).toEqual(applies[1].key);
  expect(state.decisions).toEqual(["apply"]);
});

test("Skip and keep-current/adopt use distinct actions and do not claim an apply", async ({ page }) => {
  const state = await sourceFixture(page);
  await page.goto("/import/?github=sources&update=update-1");
  await page.getByRole("button", { name: "Skip this version" }).click();
  await expect.poll(() => state.decisions).toEqual(["skip"]);
  state.update.status = "open";
  await page.reload();
  await page.getByRole("button", { name: "Keep my version and mark this update as seen" }).click();
  await expect.poll(() => state.decisions).toEqual(["skip", "adopt"]);
  expect(state.requests.filter(row => row.path.endsWith("/apply"))).toHaveLength(0);
});

test("Apply stops when someone else saved other choices, says so and shows their choices", async ({ page }) => {
  // Ruling T6-2: drafts are one shared record; another reviewer's selection is never applied in this reviewer's name.
  const state = await sourceFixture(page);
  const api = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || "4317"}`;
  const headers = { "Access-Control-Allow-Origin": `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}` };
  await page.route(`${api}/api/skill-updates/update-1`, async route => {
    if (route.request().method() !== "GET" || !state.requests.some(row => row.path.endsWith("/draft"))) return route.fallback();
    const theirs = state.update.drafts.map(choice => ({ ...choice, choice: "use_upstream" as const, merged_text: null }));
    await route.fulfill({ headers, json: { ...structuredClone(state.update), drafts: theirs, resolved_check: { format_version: 1, state: "passed", code: "resolved_writes_checked", reasons: [] } } });
  });
  await page.goto("/import/?github=sources&update=update-1");
  await page.getByLabel("Keep yours for section:intro").check();
  await page.getByRole("button", { name: "Apply update" }).click();
  await expect(page.getByText("Someone else changed the choices. Check them and try again.")).toBeVisible();
  expect(state.requests.filter(row => row.path.endsWith("/apply"))).toHaveLength(0);
  await expect(page.getByLabel("Take GitHub's version for section:intro")).toBeChecked();
});

for (const [control, action] of [["Check safety again", "recheck"], ["Skip this version", "skip"], ["Keep my version and mark this update as seen", "adopt"]]) {
  test(`${control} clears the notice of a stopped Apply`, async ({ page }) => {
    // Ruling F4: the notice belongs to the Apply that stopped, not to the next action. The action
    // is refused here so the card stays on screen whatever the action does to it.
    const state = await sourceFixture(page);
    const api = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || "4317"}`;
    const headers = { "Access-Control-Allow-Origin": `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}` };
    await page.route(`${api}/api/skill-updates/update-1/${action}`, route => route.request().method() === "POST"
      ? route.fulfill({ status: 409, headers, json: { code: "update_changed", message: "The update changed." } }) : route.fallback());
    await page.route(`${api}/api/skill-updates/update-1`, async route => {
      if (route.request().method() !== "GET" || !state.requests.some(row => row.path.endsWith("/draft"))) return route.fallback();
      const theirs = state.update.drafts.map(choice => ({ ...choice, choice: "use_upstream" as const, merged_text: null }));
      await route.fulfill({ headers, json: { ...structuredClone(state.update), drafts: theirs, resolved_check: { format_version: 1, state: "passed", code: "resolved_writes_checked", reasons: [] } } });
    });
    await page.goto("/import/?github=sources&update=update-1");
    await page.getByLabel("Keep yours for section:intro").check();
    await page.getByRole("button", { name: "Apply update" }).click();
    const notice = page.getByText("Someone else changed the choices. Check them and try again.");
    await expect(notice).toBeVisible();
    await page.getByRole("button", { name: control }).click();
    await expect(notice).toHaveCount(0);
  });
}

test("a removal is one choice, and keeping it sends no consent", async ({ page }) => {
  const state = await sourceFixture(page, { removal: true });
  await page.goto("/import/?github=sources&update=update-1");
  await expect(page.getByRole("button", { name: "Apply update" })).toBeDisabled();
  await page.getByLabel("Keep it for section:intro").check();
  await expect(page.getByRole("button", { name: "Apply update" })).toBeEnabled();
  await page.getByRole("button", { name: "Apply update" }).click();
  await expect.poll(() => state.decisions).toEqual(["apply"]);
  expect(state.requests.find(row => row.path.endsWith("/apply"))!.body).toMatchObject({ removal_consents: [] });
});

test("bulk Apply excludes executable updates and preserves per-skill results", async ({ page }) => {
  const state = await sourceFixture(page, { clean: true, script: true });
  await page.goto("/import/?github=sources");
  await expect(page.getByText("Scripts or executable files")).toBeVisible();
  await page.getByRole("button", { name: "Apply all clean updates" }).click();
  await expect.poll(() => state.decisions).toEqual(["bulk-apply"]);
  const body = state.requests.find(row => row.path.endsWith("/bulk-apply"))!.body as { updates: { update_id: string }[] };
  expect(body.updates.map(row => row.update_id)).toEqual(["update-1"]);
  expect(state.extraUpdates[0].status).toBe("open");
});

test("staged local imports require an explicit target and recover without retaining uploaded bytes", async ({ page }) => {
  const state = await sourceFixture(page, { loseLocal: true });
  await page.goto("/import/");
  await page.getByLabel("Skill ZIP package").setInputFiles({ name: "expenses.zip", mimeType: "application/zip", buffer: Buffer.from("private-upload-bytes") });
  await page.getByRole("button", { name: "Preview local package" }).click();
  const target = page.getByLabel("Destination for bundle");
  await expect(target).toHaveValue("");
  await expect(page.getByRole("button", { name: "Apply selected local packages" })).toBeDisabled();
  await target.selectOption("expenses");
  await page.getByRole("button", { name: "Apply selected local packages" }).click();
  await expect.poll(() => state.localLost).toBe(true);
  const retained = await page.evaluate(() => JSON.stringify({ ...sessionStorage }));
  expect(retained).not.toContain("private-upload-bytes");
  expect(retained).toContain("upload-1");
  await page.reload();
  await expect(page.getByText("A previous import did not confirm. Your browser sent the request but did not get the answer. Check its result before trying again; checking does not start a new import.")).toBeVisible();
  await page.getByRole("button", { name: "Check result" }).click();
  await expect(page.getByRole("button", { name: "Check result" })).toHaveCount(0);
  const requests = state.requests.filter(row => row.path === "/api/skill-local-imports");
  expect(requests).toHaveLength(2);
  expect(requests[0].key).toEqual(requests[1].key);
  expect(requests[0].body).toEqual({ upload_id: "upload-1", selections: [{ package_path: "bundle", target_skill_id: "expenses" }] });
  expect(state.decisions).toEqual(["local-import"]);
});

test("a refused retarget leaves the committed ref visible", async ({ page }) => {
  const state = await sourceFixture(page, { blockRetarget: true });
  await page.goto("/import/?github=sources&source=source-1");
  await page.getByRole("button", { name: "Change tracked ref or folder" }).click();
  await page.getByText("Advanced", { exact: true }).click();
  await page.getByLabel("What to follow").selectOption("tag");
  await page.getByLabel("Branch, tag or commit").fill("v2");
  await page.getByLabel("Skill folder (optional)").fill("expenses");
  await page.getByRole("button", { name: "Discover packages", exact: true }).click();
  await page.getByRole("button", { name: "Retarget source", exact: true }).click();
  await expect.poll(() => state.requests.filter(row => row.path.endsWith("/retarget")).length).toBe(1);
  expect(state.binding.binding?.ref.name).toBe("main");
  await expect(page.getByText("Branch: main", { exact: false })).toBeVisible();
});

test("source history exposes guarded undo without changing document restore semantics", async ({ page }) => {
  const state = await sourceFixture(page, { undo: true });
  await page.goto("/skills/?skill=expenses");
  await github(page).click();
  await expect(page.getByRole("heading", { name: "Source and attachment history" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Undo source update", exact: true })).toBeDisabled();
  await page.getByLabel("Confirm undo of this source update").check();
  await page.getByRole("button", { name: "Undo source update", exact: true }).click();
  await expect.poll(() => state.undone).toBe(true);
  expect(state.requests.find(row => row.path.endsWith("/undo"))!.body).toEqual({ undo_id: "undo-1", policy_version: "p", expected_generations: [{ skill_id: "expenses", content: 1, binding: 1 }], skill_id: "expenses" });
});

test("linking creates an admitted source identity and unlinking preserves the skill", async ({ page }) => {
  const state = await sourceFixture(page, { unlinked: true });
  await page.goto("/skills/?skill=expenses");
  await page.getByRole("button", { name: "Link to GitHub repository", exact: true }).click();
  await page.getByLabel("GitHub repository or skill folder URL").fill("https://github.com/fixture/skills");
  await page.getByText("Advanced", { exact: true }).click();
  await page.getByLabel("Skill folder (optional)").fill("expenses");
  await page.getByRole("button", { name: "Discover packages", exact: true }).click();
  await page.getByRole("button", { name: "Confirm link", exact: true }).click();
  await expect.poll(() => state.requests.some(row => row.path.endsWith("/source-binding/link"))).toBe(true);
  expect(state.requests.find(row => row.path === "/api/skill-sources" && row.method === "POST")!.body).toEqual({ discovery_id: "discovery-1" });
  await expect(github(page)).toBeVisible();
  await page.getByText("Unlink GitHub source", { exact: true }).click();
  await page.getByLabel("Confirm unlinking this skill").check();
  await page.getByRole("button", { name: "Unlink source", exact: true }).click();
  await expect.poll(() => state.binding.binding?.active).toBe(false);
  expect(state.skills[0].name).toBe("Expense review");
  await expect(page.getByRole("button", { name: "Relink source", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Link to GitHub repository", exact: true })).toBeVisible();
});

test("a blocked local attempt survives closure and resubmission clears only its own retry state", async ({ page }) => {
  const state = await sourceFixture(page, { blocked: true, blockLocal: true });
  const upload = async () => {
    await page.goto("/import/");
    await page.getByLabel("Skill ZIP package").setInputFiles({ name: "bundle.zip", mimeType: "application/zip", buffer: Buffer.from("fixture") });
    await page.getByRole("button", { name: "Preview local package" }).click();
    await page.getByLabel("Destination for bundle").selectOption("expenses");
    await page.getByRole("button", { name: "Apply selected local packages" }).click();
  };
  await upload();
  // Leaving before the refusal reaches the browser would keep the request pending and block the resubmission below.
  await expect(page.getByText(/This local attempt was refused/)).toBeVisible();
  await expect.poll(() => state.binding.blocked_attempts.some(row => row.attempt_id === "local-attempt")).toBe(true);
  await page.goto("/import/?github=sources&source=source-1&update=update-1");
  await page.getByRole("button", { name: "Skip this version" }).click();
  await expect.poll(() => state.decisions).toEqual(["skip"]);
  await page.reload();
  await expect(page.getByText(/Local upload refused/)).toBeVisible();
  expect(state.binding.blocked_attempts).toHaveLength(2);
  await upload();
  await expect.poll(() => state.binding.blocked_attempts.map(row => row.attempt_id)).toEqual(["blocked-1"]);
});

test("late binding and card responses cannot restore the old ref after retarget", async ({ page }) => {
  const state = await sourceFixture(page);
  const api = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || "4317"}`;
  let delaying = false;
  const queued: (() => void)[] = [];
  const completed: Promise<void>[] = [];
  await page.route(`${api}/api/**`, async route => {
    const path = new URL(route.request().url()).pathname;
    if (!delaying || route.request().method() !== "GET" || !["/api/skills/expenses/source-binding", "/api/skill-updates/update-1"].includes(path)) return route.fallback();
    const snapshot = structuredClone(path.includes("source-binding") ? state.binding : state.update);
    let done!: () => void;
    completed.push(new Promise(resolve => { done = resolve; }));
    await new Promise<void>(resolve => queued.push(resolve));
    await route.fulfill({ json: snapshot, headers: { "Access-Control-Allow-Origin": `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}` } });
    done();
  });
  await page.goto("/import/?github=sources&source=source-1&update=update-1");
  await expect(page.getByLabel("Decision for section:intro")).toBeVisible();
  // The review revision sits in the card's closed "Technical details".
  await page.locator(".oms-source-technical > summary").click();
  delaying = true;
  await page.getByRole("button", { name: "Check source now", exact: true }).click();
  await expect.poll(() => queued.length).toBe(2);
  delaying = false;
  await page.getByRole("button", { name: "Change tracked ref or folder" }).click();
  await page.getByText("Advanced", { exact: true }).click();
  await page.getByLabel("What to follow").selectOption("tag");
  await page.getByLabel("Branch, tag or commit").fill("v2");
  await page.getByLabel("Skill folder (optional)").fill("expenses");
  await page.getByRole("button", { name: "Discover packages", exact: true }).click();
  await page.getByRole("button", { name: "Retarget source", exact: true }).click();
  await expect(page.getByText("Pinned tag: v2", { exact: false })).toBeVisible();
  await expect(page.getByText(/content 1 · binding 2/)).toBeVisible();
  queued.forEach(resolve => resolve()); await Promise.all(completed);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await expect(page.getByText("Branch: main", { exact: false })).toHaveCount(0);
  await expect(page.getByText(/content 1 · binding 2/)).toBeVisible();
});

test("file previews remain inert and downloads use authorised side paths", async ({ page }) => {
  const state = await sourceFixture(page, { file: true });
  await page.goto("/import/?github=sources&update=update-1");
  // A change that needs no decision is listed under the card's closed "Show"; the file opens from there.
  await page.locator(".oms-source-list > summary").click();
  await page.locator(".oms-source-safe-item > summary").click();
  await expect(page.getByText("Retained but excluded from publication: reserved output")).toBeVisible();
  await expect(page.locator(".oms-source-files pre")).toHaveText(Array(3).fill("<script>window.bad = true</script>"));
  expect(await page.evaluate(() => "bad" in window)).toBe(false);
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download this version (New on GitHub)", exact: true }).click();
  expect((await download).suggestedFilename()).toBe("guide.md");
  expect(state.requests.some(row => row.path.endsWith("/files"))).toBe(true);
});

test("opening a flagged file shows its first flagged line, marked and tagged, and fetches only the GitHub side", async ({ page }) => {
  const state = await sourceFixture(page, { flagged: true });
  await page.goto("/import/?github=sources&update=update-1");
  const item = page.getByRole("region", { name: "Item references/contacts.md" });
  await expect(item.getByText("3 lines look like a phone number: lines 40, 263, 400")).toBeVisible();
  await item.locator(".oms-source-show > summary").click();
  const preview = item.locator(".oms-source-numbered");
  const first = preview.locator('[data-line="40"]');
  await expect(first).toHaveClass(/oms-source-flagged/);
  await expect(first.locator(".oms-source-flag")).toHaveText("looks like a phone number");
  await expect(first).toBeInViewport();
  await expect(preview.locator('[data-line="263"]')).not.toBeInViewport();
  // A new file: only the GitHub side, and no Download button for the sides OMS does not have.
  await expect(item.getByText("This file is new. OMS does not have it yet.")).toBeVisible();
  await expect(item.getByRole("heading", { name: "New on GitHub" })).toBeVisible();
  await expect(item.getByRole("heading", { name: "Last version taken from GitHub" })).toHaveCount(0);
  await expect(item.getByRole("button", { name: /^Download this version/ })).toHaveCount(1);
  await expect(item.getByRole("button", { name: "Download this version (New on GitHub)" })).toBeVisible();
  const files = state.requests.filter(row => row.path.endsWith("/files"));
  expect(files.length).toBeGreaterThan(0);
  expect(files.every(row => new URLSearchParams(row.query).get("side") === "upstream")).toBe(true);
});

test("a line number in the reason opens Show at that line", async ({ page }) => {
  await sourceFixture(page, { flagged: true });
  await page.goto("/import/?github=sources&update=update-1");
  const item = page.getByRole("region", { name: "Item references/contacts.md" });
  const preview = item.locator(".oms-source-numbered");
  // Wait for the preview to load inside the closed disclosure.
  await expect(preview.locator('[data-line="263"]')).toHaveCount(1);
  await item.getByRole("button", { name: "Show line 263 of references/contacts.md" }).click();
  await expect(item.locator(".oms-source-show")).toHaveAttribute("open", "");
  await expect(preview.locator('[data-line="263"]')).toBeInViewport();
  await expect(preview.locator('[data-line="40"]')).not.toBeInViewport();
  await expect(preview).toBeFocused();
  // With Show already open, another line number scrolls the same preview.
  await item.getByRole("button", { name: "Show line 40 of references/contacts.md" }).click();
  await expect(preview.locator('[data-line="40"]')).toBeInViewport();
  await expect(preview.locator('[data-line="263"]')).not.toBeInViewport();
  // A line the preview does not hold: the button takes the reader to the note that says so.
  await item.getByRole("button", { name: "Show line 400 of references/contacts.md" }).click();
  const note = item.getByText("Line 400 is beyond this preview.");
  await expect(note).toBeFocused();
  await expect(note).toBeInViewport();
});

test("a line number clicked before the preview loads scrolls to that line once it arrives", async ({ page }) => {
  await sourceFixture(page, { flagged: true });
  let release!: () => void;
  const loaded = new Promise<void>(resolve => { release = resolve; });
  await page.route(url => url.pathname.endsWith("/files"), async route => { await loaded; await route.fallback(); });
  await page.goto("/import/?github=sources&update=update-1");
  const item = page.getByRole("region", { name: "Item references/contacts.md" });
  await item.getByRole("button", { name: "Show line 263 of references/contacts.md" }).click();
  await expect(item.locator(".oms-source-show")).toHaveAttribute("open", "");
  await expect(item.getByText("Loading preview…")).toBeVisible();
  release();
  const preview = item.locator(".oms-source-numbered");
  await expect(preview.locator('[data-line="263"]')).toBeInViewport();
  await expect(preview.locator('[data-line="40"]')).not.toBeInViewport();
  await expect(preview).toBeFocused();
});

test("Community rejects a runtime workspace override before contacting the API", async ({ page }) => {
  const state = await sourceFixture(page);
  await page.route("**/oms-config.json", route => route.fulfill({ json: { api_url: "", tenant: "other-workspace" } }));
  await page.goto("/import/?github=sources");
  await expect(page.getByText("Community configuration cannot override the local workspace.")).toBeVisible();
  expect(state.requests).toHaveLength(0);
});

test("the source workspace remains readable with keyboard focus on desktop and mobile", async ({ page }) => {
  await sourceFixture(page);
  for (const width of [1320, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/import/?github=sources&update=update-1");
    await expect(page.getByLabel("Decision for section:intro")).toBeVisible();
    const decision = page.getByLabel("Keep yours for section:intro");
    await decision.focus(); await expect(decision).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.locator(".oms-source-card").scrollIntoViewIfNeeded();
    await page.screenshot({ path: `test-results/sources-${width}.png` });
  }
});

test("a lost discovery response recovers its receipt without storing the private URL", async ({ page }) => {
  const state = await sourceFixture(page, { loseDiscovery: true });
  await openAddSource(page);
  await page.getByLabel("GitHub repository or skill folder URL").fill("https://github.com/fixture/private-repository-name");
  await page.getByRole("button", { name: "Discover packages", exact: true }).click();
  await expect.poll(() => state.discoveryLost).toBe(true);
  expect(await page.evaluate(() => JSON.stringify({ ...sessionStorage }))).not.toContain("private-repository-name");
  await page.reload();
  await expect(page.getByText("A previous import did not confirm. Your browser sent the request but did not get the answer. Check its result before trying again; checking does not start a new import.")).toBeVisible();
  await page.getByRole("button", { name: "Check result" }).click();
  await expect(page.getByRole("heading", { name: "Discovered skill packages" })).toBeVisible();
  expect(state.requests.filter(row => row.method === "POST" && row.path === "/api/skill-source-discoveries")).toHaveLength(1);
  expect(state.requests.some(row => row.path === "/api/skill-source-operations" && row.method === "GET")).toBe(true);
});

test("a lost GitHub installation response recovers retained selections with the original key", async ({ page }) => {
  const state = await sourceFixture(page, { loseInstall: true });
  await openAddSource(page);
  await page.getByLabel("GitHub repository or skill folder URL").fill("https://github.com/fixture/skills");
  await page.getByRole("button", { name: "Discover packages", exact: true }).click();
  await page.getByLabel("Select orders", { exact: true }).check();
  await page.getByRole("button", { name: "Install selected skills" }).click();
  await expect.poll(() => state.installLost).toBe(true);
  await page.reload();
  await page.getByRole("button", { name: "Check result" }).click();
  await expect(page.getByRole("button", { name: "Check result" })).toHaveCount(0);
  // The recovered result of an install request gets the install counts too.
  await expect(page.getByText("0 installed, 1 already installed, 0 held for review, 0 failed", { exact: true })).toBeVisible();
  const installs = state.requests.filter(row => row.path === "/api/skill-source-installations");
  expect(installs).toHaveLength(2); expect(installs[0].key).toEqual(installs[1].key); expect(installs[0].body).toEqual(installs[1].body);
});

test("a confirmed failed installation allows an identical explicit retry with a fresh key", async ({ page }) => {
  const state = await sourceFixture(page);
  const keys: string[] = [];
  await page.route("**/api/skill-source-installations", async route => {
    if (route.request().method() !== "POST") return route.fallback();
    const key = route.request().headers()["idempotency-key"];
    keys.push(key);
    if (keys.length > 1) return route.fallback();
    state.operations.set(key, { operation_id: key, state: "failed", committed: false,
      outcomes: [{ skill_id: "orders", state: "failed", update_id: null, code: "package_already_owned" }] });
    await route.fulfill({ status: 409, json: { code: "package_already_owned", message: "Package is already installed", operation_id: key },
      headers: { "Access-Control-Allow-Origin": `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}` } });
  });
  await openAddSource(page);
  await page.getByLabel("GitHub repository or skill folder URL").fill("https://github.com/fixture/skills");
  await page.getByRole("button", { name: "Discover packages", exact: true }).click();
  await page.getByLabel("Select orders", { exact: true }).check();
  await page.getByRole("button", { name: "Install selected skills" }).click();
  await expect(page.getByText("Package is already installed", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Check result" })).toHaveCount(0);
  await page.getByRole("button", { name: "Install selected skills" }).click();
  await expect.poll(() => keys.length).toBe(2);
  expect(keys[1]).not.toBe(keys[0]);
  await expect(page.getByText("Operation complete", { exact: true }).first()).toBeVisible();
  expect(state.violations).toEqual([]);
});

test("an install result lists each skill, counts held ones and links to the held skill's review card", async ({ page }) => {
  const state = await sourceFixture(page);
  await page.route("**/api/skill-source-installations", async route => {
    if (route.request().method() !== "POST") return route.fallback();
    const key = route.request().headers()["idempotency-key"];
    const operation: SourceOperation = { operation_id: key, state: "complete", committed: true, outcomes: [
      { skill_id: "expenses-incoming", state: "applied", update_id: null, code: null, detail: null },
      { skill_id: "orders", state: "awaiting_review", update_id: "update-held", code: "safety_review_required", detail: "Held: guide.md, line 4: looks like a phone number" }] };
    state.operations.set(key, operation);
    await route.fulfill({ json: operation, headers: { "Access-Control-Allow-Origin": `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}` } });
  });
  await openAddSource(page);
  await page.getByLabel("GitHub repository or skill folder URL").fill("https://github.com/fixture/skills");
  await page.getByRole("button", { name: "Discover packages", exact: true }).click();
  await page.getByLabel("Select expenses", { exact: true }).check();
  await page.getByLabel("Select orders", { exact: true }).check();
  await page.getByRole("button", { name: "Install selected skills" }).click();
  await expect(page.getByText("1 installed, 1 held for review, 0 failed", { exact: true })).toBeVisible();
  await expect(page.getByText(/line 4: looks like a phone number/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Open in review" })).toHaveAttribute("href", "/import/?github=sources&update=update-held");
  expect(state.violations).toEqual([]);
});

test("a name taken while the form was open is flagged after the server refuses it", async ({ page }) => {
  // Spec 7.2: the form re-reads the skill list after a skill_already_exists
  // refusal, so the names section shows the same prompt as for a known clash.
  const state = await sourceFixture(page);
  await page.route("**/api/skill-source-installations", async route => {
    if (route.request().method() !== "POST") return route.fallback();
    const key = route.request().headers()["idempotency-key"];
    state.skills.push({ ...state.skills[0], id: "orders", name: "Orders" });
    state.operations.set(key, { operation_id: key, state: "failed", committed: false, outcomes: [] });
    await route.fulfill({ status: 409, json: { code: "skill_already_exists", message: "a skill named orders already exists", operation_id: key },
      headers: { "Access-Control-Allow-Origin": `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}` } });
  });
  await openAddSource(page);
  await page.getByLabel("GitHub repository or skill folder URL").fill("https://github.com/fixture/skills");
  await page.getByRole("button", { name: "Discover packages", exact: true }).click();
  await page.getByLabel("Select orders", { exact: true }).check();
  await page.getByRole("button", { name: "Install selected skills" }).click();
  await expect(page.getByText("a skill named orders already exists", { exact: true })).toBeVisible();
  await expect(page.getByText("Give every selected package a unique local name and an available domain.", { exact: false })).toBeVisible();
  await expect(page.locator("details.oms-source-names")).toHaveAttribute("open", "");
  await expect(page.getByRole("button", { name: "Install selected skills" })).toBeDisabled();
  expect(state.violations).toEqual([]);
});

test("a parsed response without an operation state never reports success", async ({ page }) => {
  await sourceFixture(page, { invalidOperation: true });
  await page.goto("/import/?github=sources&source=source-1");
  await page.getByRole("button", { name: "Check source now", exact: true }).click();
  await expect(page.getByText("The server returned an invalid source operation. Recover the original request before retrying.")).toBeVisible();
  await expect(page.getByText("Operation complete", { exact: true })).toHaveCount(0);
});

test("changed approval input cannot replace an unresolved local request with a new key", async ({ page }) => {
  const state = await sourceFixture(page, { loseLocal: true });
  await page.goto("/import/");
  await page.getByLabel("Skill ZIP package").setInputFiles({ name: "bundle.zip", mimeType: "application/zip", buffer: Buffer.from("fixture") });
  await page.getByRole("button", { name: "Preview local package" }).click();
  await page.getByLabel("Destination for bundle").selectOption("expenses");
  await page.getByRole("button", { name: "Apply selected local packages" }).click();
  await expect.poll(() => state.localLost).toBe(true);
  await page.getByLabel("Destination for bundle").selectOption("new");
  await page.getByLabel("Local name for bundle").fill("New local guide");
  await page.getByRole("button", { name: "Apply selected local packages" }).click();
  await expect(page.getByText("A previous attempt needs recovery. Resolve it below before submitting your current input.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Check result" })).toBeEnabled();
  expect(state.requests.filter(row => row.path === "/api/skill-local-imports")).toHaveLength(1);
});

test("a new database at the same server URL discards pending mutations but keeps the browser session", async ({ page }) => {
  const state = await sourceFixture(page, { loseLocal: true });
  await page.goto("/import/");
  await page.getByLabel("Skill ZIP package").setInputFiles({ name: "bundle.zip", mimeType: "application/zip", buffer: Buffer.from("fixture") });
  await page.getByRole("button", { name: "Preview local package" }).click();
  await page.getByLabel("Destination for bundle").selectOption("expenses");
  await page.getByRole("button", { name: "Apply selected local packages" }).click();
  await expect.poll(() => state.localLost).toBe(true);
  await expect(page.getByRole("button", { name: "Check result" })).toBeVisible();
  const originalSession = await page.evaluate(() => sessionStorage.getItem("oms-community-source-session"));
  state.workspaceId = "22222222-2222-4222-8222-222222222222";
  state.operations.clear();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Bring your existing skills" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("oms.source-requests.v1"))).toBeNull();
  await expect(page.getByRole("button", { name: "Check result" })).toHaveCount(0);
  expect(await page.evaluate(() => sessionStorage.getItem("oms-community-source-session"))).toEqual(originalSession);
  expect(state.requests.filter(row => row.path === "/api/skill-local-imports")).toHaveLength(1);
  expect(state.requests.filter(row => row.path === "/api/skill-source-workspace")).toHaveLength(2);
});

test("missing or malformed workspace identity prevents recovery and source mutations", async ({ page }) => {
  const state = await sourceFixture(page);
  state.workspaceAvailable = false;
  await page.goto("/import/?github=sources");
  await expect(page.getByText("Workspace identity is unavailable")).toBeVisible();
  expect(state.requests.every(row => ["/api/capabilities", "/api/skill-source-workspace"].includes(row.path))).toBe(true);
  state.workspaceAvailable = true; state.workspaceId = "";
  await page.reload();
  await expect(page.getByText("The server did not provide a valid source workspace identity.")).toBeVisible();
  expect(state.requests.filter(row => row.method === "POST")).toHaveLength(0);
});

test("changing the tracked folder uses relink with the current source and generation guards", async ({ page }) => {
  const state = await sourceFixture(page);
  await page.goto("/import/?github=sources&source=source-1");
  await page.getByRole("button", { name: "Change tracked ref or folder" }).click();
  await page.getByText("Advanced", { exact: true }).click();
  await page.getByLabel("Skill folder (optional)").fill("orders");
  await page.getByRole("button", { name: "Discover packages", exact: true }).click();
  await page.getByRole("button", { name: "Confirm folder relink", exact: true }).click();
  await expect.poll(() => state.binding.binding?.package_path).toBe("orders");
  const request = state.requests.find(row => row.path.endsWith("/source-binding/relink"));
  expect(request?.body).toMatchObject({ source_id: "source-1", package_path: "orders", expected_content_generation: 1, expected_binding_generation: 1, ref: { package_path: "orders" } });
  expect(state.requests.filter(row => row.path.endsWith("/retarget") || row.path === "/api/skill-sources" && row.method === "POST")).toHaveLength(0);
});

test("checking 101 bindings collects both pages and claims the source throttle once", async ({ page }) => {
  const state = await sourceFixture(page, { bindingCount: 101 });
  await page.goto("/import/?github=sources&source=source-1");
  await page.getByRole("button", { name: "Check source now", exact: true }).click();
  await expect.poll(() => state.checkClaims).toBeGreaterThan(0);
  await expect(page.getByText("Processing source request…", { exact: true })).toHaveCount(0);
  const checks = state.requests.filter(row => row.path.endsWith("/check") && row.method === "POST");
  expect(checks).toHaveLength(1); expect(state.checkClaims).toBe(1);
  const body = checks[0].body as { skill_ids: string[]; expected_source_generation: number };
  expect(body.skill_ids).toHaveLength(101); expect(new Set(body.skill_ids).size).toBe(101);
  expect(body.skill_ids).toContain("expenses"); expect(body.skill_ids).toContain("linked-100");
  expect(state.requests.some(row => row.path === "/api/skill-sources/source-1" && row.query === "?cursor=100")).toBe(true);
});

test("explicit recovery rechecks workspace identity while the tab stays open", async ({ page }) => {
  const state = await sourceFixture(page, { loseLocal: true });
  await page.goto("/import/");
  await page.getByLabel("Skill ZIP package").setInputFiles({ name: "bundle.zip", mimeType: "application/zip", buffer: Buffer.from("fixture") });
  await page.getByRole("button", { name: "Preview local package" }).click();
  await page.getByLabel("Destination for bundle").selectOption("expenses");
  await page.getByRole("button", { name: "Apply selected local packages" }).click();
  await expect(page.getByRole("button", { name: "Check result" })).toBeVisible();
  // Let the page finish its own refreshes first: a request still in flight would meet the new workspace and reset the view early.
  await page.waitForLoadState("networkidle");
  state.workspaceId = "22222222-2222-4222-8222-222222222222"; state.operations.clear();
  await page.getByRole("button", { name: "Check result" }).click();
  await expect(page.getByText(/The workspace changed\. Earlier source requests and views were reset/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Check result" })).toHaveCount(0);
  expect(state.requests.filter(row => row.path === "/api/skill-local-imports")).toHaveLength(1);
  expect(await page.evaluate(() => sessionStorage.getItem("oms.source-requests.v1"))).toBeNull();
});

test("workspace guard rejection resets the view before a stale check can reserve an operation", async ({ page }) => {
  const state = await sourceFixture(page);
  await page.goto("/import/?github=sources&source=source-1");
  await expect(page.getByRole("button", { name: "Check source now", exact: true })).toBeEnabled();
  const originalWorkspace = state.workspaceId;
  state.workspaceId = "22222222-2222-4222-8222-222222222222";
  await page.getByRole("button", { name: "Check source now", exact: true }).click();
  await expect(page.getByText(/The workspace changed\. Earlier source requests and views were reset/)).toBeVisible();
  expect(state.checkClaims).toBe(0); expect(state.operations.size).toBe(0);
  const attempted = state.requests.filter(row => row.path.endsWith("/check"));
  expect(attempted).toHaveLength(1); expect(attempted[0].workspace).toBe(originalWorkspace);
  await expect.poll(() => state.requests.some(row => row.path === "/api/skill-sources/source-1" && row.workspace === state.workspaceId)).toBe(true);
  expect(state.requests.filter(row => row.path === "/api/skill-source-workspace").every(row => row.workspace === undefined)).toBe(true);
  expect(await page.evaluate(() => sessionStorage.getItem("oms.source-requests.v1"))).toBeNull();
});

test("confirmed repository relocation changes only the endpoint before the next Check", async ({ page }) => {
  const state = await sourceFixture(page);
  const moves: Record<string, unknown>[] = [];
  await page.route("**/api/skill-sources/source-1/relocate", async route => {
    const body = route.request().postDataJSON(); moves.push(body);
    expect(route.request().headers()["idempotency-key"]).toBeTruthy();
    Object.assign(state.source, { canonical_url: body.destination_url, generation: 2 });
    await route.fulfill({ json: { operation_id: "moved", source_id: "source-1", state: "complete", committed: true, outcomes: [] }, headers: { "Access-Control-Allow-Origin": `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}` } });
  });
  await page.goto("/import/?github=sources&source=source-1");
  await page.getByText("Change repository location", { exact: true }).click();
  await page.getByLabel("New GitHub repository URL").fill("https://github.com/fixture/moved");
  await page.getByRole("button", { name: "Change repository URL" }).click();
  const dialog = page.getByRole("alertdialog", { name: "Change repository URL confirmation" });
  await expect(dialog.getByText("Warning: This action will move the source for all skills that are part of this repository to https://github.com/fixture/moved. Do you still want to move it?")).toBeVisible();
  await dialog.getByText("Affected skills (1)").click();
  await expect(dialog.getByRole("listitem")).toHaveText(["Expense review"]);
  await dialog.getByRole("button", { name: "No" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Change repository URL" })).toBeFocused();
  expect(moves).toHaveLength(0);
  await page.getByRole("button", { name: "Change repository URL" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Yes" }).click();
  await expect.poll(() => moves.length).toBe(1);
  await expect(page.getByRole("heading", { name: "https://github.com/fixture/moved" })).toBeVisible();
  expect(moves[0]).toEqual({ destination_url: "https://github.com/fixture/moved", expected_source_generation: 1,
    expected_generations: [{ skill_id: "expenses", content: 1, binding: 1 }] });
  expect(state.binding.binding!.ref.commit).toBe("a".repeat(40));
  expect(state.requests.filter(row => row.method === "POST")).toEqual([]);
});
