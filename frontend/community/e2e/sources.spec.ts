import { test, expect, type Page } from "@playwright/test";
import { sourceFixture } from "./source-fixture";

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

test("merged choices survive save revisions and a lost Apply response recovers the same request", async ({ page }) => {
  const state = await sourceFixture(page, { loseApply: true });
  await page.goto("/import/?github=sources&update=update-1");
  await page.getByLabel("Decision for section:intro").selectOption("merged_text");
  await page.getByLabel("Merged text for section:intro").fill("Retain receipts and verify each order.");
  await expect(page.getByRole("button", { name: "Apply whole skill" })).toBeDisabled();
  await page.getByRole("button", { name: "Save choices", exact: true }).click();
  await expect(page.getByLabel("Merged text for section:intro")).toHaveValue("Retain receipts and verify each order.");
  await expect(page.getByRole("button", { name: "Apply whole skill" })).toBeEnabled();
  await page.getByRole("button", { name: "Apply whole skill" }).click();
  await expect.poll(() => state.decisions).toEqual(["apply"]);
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
  await page.getByRole("button", { name: "Keep all current OMS content and adopt this revision" }).click();
  await expect.poll(() => state.decisions).toEqual(["skip", "adopt"]);
  expect(state.requests.filter(row => row.path.endsWith("/apply"))).toHaveLength(0);
});

test("removal consent and individual Keep are explicit whole-skill decisions", async ({ page }) => {
  const state = await sourceFixture(page, { removal: true });
  await page.goto("/import/?github=sources&update=update-1");
  await expect(page.getByRole("button", { name: "Apply whole skill" })).toBeDisabled();
  await page.getByLabel("Consent to remove group 1").check();
  await expect(page.getByRole("button", { name: "Apply whole skill" })).toBeEnabled();
  await page.getByLabel("Keep section:intro", { exact: true }).check();
  await expect(page.getByRole("button", { name: "Apply whole skill" })).toBeDisabled();
  await page.getByRole("button", { name: "Save choices", exact: true }).click();
  await expect(page.getByRole("button", { name: "Apply whole skill" })).toBeEnabled();
  await page.getByRole("button", { name: "Apply whole skill" }).click();
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
  await expect(page.getByText("Retained but excluded from publication: reserved output")).toBeVisible();
  await expect(page.locator(".oms-source-files pre")).toHaveText(Array(3).fill("<script>window.bad = true</script>"));
  expect(await page.evaluate(() => "bad" in window)).toBe(false);
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download incoming", exact: true }).click();
  expect((await download).suggestedFilename()).toBe("guide.md");
  expect(state.requests.some(row => row.path.endsWith("/files"))).toBe(true);
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
    const decision = page.getByLabel("Decision for section:intro");
    await expect(decision).toBeVisible();
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
