import { test, expect, type Page } from "@playwright/test";
import { allowedRequest } from "./route-contract.mjs";
import type { ImportReviewItem, LocalConstraint } from "../lib/local-review";
import type { Decision, ReviewItem } from "@inogen/oms-client";

const API = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || "4317"}`;
const UI = `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}`;
const caps = {
  edition: "community", api_contract_version: "1.1", schema_version: 1,
  manual_learning: true, semantic_compilation: false, multi_user_identity: false,
  team_scoping: false, personal_mutes: false, contributor_portal: false, redaction_vault: false,
  model_settings: false, scheduled_publish: false, managed_publish: false,
  graph_query_console: false, advanced_review: false, usage_analytics: false,
};
const skill = { id: "expenses", name: "Expense review", description: "Make expense checks repeatable.", domain: "operations", body: "# Expense review\n\nKeep the original introduction.", sections: [{ id: "intro", heading: "Introduction", text: "Keep the original introduction.", editable: true }], artefacts: [{ path: "references/policy.md" }, { path: "scripts/check.sh" }], versions: [{ id: "version-1", at: "2026-09-15T10:00:00Z", cause: "upload_import", actor_person_id: null, detail: "Original package", revision: "rev-1" }] };
const correction: ReviewItem = { txn_id: "correction-1", text: "Verify the receipt before submitting an expense.", signal_type: "explicit_correction", source_ref: "local:manual", state: "awaiting_manual_review", skill_hint: "expnse review", candidate_skill_ids: [], exact_matches: [], skill_suggestions: [{ id: "expenses", name: "Expense review", score: 0.8, reason: "Name or ID closely resembles the supplied skill hint." }], similar_matches: [] };

async function fixture(page: Page, options: { empty?: boolean; manual?: boolean; held?: boolean; exact?: boolean; similar?: boolean } = {}) {
  const existingWording = options.similar ? "Check original receipts for every expense claim." : correction.text;
  const state = {
    violations: [] as string[], requests: [] as string[], skills: options.empty ? [] : [structuredClone(skill)],
    inbox: [{ ...correction, state: options.held ? "held_safety" : correction.state, held_reason: options.held ? "Injection warning requires review" : undefined,
      exact_matches: options.exact ? [{ id: "existing", body: existingWording }] : [],
      similar_matches: options.exact || options.similar ? [{ id: "existing", body: existingWording, score: options.exact ? 1 : 0.6, reason: options.exact ? "Same wording after normalising case and spacing." : "Shares words: expense, receipt. Compare the meaning before reinforcing.", exact: Boolean(options.exact) }] : [] }] as ReviewItem[],
    rules: options.exact || options.similar ? [{ id: "existing", body: existingWording, status: "active", corroboration_count: 1, transaction_ids: ["earlier-correction"] }] : [] as { id: string; body: string; status: string; corroboration_count: number; transaction_ids: string[] }[],
    constraints: [] as LocalConstraint[], importReviews: [] as ImportReviewItem[],
    importDecisions: [] as { id: string; action: string; body?: string }[],
    settings: { body_budget: 200, skill_history_keep: 200, root_instruction_files: ["AGENTS.md", "CLAUDE.md"], publication_target: "local", publication_folder: "", publish_root: "/tmp/community-output", publish_base: "/tmp/community-output", publish_host_path: "/tmp/community-output", publish_host_root: "/tmp/community-output", publish_in_container: false, publication_ready: true, public_url: "http://localhost:4317", public_url_is_loopback: true }, publishes: 0,
    documentRevision: 0, decisions: [] as Decision[], graphPages: [] as number[], deletions: [] as string[],
  };
  const rendered = () => `${state.skills[0].body}\n\n${state.rules.filter(rule => rule.status === "active").map(rule => `* ${rule.body}`).join("\n")}`;
  const document = () => ({ skill_id: "expenses", revision: `document-${state.documentRevision}`, path: "expenses/SKILL.md", parts: [
    { anchor: "title", kind: "title", source_id: "expenses", editable: true, edit_text: state.skills[0].name, lines: [`# ${state.skills[0].name}`] },
    { anchor: "block:intro", kind: "prose", source_id: "intro", editable: true, edit_text: state.skills[0].sections[0].text, lines: [state.skills[0].sections[0].text] },
    ...state.rules.filter(rule => rule.status === "active").map(rule => ({ anchor: `rule:${rule.id}`, kind: "rule", source_id: rule.id, editable: true, edit_text: rule.body, lines: [`* ${rule.body}`] })),
  ] });
  await page.route("**/*", async (route) => {
    const request = route.request(); const url = new URL(request.url()); const method = request.method();
    if (url.origin === UI && !url.pathname.startsWith("/api/")) return route.continue();
    if (url.origin !== API) { state.violations.push(`Unexpected origin: ${method} ${url.origin}${url.pathname}`); return route.abort(); }
    const path = url.pathname; state.requests.push(`${method} ${path}`);
    const reply = (data: unknown, status = 200) => route.fulfill({ status, json: data, headers: { "Access-Control-Allow-Origin": UI, "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET, POST, PATCH, PUT, DELETE, OPTIONS" } });
    if (method === "OPTIONS") return reply({});
    if (!allowedRequest(method, path)) { state.violations.push(`${method} ${path}`); return reply({ detail: "Forbidden request in Community browser test" }, 418); }
    const body = request.headers()["content-type"]?.includes("application/json") ? request.postDataJSON() : {};
    if (path === "/api/capabilities") return reply({ ...caps, manual_learning: options.manual ?? true });
    if (path === "/api/skill-changes") return reply(state.skills.flatMap((item) => item.versions.map((version) => ({ ...version, skill_id: item.id, skill_name: item.name }))));
    if (path === "/api/health") return reply({ status: "healthy", workspace: "local", storage: "connected" });
    if (path === "/api/ingest") { const transactionId = body.transaction_id ?? "browser-correction"; state.inbox.push({ ...correction, txn_id: transactionId, text: body.correction, held_reason: undefined, exact_matches: [] }); return reply({ transaction_id: transactionId, state: "awaiting_manual_review" }); }
    if (path === "/api/import") { state.skills = [structuredClone(skill)]; return reply({ skills: 1, artefacts: 2 }); }
    if (path === "/api/import-review") return reply(state.importReviews);
    if (path.startsWith("/api/import-review/") && path.endsWith("/decision")) {
      const id = decodeURIComponent(path.split("/")[3]);
      state.importDecisions.push({ id, ...body });
      state.importReviews = state.importReviews.filter((item) => item.id !== id);
      return reply({ resolved: true });
    }
    if (path === "/api/skills" && method === "GET") return reply(state.skills);
    if (path === "/api/skills" && method === "POST") { const created = { ...skill, ...body, id: "new-skill" }; state.skills.push(created); return reply(created); }
    if (path === "/api/skills/expenses" && method === "GET") return reply({ ...state.skills[0], body: rendered(), rules: state.rules });
    if (path === "/api/skills/expenses" && method === "PATCH") { Object.assign(state.skills[0], body); return reply(state.skills[0]); }
    if (path === "/api/skills/expenses" && method === "DELETE") { const found = state.skills.find(item => item.id === "expenses")!; state.skills = state.skills.filter(item => item.id !== found.id); state.deletions.push(found.id); return reply({ deleted: found.id, name: found.name, rules_detached: state.rules.length }); }
    if (path === "/api/skills/expenses/document" && method === "GET") return reply(document());
    if (path === "/api/skills/expenses/document" && method === "PUT") {
      if (body.revision !== document().revision) return reply({ detail: "This document has changed. Reload it before saving your edits." }, 409);
      for (const part of body.parts as { anchor: string; text: string }[]) {
        if (part.anchor === "title") state.skills[0].name = part.text;
        else if (part.anchor === "block:intro") state.skills[0].sections[0].text = part.text;
        else {
          const rule = state.rules.find(rule => `rule:${rule.id}` === part.anchor && rule.status === "active");
          if (!rule) return reply({ detail: "This document part cannot be edited." }, 422);
          rule.body = part.text;
        }
      }
      state.skills[0].body = `# ${state.skills[0].name}\n\n${state.skills[0].sections[0].text}`;
      state.documentRevision++;
      return reply(document());
    }
    if (path === "/api/skills/expenses/files") {
      const file = url.searchParams.get("path");
      const text = file === "references/policy.md" ? "# Expense policy\n\nRetain original receipts." : file === "scripts/check.sh" ? "#!/bin/sh\nprintf 'checked\\n'\n" : null;
      return text === null ? reply({ detail: "File not found" }, 404) : reply({ path: file, text, size: Buffer.byteLength(text) });
    }
    if (path === "/api/skills/expenses/versions") return reply(state.skills[0].versions);
    if (path === "/api/skills/expenses/versions/version-1/compare") return reply({ version: skill.versions[0], revision: document().revision, markdown: skill.body, rows: [{ anchor: "block:intro", kind: "prose", state: state.skills[0].sections[0].text === skill.sections[0].text ? "same" : "changed", old_text: skill.sections[0].text, new_text: state.skills[0].sections[0].text, rule_id: null, restorable: true }] });
    if (path === "/api/review") return reply(state.inbox);
    if (path.endsWith("/decision")) {
      state.decisions.push(body);
      if (body.action === "release_safety") { state.inbox[0].state = "awaiting_manual_review"; state.inbox[0].held_reason = undefined; return reply({ state: "awaiting_manual_review" }); }
      if (body.action === "create") state.rules.push({ id: "rule-correction-1", body: body.body, status: "active", corroboration_count: 1, transaction_ids: ["correction-1"] });
      if (body.action === "reinforce") state.rules[0].corroboration_count++;
      state.inbox = []; return reply({ state: body.action === "reject" ? "rejected" : "applied" });
    }
    if (path === "/api/rules") return reply(state.rules);
    if (path.startsWith("/api/rules/")) { state.rules[0].status = body.action === "retract" ? "retracted" : "active"; return reply(state.rules[0]); }
    if (path === "/api/constraints" && method === "GET") return reply(state.constraints);
    if (path === "/api/constraints" && method === "POST") { const constraint: LocalConstraint = { id: "constraint-1", body: body.body, source: "console", status: "active" }; state.constraints.push(constraint); return reply(constraint); }
    if (path.startsWith("/api/constraints/") && method === "PATCH") {
      const constraint = state.constraints.find((item) => item.id === decodeURIComponent(path.split("/")[3]));
      if (!constraint || constraint.source !== "console") return reply({ detail: "Edit the source file and import it again." }, 409);
      Object.assign(constraint, body); return reply(constraint);
    }
    if (path === "/api/publish/preview") return reply({ body: rendered() });
    if (path === "/api/publish") { state.publishes++; return reply({ output: "/tmp/community-output", skills: state.skills.length }); }
    if (path === "/api/graph") {
      if (url.searchParams.get("skill_id") === "expenses") return reply({ nodes: [{ id: "expenses", name: "Expense review", kind: "skill" }], edges: [] });
      return reply({ nodes: [...state.skills.map((item) => ({ id: item.id, name: item.name, kind: "skill" })), ...(state.skills.length ? [{ id: "graph-rule", name: "Verify the receipt", kind: "rule" }] : [])], edges: state.skills.length ? [{ source: "graph-rule", target: "expenses", type: "BELONGS_TO" }] : [] });
    }
    if (path === "/api/graph/nodes/expenses/neighbours") {
      const offset = Number(url.searchParams.get("offset") || 0); state.graphPages.push(offset);
      const node = offset === 0 ? { id: "graph-rule", name: "Verify the receipt", kind: "rule" } : { id: "graph-rule-2", name: "Check approval", kind: "rule" };
      return reply({ nodes: [{ id: "expenses", name: "Expense review", kind: "skill" }, node], edges: [{ source: node.id, target: "expenses", type: "BELONGS_TO" }], total: 2, next_offset: offset === 0 ? 1 : null });
    }
    if (path === "/api/graph/nodes/graph-rule") return reply({ id: "graph-rule", body: "Verify the receipt", kind: "rule", properties: { status: "active" } });
    if (path === "/api/graph/nodes/expenses") return reply({ id: "expenses", name: "Expense review", kind: "Skill", properties: { source: "import" } });
    if (path === "/api/settings" && method === "GET") return reply(state.settings);
    if (path === "/api/settings" && method === "PATCH") { Object.assign(state.settings, body); return reply(state.settings); }
    state.violations.push(`${method} ${path} not covered by fixture`); return reply({}, 500);
  });
  return state;
}

test("every Community page loads without calling an unsupported endpoint", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workspace overview" })).toBeVisible();
  await expect(page.getByText("Skills in your library")).toBeVisible();
  await page.screenshot({ path: "test-results/community-overview.png", fullPage: true });
  for (const [label, heading] of [["Skills", "Your skills library"], ["Correction inbox", "Review corrections"], ["Constraints", "Constraints for every task"], ["Import", "Bring your existing skills"], ["Preview & publish", "Preview, then publish"], ["Graph", "Skill graph"], ["Settings", "Workspace settings"]]) {
    await page.getByRole("navigation").getByRole("link", { name: label, exact: true }).click();
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  }
  await expect(page.getByRole("navigation")).not.toContainText(/Models|Redactions|Usage|Query console/);
  await expect(page.getByRole("navigation").getByRole("button", { name: "People and teams, Paid feature preview" })).toHaveCount(1);
  await expect(page.getByRole("navigation").getByRole("link", { name: /People|Teams/ })).toHaveCount(0);
  expect(state.violations).toEqual([]);
  expect(state.requests.filter((path) => path === "GET /api/capabilities")).toHaveLength(1);
});

test("paid feature preview preserves drafts, traps focus and makes no additional requests", async ({ page }) => {
  const state = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.goto("/skills?skill=expenses");
  await page.getByRole("button", { name: "Edit details", exact: true }).click();
  const draft = page.getByRole("textbox", { name: "Description", exact: true });
  await draft.fill("Retain this unsaved Community draft.");
  const url = page.url();
  const requests = [...state.requests];
  const preview = page.getByRole("button", { name: "People and teams, Paid feature preview" });
  await expect(preview).toBeEnabled();
  await expect(preview).toHaveAttribute("aria-haspopup", "dialog");
  await expect(preview.locator("svg")).toBeVisible();
  await expect(preview).toContainText("Paid");
  await page.screenshot({ path: "test-results/community-paid-nav-desktop.png", fullPage: true });
  await preview.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "People and teams", exact: true });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", { name: "People and teams", exact: true })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "Back to workspace", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "Close paid features preview" })).toBeFocused();
  await page.screenshot({ path: "test-results/community-paid-preview-desktop.png", fullPage: false });
  // The preview is local content, including its comparison. It still works
  // if the API or external network becomes unavailable after opening the app.
  await page.context().setOffline(true);
  await dialog.getByRole("button", { name: "Compare editions", exact: true }).click();
  const comparison = page.getByRole("dialog", { name: "Compare editions", exact: true });
  await expect(comparison.getByRole("heading", { name: "Community", exact: true })).toBeVisible();
  await expect(comparison.getByRole("heading", { name: "Pro", exact: true })).toBeVisible();
  await expect(comparison.getByRole("heading", { name: "Enterprise", exact: true })).toBeVisible();
  const pricing = comparison.getByRole("link", { name: "Full comparison and pricing on inogen.ai (opens in a new tab)", exact: true });
  await expect(pricing).toHaveAttribute("href", "https://www.inogen.ai/pricing");
  await expect(pricing).toHaveAttribute("target", "_blank");
  await expect(pricing).toHaveAttribute("rel", "noopener noreferrer");
  await page.screenshot({ path: "test-results/community-editions-desktop.png", fullPage: false });
  // The external links and scrollable details participate in the focus loop.
  await page.keyboard.press("Shift+Tab");
  await expect(pricing).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(comparison.getByRole("button", { name: "Close paid features preview" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(comparison.getByRole("region", { name: "Edition comparison details", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(comparison.getByRole("link", { name: "Discuss Pro (opens in a new tab)", exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(comparison.getByRole("link", { name: "Discuss Enterprise (opens in a new tab)", exact: true })).toBeFocused();
  await page.context().setOffline(false);
  await page.context().route("https://www.inogen.ai/pricing", route => route.fulfill({ contentType: "text/html", body: "<title>Pricing destination</title>" }));
  const popupPromise = page.waitForEvent("popup");
  await pricing.click();
  const popup = await popupPromise;
  await expect(popup).toHaveURL("https://www.inogen.ai/pricing");
  await popup.close();
  await expect(comparison).toBeVisible();
  await comparison.getByRole("button", { name: "People and teams", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(preview).toBeFocused();
  await expect(draft).toHaveValue("Retain this unsaved Community draft.");
  expect(page.url()).toBe(url);
  expect(state.requests).toEqual(requests);
  expect(state.violations).toEqual([]);
  await page.context().setOffline(false);
  await page.getByRole("button", { name: "Collapse sidebar" }).click();
  await expect(preview).toHaveAttribute("title", "People and teams, Paid feature preview");
  await preview.click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Back to workspace" }).click();
  await expect(preview).toBeFocused();
});

test("correction automation preview explains the full workflow offline and retains the review draft", async ({ page }) => {
  const state = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/inbox");
  const hint = page.getByRole("complementary", { name: "Paid correction automation" });
  await expect(hint).toContainText("apply eligible updates automatically");
  await page.getByRole("button", { name: `Review correction: ${correction.text}` }).click();
  await page.getByRole("tab", { name: "Add or confirm a rule", exact: true }).click();
  const wording = page.getByRole("textbox", { name: "Proposed rule wording", exact: true });
  await wording.fill("Keep the unsaved correction wording.");
  const url = page.url();
  const requests = [...state.requests];
  await page.screenshot({ path: "test-results/community-automation-inbox.png", fullPage: true });
  const trigger = hint.getByRole("button", { name: "Explore automatic processing" });
  await page.context().setOffline(true);
  await trigger.focus();
  await page.keyboard.press("Enter");
  const preview = page.getByRole("dialog", { name: "Automatic correction processing", exact: true });
  await expect(preview).toBeVisible();
  for (const name of ["Distil a reusable rule", "Find the relevant skills", "Reconcile and revise guidance", "Apply updates or request review"]) {
    await expect(preview.getByRole("heading", { name, exact: true })).toBeVisible();
  }
  await expect(preview).toContainText("confidence thresholds and safety checks");
  await expect(preview).toContainText("valid licence and a configured model provider");
  await page.screenshot({ path: "test-results/community-automation-preview.png" });
  await preview.getByRole("button", { name: "Compare editions", exact: true }).click();
  const comparison = page.getByRole("dialog", { name: "Compare editions", exact: true });
  await expect(comparison).toContainText("Distil corrections into reusable rules");
  await comparison.getByRole("button", { name: "Automatic correction processing", exact: true }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await expect(wording).toHaveValue("Keep the unsaved correction wording.");
  expect(page.url()).toBe(url);
  expect(state.requests).toEqual(requests);
  expect(state.violations).toEqual([]);
});

test("automation hint dismissal persists and Settings retains comparison and restores the tip", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/inbox");
  await page.getByRole("button", { name: "Dismiss paid automation tip" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Review corrections" })).toBeFocused();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Review corrections" })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Paid correction automation" })).toHaveCount(0);
  await page.getByRole("navigation").getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: "About your edition" })).toBeVisible();
  const budget = page.getByRole("spinbutton", { name: "Maximum rules in the main skill file" });
  await budget.fill("123");
  const requests = [...state.requests];
  const compare = page.getByRole("button", { name: "Compare editions", exact: true });
  await compare.click();
  await expect(page.getByRole("dialog", { name: "Compare editions" })).toBeVisible();
  await page.getByRole("button", { name: "Back to workspace" }).click();
  await expect(compare).toBeFocused();
  await expect(budget).toHaveValue("123");
  expect(state.requests).toEqual(requests);
  await page.getByRole("button", { name: "Show the correction inbox tip again" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Explore automatic processing" })).toBeFocused();
  await page.getByRole("navigation").getByRole("link", { name: "Correction inbox", exact: true }).click();
  await expect(page.getByRole("complementary", { name: "Paid correction automation" })).toBeVisible();
  expect(state.violations).toEqual([]);
});

test("automation preview fits phones and tip dismissal works with storage blocked", async ({ page }) => {
  await page.addInitScript(() => {
    Storage.prototype.setItem = () => { throw new DOMException("Storage blocked", "SecurityError"); };
    Storage.prototype.getItem = () => { throw new DOMException("Storage blocked", "SecurityError"); };
  });
  const state = await fixture(page);
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/inbox");
    const trigger = page.getByRole("button", { name: "Explore automatic processing" });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "Automatic correction processing" });
    await expect(dialog).toBeVisible();
    expect(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    await page.screenshot({ path: `test-results/community-automation-${width}.png` });
    await dialog.getByRole("button", { name: "Back to workspace" }).click();
    await expect(trigger).toBeFocused();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.getByRole("button", { name: "Dismiss paid automation tip" }).click();
    await expect(trigger).toHaveCount(0);
  }
  expect(state.violations).toEqual([]);
});

test("paid feature preview is accessible on phones and restores mobile navigation focus", async ({ page }) => {
  const state = await fixture(page);
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/skills?skill=expenses");
    await page.getByRole("tab", { name: "Edit document", exact: true }).click();
    const text = page.locator('[data-anchor="block:intro"]').getByRole("textbox");
    await text.fill("Keep the mobile document draft.");
    const menu = page.getByRole("button", { name: "Pages, currently Skills" });
    await menu.click();
    const nav = page.getByRole("navigation", { name: "Main navigation" });
    const preview = nav.getByRole("button", { name: "People and teams, Paid feature preview" });
    await expect(preview.locator("svg")).toBeVisible();
    await page.screenshot({ path: `test-results/community-paid-nav-${width}.png`, fullPage: true });
    await preview.click();
    const dialog = page.getByRole("dialog", { name: "People and teams", exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("heading", { name: "People and teams", exact: true })).toBeFocused();
    await expect.poll(() => dialog.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    await page.screenshot({ path: `test-results/community-paid-preview-${width}.png`, fullPage: false });
    await dialog.getByRole("button", { name: "Compare editions", exact: true }).click();
    const comparison = page.getByRole("dialog", { name: "Compare editions", exact: true });
    await expect(comparison).toBeVisible();
    await expect.poll(() => comparison.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    const details = comparison.getByRole("region", { name: "Edition comparison details", exact: true });
    await expect.poll(() => details.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
    await expect(comparison.getByRole("link", { name: "Full comparison and pricing on inogen.ai (opens in a new tab)", exact: true })).toBeInViewport();
    await page.screenshot({ path: `test-results/community-editions-${width}.png`, fullPage: false });
    for (const edition of ["Community", "Pro", "Enterprise"]) {
      const plan = comparison.getByRole("region", { name: edition, exact: true });
      await plan.scrollIntoViewIfNeeded();
      await expect(plan.getByRole("heading", { name: edition, exact: true })).toBeInViewport();
      await expect.poll(() => plan.evaluate(node => node.scrollWidth <= node.clientWidth)).toBe(true);
      await page.screenshot({ path: `test-results/community-editions-${width}-${edition.toLowerCase()}.png`, fullPage: false });
    }
    await page.keyboard.press("Escape");
    await expect(menu).toBeFocused();
    await expect(text).toHaveValue("Keep the mobile document draft.");
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  }
  expect(state.violations).toEqual([]);
});

test("skill deletion keeps drafts on cancel or failure, disables repeat requests and refreshes the library", async ({ page }) => {
  const state = await fixture(page);
  state.skills.push({ ...structuredClone(skill), id: "release-guidance", name: "Release guidance", domain: "engineering" });
  let fail = true;
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/skills/expenses", async route => {
    if (route.request().method() !== "DELETE" || !fail) return route.fallback();
    await pending;
    return route.fulfill({ status: 503, json: { detail: "Delete temporarily unavailable" }, headers: { "Access-Control-Allow-Origin": UI } });
  });
  await page.goto("/skills?skill=expenses");
  await page.getByRole("button", { name: "All skills", exact: true }).click();
  await page.getByRole("combobox", { name: "Filter by domain" }).selectOption("operations");
  await expect(page.getByRole("button", { name: /^Release guidance/ })).toHaveCount(0);
  await page.getByRole("button", { name: /^Expense review/ }).click();
  await page.getByRole("button", { name: "Edit details", exact: true }).click();
  await page.getByRole("textbox", { name: "Description", exact: true }).fill("Keep this unsaved draft.");
  await page.getByRole("button", { name: "Delete skill", exact: true }).click();
  const confirmation = page.getByRole("region", { name: "Delete skill confirmation" });
  const input = confirmation.getByLabel("Type the skill name to confirm deletion");
  await expect(input).toBeFocused();
  await expect(input).toHaveAccessibleDescription(/permanently removes.*unsaved edits.*discarded/);
  await expect(confirmation).toContainText("Your unsaved edits to this skill will also be discarded.");
  await input.fill("wrong name");
  await expect(confirmation.getByRole("button", { name: "Delete permanently" })).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(confirmation).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Delete skill", exact: true })).toBeFocused();
  await expect(page.getByRole("textbox", { name: "Description", exact: true })).toHaveValue("Keep this unsaved draft.");
  expect(state.deletions).toEqual([]);
  await page.getByRole("button", { name: "Delete skill", exact: true }).click();
  await input.fill("Expense review");
  await page.screenshot({ path: "test-results/community-delete-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/community-delete-mobile.png", fullPage: true });
  await confirmation.getByRole("button", { name: "Delete permanently" }).click();
  await expect(confirmation.getByRole("button", { name: "Deleting…" })).toBeDisabled();
  await expect(confirmation.getByRole("button", { name: "Cancel", exact: true })).toBeDisabled();
  release();
  await expect(page.getByRole("alert").filter({ hasText: "Delete temporarily unavailable" })).toBeVisible();
  await expect(input).toHaveValue("Expense review");
  await expect(page.getByRole("textbox", { name: "Description", exact: true })).toHaveValue("Keep this unsaved draft.");
  fail = false;
  await confirmation.getByRole("button", { name: "Delete permanently" }).click();
  await expect(page.getByRole("heading", { name: "Your skills library", exact: true })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "deleted." })).toContainText("Expense review");
  await expect(page.getByRole("link", { name: "Publish updates", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /^Expense review/ })).toHaveCount(0);
  await expect(page.getByRole("combobox", { name: "Filter by domain" })).toHaveValue("");
  await expect(page.getByRole("button", { name: /^Release guidance/ })).toBeVisible();
  await expect(page.getByText(/Unsaved edits in/)).toHaveCount(0);
  expect(new URL(page.url()).searchParams.has("skill")).toBe(false);
  expect(state.deletions).toEqual(["expenses"]);
  expect(state.violations).toEqual([]);
});

test("ordinary publication shows the host install command and keeps it available after reload", async ({ page }) => {
  const state = await fixture(page);
  Object.assign(state.settings, { publish_in_container: true, publish_root: "/published/team skills",
    publish_host_root: "/home/test/Chris's skills", publish_host_path: "/home/test/Chris's skills/team skills", publication_folder: "team skills" });
  await page.addInitScript(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (value: string) => { (window as Window & { copiedCommand?: string }).copiedCommand = value; } } }));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/publish");
  const installation = page.getByRole("region", { name: "Agent installation", exact: true });
  const command = "sh '/home/test/Chris'\\''s skills/team skills/install.sh'";
  await expect(installation).not.toBeVisible();
  await page.getByRole("button", { name: "Publish skills", exact: true }).click();
  await expect(installation).toBeVisible();
  await expect(installation.getByLabel("Install command")).toHaveText(command);
  await installation.getByRole("button", { name: "Copy install command" }).click();
  expect(await page.evaluate(() => (window as Window & { copiedCommand?: string }).copiedCommand)).toBe(command);
  await expect(installation.getByRole("status")).toHaveText("Command copied.");
  await page.screenshot({ path: "test-results/community-install-after-publish-desktop.png", fullPage: true });
  expect(state.requests).not.toContain("PATCH /api/settings");
  expect(state.publishes).toBe(1);

  await page.reload();
  const disclosure = page.locator("summary", { hasText: "Install or reconnect agents" });
  await disclosure.focus();
  await page.keyboard.press("Enter");
  await expect(installation.getByLabel("Install command")).toHaveText(command);
  expect(state.publishes).toBe(1);
  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined }));
  await installation.getByRole("button", { name: "Copy install command" }).click();
  await expect(installation.getByRole("status")).toHaveText("Select and copy the command above.");
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/community-install-after-publish-${width}.png`, fullPage: true });
  }
  expect(state.violations).toEqual([]);
});

test("failed publication does not open installation instructions until retry succeeds", async ({ page }) => {
  const state = await fixture(page);
  let fail = true;
  await page.route("**/api/publish", route => fail
    ? route.fulfill({ status: 422, json: { detail: "Publication is temporarily unavailable." }, headers: { "Access-Control-Allow-Origin": UI } })
    : route.fallback());
  await page.goto("/publish");
  await page.getByRole("button", { name: "Publish skills", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Publication is temporarily unavailable." })).toBeVisible();
  await expect(page.getByRole("region", { name: "Agent installation", exact: true })).not.toBeVisible();
  expect(state.publishes).toBe(0);
  fail = false;
  await page.getByRole("button", { name: "Publish skills", exact: true }).click();
  await expect(page.getByLabel("Install command")).toBeVisible();
  expect(state.publishes).toBe(1);
});

test("installation instructions withhold commands for an unmapped folder or missing repository", async ({ page }) => {
  const state = await fixture(page);
  Object.assign(state.settings, { publish_in_container: true, publish_root: "/published", publish_host_path: null, publish_host_root: null });
  await page.goto("/publish");
  await page.locator("summary", { hasText: "Install or reconnect agents" }).click();
  await expect(page.getByText("Connect a host folder in Set up publication before installing. The installer needs the folder on your computer.", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Install command")).toHaveCount(0);
  Object.assign(state.settings, { publication_target: "git", publication_git_url: "", publication_ready: false });
  await page.reload();
  await page.locator("summary", { hasText: "Install or reconnect agents" }).click();
  await expect(page.getByText("Choose a Git repository in Set up publication before connecting your agents.", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Install command")).toHaveCount(0);
  expect(state.publishes).toBe(0);
});

test("ordinary Git publication shows installation and read access for the configured repository", async ({ page }) => {
  const state = await fixture(page);
  Object.assign(state.settings, { publication_target: "git", publication_git_url: "https://github.com/example-org/agent-guidance.git", publication_git_branch: "guidance" });
  await page.goto("/publish");
  await page.getByRole("button", { name: "Publish and push", exact: true }).click();
  await expect(page.getByLabel("Install command")).toContainText("git clone --branch 'guidance' -- 'https://github.com/example-org/agent-guidance.git'");
  await expect(page.getByLabel("Install command")).toContainText('&&\nsh "$HOME/.oms/bundles/github.com-example-org-agent-guidance/install.sh"');
  await expect(page.getByRole("region", { name: "Computer Git access" })).toBeVisible();
  await expect(page.getByText(/Already cloned this repository/)).toBeVisible();
  expect(state.requests).not.toContain("PATCH /api/settings");
  expect(state.publishes).toBe(1);
  expect(state.violations).toEqual([]);
});

test("host folder changes target the same workspace and wait for the real mount", async ({ page }) => {
  const state = await fixture(page);
  Object.assign(state.settings, { publish_in_container: true, publish_host_root: "/home/test/Community/published",
    publish_host_path: "/home/test/Community/published", publish_host_home: "/home/test",
    publish_launcher_dir: "/home/test/OMS Community", publish_compose_project: "ubuntu-qa" });
  await page.goto("/publish");
  await page.getByRole("button", { name: "Change folder", exact: true }).click();
  await expect(page.getByLabel("New host folder")).toBeFocused();
  await expect(page.getByLabel("New host folder")).toHaveValue("~/.oms_skills");
  await expect(page.getByLabel("Folder change command")).toContainText("cd '/home/test/OMS Community' &&");
  await expect(page.getByLabel("Folder change command")).toContainText("COMPOSE_PROJECT_NAME='ubuntu-qa' ./run-local.sh --publish-dir '~/.oms_skills'");
  await expect(page.getByRole("button", { name: "Publish skills", exact: true })).toBeDisabled();
  await expect(page.locator("summary", { hasText: "Install or reconnect agents" })).toHaveCount(0);
  await page.getByRole("button", { name: "Check folder connection" }).click();
  await expect(page.getByRole("region", { name: "Host publication folder" }).getByRole("alert")).toContainText("OMS is still using /home/test/Community/published");
  expect(state.requests).not.toContain("PATCH /api/settings");
  await page.screenshot({ path: "test-results/community-host-folder-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/community-host-folder-mobile.png", fullPage: true });
  Object.assign(state.settings, { publish_host_root: "/home/test/.oms_skills", publish_host_path: "/home/test/.oms_skills" });
  await page.getByRole("button", { name: "Check folder connection" }).click();
  await expect(page.getByText("Folder connected. Publish again, then run the installer from the new folder.", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Publish skills", exact: true })).toBeEnabled();
  await page.locator("summary", { hasText: "Install or reconnect agents" }).click();
  await expect(page.getByLabel("Install command")).toHaveText("sh '/home/test/.oms_skills/install.sh'");
  await page.getByRole("button", { name: "Set up publication", exact: true }).click();
  await expect(page.getByRole("region", { name: "Host publication folder" })).toContainText("/home/test/.oms_skills");
  await page.getByRole("button", { name: "Change folder", exact: true }).click();
  await expect(page.getByRole("button", { name: "Publish skills", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Save and continue", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Cancel folder change" }).click();
  await page.getByRole("button", { name: "Save and continue", exact: true }).click();
  await page.getByRole("button", { name: "Continue to publish" }).click();
  await page.getByRole("button", { name: "Publish bundle", exact: true }).click();
  await expect(page.getByLabel("Install command")).toContainText("sh '/home/test/.oms_skills/install.sh'");
  await page.getByRole("button", { name: "Hide setup", exact: true }).click();
  await expect(page.getByLabel("Install command")).toBeVisible();
  expect(state.publishes).toBe(1);
  expect(state.violations).toEqual([]);
});

test("host folder setup quotes paths and cannot save an unconnected destination", async ({ page }) => {
  const state = await fixture(page);
  Object.assign(state.settings, { publish_in_container: true, publish_host_home: "/home/test",
    publish_launcher_dir: "/home/test/OMS", publish_compose_project: "ubuntu-qa" });
  await page.goto("/settings");
  await page.getByRole("button", { name: "Change folder", exact: true }).click();
  await page.getByLabel("New host folder").fill("/home/test/Chris's skills $(touch nope)");
  await expect(page.getByLabel("Folder change command")).toContainText("--publish-dir '/home/test/Chris'\\''s skills $(touch nope)'");
  await expect(page.getByRole("button", { name: "Save and continue" })).toBeDisabled();
  await page.getByLabel("New host folder").fill("/");
  await expect(page.getByLabel("Folder change command")).toHaveCount(0);
  await page.getByRole("button", { name: "Cancel folder change" }).click();
  await expect(page.getByRole("button", { name: "Save and continue" })).toBeEnabled();
});

test("Git setup guides the exact Docker publisher before connecting an agent computer", async ({ page }) => {
  const state = await fixture(page);
  Object.assign(state.settings, { publish_in_container: true, publish_container_id: "a1b2c3d4e5f6" });
  let rejectPublish = true;
  await page.route("**/api/publish", async route => {
    if (route.request().method() !== "POST" || !rejectPublish) return route.fallback();
    return route.fulfill({ status: 422, json: { detail: "Git publication failed. Check the repository, branch and the server's Git access, then publish again." }, headers: { "Access-Control-Allow-Origin": UI } });
  });
  await page.goto("/settings");
  await page.getByRole("radio", { name: "A Git repository", exact: true }).check();
  await page.getByLabel("Git repository URL", { exact: true }).fill("https://github.com/example-org/agent-guidance.git");
  await page.getByLabel("Branch", { exact: true }).fill("main");
  await page.getByRole("button", { name: "Save and continue", exact: true }).click();
  const publisher = page.getByRole("region", { name: "Publisher Git access" });
  await expect(publisher).toBeVisible();
  await expect.poll(async () => (await publisher.boundingBox())?.width ?? 0).toBeGreaterThan(700);
  await expect(publisher.getByLabel("Publisher login command")).toContainText("docker exec -it 'a1b2c3d4e5f6' oms git-login");
  await expect(publisher.getByLabel("Publisher login command")).not.toContainText("compose");
  const token = new URL((await publisher.getByRole("link", { name: /Create a GitHub publishing token/ }).getAttribute("href"))!);
  expect(token.searchParams.get("target_name")).toBe("example-org");
  expect(token.searchParams.get("contents")).toBe("write");
  await expect(page.getByRole("region", { name: "Computer Git access" })).toHaveCount(0);
  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (value: string) => { (window as Window & { copiedCommand?: string }).copiedCommand = value; } } }));
  await publisher.getByRole("button", { name: "Copy publisher login command", exact: true }).click();
  expect(await page.evaluate(() => (window as Window & { copiedCommand?: string }).copiedCommand)).toBe("docker exec -it 'a1b2c3d4e5f6' oms git-login --repository 'https://github.com/example-org/agent-guidance.git'");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: "test-results/community-git-access-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: "test-results/community-git-access-mobile.png", fullPage: true });
  await page.getByRole("button", { name: "Continue to publish", exact: true }).click();
  await page.getByRole("button", { name: "Publish and push", exact: true }).click();
  await expect(page.getByText(/token selects this repository/)).toBeVisible();
  await page.getByRole("button", { name: "Back to Git access", exact: true }).click();
  await expect(publisher.getByLabel("Publisher login command")).toContainText("example-org/agent-guidance.git");
  rejectPublish = false;
  await page.getByRole("button", { name: "Continue to publish", exact: true }).click();
  await page.getByRole("button", { name: "Publish and push", exact: true }).click();
  const reader = page.getByRole("region", { name: "Computer Git access" });
  await expect(reader).toBeVisible();
  await expect(publisher).toHaveCount(0);
  await expect(reader.getByRole("radio", { name: "Private repository" })).toBeChecked();
  await expect(reader.getByLabel("Computer read check")).toContainText("git ls-remote -- 'https://github.com/example-org/agent-guidance.git'");
  await expect(page.getByLabel("Install command")).toContainText("github.com-example-org-agent-guidance");
  await expect(page.getByLabel("Install command")).toContainText("&&");
  await reader.getByRole("radio", { name: "Public repository" }).check();
  await expect(reader.getByText(/No Git login or token is needed/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy install command" })).toBeVisible();
  await page.screenshot({ path: "test-results/community-git-install-mobile.png", fullPage: true });
  expect(state.violations).toEqual([]);
});

test("choosing Git warns about a loopback contribution address before any token work", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/settings");
  await expect(page.getByText(/OMS_PUBLIC_URL/)).toHaveCount(0);
  await page.getByRole("radio", { name: "A Git repository", exact: true }).check();
  const warning = page.getByRole("status").filter({ hasText: "OMS_PUBLIC_URL" });
  await expect(warning).toBeVisible();
  await expect(warning).toContainText("http://localhost:4317");
  await expect(warning).toContainText(".env.local");
  await expect(warning).toContainText("Git publication is refused");
  Object.assign(state.settings, { public_url: "https://oms.example.org", public_url_is_loopback: false });
  await page.reload();
  await page.getByRole("radio", { name: "A Git repository", exact: true }).check();
  await expect(page.getByText(/OMS_PUBLIC_URL/)).toHaveCount(0);
  await expect(page.getByText(/send corrections to/)).toContainText("https://oms.example.org");
  Object.assign(state.settings, { public_url: null, public_url_is_loopback: false });
  await page.reload();
  await page.getByRole("radio", { name: "A Git repository", exact: true }).check();
  await expect(page.getByText(/Contribution is off/)).toBeVisible();
  expect(state.violations).toEqual([]);
});

test("import, edit a correction, apply it, inspect lineage and publish a preview", async ({ page }) => {
  const state = await fixture(page, { empty: true });
  await page.goto("/import");
  await page.getByLabel("Skill ZIP package").setInputFiles({ name: "skills.zip", mimeType: "application/zip", buffer: Buffer.from("public-browser-fixture") });
  await page.getByRole("button", { name: "Import package", exact: true }).click();
  await expect(page.getByText("Skill package imported.", { exact: true })).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Correction inbox" }).click();
  await page.getByRole("button", { name: `Review correction: ${correction.text}`, exact: true }).click();
  await page.getByRole("tab", { name: "Add or confirm a rule", exact: true }).click();
  await expect(page.getByRole("button", { name: "Create rule", exact: true })).toBeDisabled();
  await page.getByLabel("Proposed rule wording").fill("Verify the receipt and approval before submitting an expense.");
  await page.getByLabel("Expense review", { exact: true }).check();
  await page.getByRole("button", { name: "Create rule", exact: true }).click();
  await expect(page.getByRole("heading", { name: "You’re all caught up" })).toBeVisible();
  expect(state.rules).toHaveLength(1);
  await page.getByRole("navigation").getByRole("link", { name: "Skills", exact: true }).click();
  await page.getByRole("button", { name: /^Expense review/ }).click();
  await page.getByRole("tab", { name: "Edit document", exact: true }).click();
  await page.getByText("Correction evidence", { exact: true }).click();
  await expect(page.getByText("Corrections: correction-1", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Files (2)", exact: true }).click();
  await expect(page.getByText("references/policy.md")).toBeVisible();
  await expect(page.getByText("scripts/check.sh")).toBeVisible();
  await page.getByRole("button", { name: "references/policy.md", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Expense policy", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "scripts/check.sh", exact: true }).click();
  await expect(page.locator(".file-source")).toContainText("printf 'checked");
  await page.getByRole("tab", { name: "History", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Saved versions", exact: true })).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Preview & publish" }).click();
  await page.getByRole("combobox", { name: "Skill", exact: true }).selectOption("expenses");
  await page.getByRole("button", { name: "Preview skill", exact: true }).click();
  await expect(page.getByLabel("Published skill preview")).toContainText(state.rules[0].body);
  await expect(page.getByLabel("Published skill preview")).toContainText("Keep the original introduction.");
  await page.getByRole("button", { name: "Publish skills", exact: true }).click();
  await expect(page.getByText("Workspace published.", { exact: true })).toBeVisible();
  expect(state.publishes).toBe(1);
  expect(state.violations).toEqual([]);
});

test("safety release requires a second manual decision and exact reinforcement is explicit", async ({ page }) => {
  const state = await fixture(page, { held: true, exact: true });
  await page.goto("/inbox");
  await page.getByRole("button", { name: `Review correction: ${correction.text}`, exact: true }).click();
  await expect(page.getByRole("button", { name: "Create rule", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Release for manual review" }).click();
  await page.getByRole("tab", { name: "Add or confirm a rule", exact: true }).click();
  await expect(page.getByRole("button", { name: "Create rule", exact: true })).toBeVisible();
  expect(state.rules[0].corroboration_count).toBe(1);
  await page.getByLabel("Expense review", { exact: true }).check();
  await expect(page.getByRole("button", { name: "Reinforce existing rule" })).toBeDisabled();
  await page.getByRole("radio").check();
  await page.getByRole("button", { name: "Reinforce existing rule" }).click();
  await expect(page.getByRole("heading", { name: "You’re all caught up" })).toBeVisible();
  expect(state.rules).toHaveLength(1);
  expect(state.rules[0].corroboration_count).toBe(2);
  expect(state.violations).toEqual([]);
});

test("similar wording requires comparison and explicit confirmation while retaining the existing rule", async ({ page }) => {
  const state = await fixture(page, { similar: true });
  const original = state.rules[0].body;
  await page.goto("/inbox");
  await page.getByRole("button", { name: `Review correction: ${correction.text}`, exact: true }).click();
  await expect(page.getByText("Name or ID closely resembles the supplied skill hint.")).toBeVisible();
  await page.getByRole("tab", { name: "Add or confirm a rule", exact: true }).click();
  await page.getByLabel("Expense review", { exact: true }).check();
  await page.getByRole("radio", { name: `Select rule: ${original}`, exact: true }).check();
  await expect(page.getByRole("region", { name: "Review selected correction", exact: true })).toContainText(correction.text);
  await expect(page.getByRole("region", { name: "Review selected correction", exact: true })).toContainText(original);
  await expect(page.getByLabel("Proposed rule wording")).toHaveValue(correction.text);
  const reinforce = page.getByRole("button", { name: "Reinforce existing rule", exact: true });
  await expect(reinforce).toBeDisabled();
  const confirmation = page.getByLabel("I have compared the wording and confirm this correction supports the existing rule.");
  await confirmation.check();
  await page.getByLabel("Proposed rule wording").fill("Keep the original receipt for every expense claim.");
  await expect(confirmation).not.toBeChecked();
  await expect(reinforce).toBeDisabled();
  expect(state.decisions).toEqual([]);
  await confirmation.check();
  await reinforce.click();
  await expect(page.getByRole("heading", { name: "You’re all caught up" })).toBeVisible();
  expect(state.decisions).toEqual([{ action: "reinforce", body: "Keep the original receipt for every expense claim.", skill_ids: ["expenses"], rule_id: "existing", confirm_reinforcement: true, expected_rule_body: original }]);
  expect(state.rules[0].body).toBe(original);
  expect(state.rules[0].corroboration_count).toBe(2);
  expect(state.violations).toEqual([]);
});

test("disabled manual capability hides its navigation and makes no review request", async ({ page }) => {
  const state = await fixture(page, { manual: false });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Workspace overview" })).toBeVisible();
  await expect(page.getByRole("navigation").getByRole("link", { name: "Correction inbox" })).toHaveCount(0);
  expect(state.requests).not.toContain("GET /api/review");
  expect(state.violations).toEqual([]);
});

test("a browser contribution goes through ingestion and becomes a visible manual item", async ({ page }) => {
  const state = await fixture(page);
  state.inbox = [];
  await page.goto("/inbox");
  await page.getByRole("button", { name: "Submit a correction", exact: true }).click();
  await page.getByLabel("Correction", { exact: true }).fill("Check the currency before approving an expense.");
  await page.getByLabel("Skill hint").selectOption("expenses");
  await page.getByRole("button", { name: "Send to inbox" }).click();
  await expect(page.getByRole("button", { name: "Review correction: Check the currency before approving an expense.", exact: true })).toHaveAttribute("aria-expanded", "false");
  expect(state.inbox).toHaveLength(1);
  expect(state.inbox[0].text).toBe("Check the currency before approving an expense.");
  expect(state.requests).toContain("POST /api/ingest");
  expect(state.violations).toEqual([]);
});

test("skill edits, rule lifecycle, constraints and settings use the public contract", async ({ page }) => {
  const state = await fixture(page, { exact: true });
  await page.goto("/skills");
  await page.getByRole("button", { name: /^Expense review/ }).click();
  await page.getByRole("button", { name: "Edit details" }).click();
  await page.getByRole("textbox", { name: "Description", exact: true }).fill("A clearer skill description.");
  await page.getByRole("button", { name: "Save details" }).click();
  await expect(page.getByText("Skill details saved.")).toBeVisible();
  await page.getByRole("tab", { name: "Edit document", exact: true }).click();
  await page.locator('[data-anchor="block:intro"]').getByRole("textbox").fill("Amended introductory prose.");
  await page.getByRole("button", { name: "Save document" }).click();
  await expect(page.getByText("Document saved.")).toBeVisible();
  expect(state.skills[0].sections[0].text).toBe("Amended introductory prose.");
  await page.getByRole("button", { name: "Retract rule" }).click();
  await page.getByText("Inactive rules (1)", { exact: true }).click();
  await expect(page.getByRole("button", { name: "Restore rule" })).toBeVisible();
  expect(state.rules[0].status).toBe("retracted");
  await page.getByRole("button", { name: "Restore rule" }).click();
  await expect(page.getByRole("button", { name: "Retract rule" })).toBeVisible();
  expect(state.rules[0].status).toBe("active");
  await page.getByRole("navigation").getByRole("link", { name: "Constraints", exact: true }).click();
  await expect(page.getByText(/Active constraints appear in the published root instruction files/)).toBeVisible();
  await page.getByLabel("Required guidance").fill("Never publish payment credentials.");
  await page.getByRole("button", { name: "Add constraint" }).click();
  await expect(page.getByText("Never publish payment credentials.", { exact: true })).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Settings" }).click();
  await page.getByLabel("Maximum rules in the main skill file").fill("300");
  await page.getByLabel("Versions to retain per skill").fill("150");
  await page.getByLabel("CLAUDE.md", { exact: true }).uncheck();
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect.poll(() => state.settings.body_budget).toBe(300);
  expect(state.settings.skill_history_keep).toBe(150);
  expect(state.settings.root_instruction_files).toEqual(["AGENTS.md"]);
  expect(state.requests).not.toContain("GET /api/rules");
  expect(state.violations).toEqual([]);
});

test("import decisions show their exact consequences and preserve edited source wording", async ({ page }) => {
  const state = await fixture(page);
  state.importReviews = [
    { id: "review-block", kind: "block_revision", subject_id: "intro", reason: "The source introduction changed.", proposed_body: "A proposed source introduction.", skill_id: "expenses" },
    { id: "review-removal", kind: "removal", subject_id: "old-rule", reason: "An earlier rule is no longer present." },
    { id: "review-safety", kind: "injection", subject_id: "screened-rule", reason: "Review this screened instruction.", proposed_body: "The source contains an instruction that needs review." },
    { id: "review-conflict", kind: "polarity_conflict", subject_id: "earlier-rule", other_id: "new-rule", reason: "The new source rule is already present." },
  ];
  await page.goto("/import");
  await expect(page.getByRole("heading", { name: "Source text changed" })).toBeVisible();
  await page.getByRole("link", { name: "Open the affected skill" }).click();
  await expect(page.locator(".page-heading").getByRole("heading", { name: "Expense review", exact: true })).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Import", exact: true }).click();
  await page.getByLabel("Source wording to apply").fill("The operator reviewed and amended this introduction.");
  await page.screenshot({ path: "test-results/community-import-review.png", fullPage: true });
  await page.getByRole("button", { name: "Apply source text", exact: true }).click();
  await page.getByRole("button", { name: "Keep rule", exact: true }).click();
  await page.getByRole("button", { name: "Retire rule", exact: true }).click();
  await page.getByRole("button", { name: "Keep earlier rule as well", exact: true }).click();
  await expect(page.getByRole("heading", { name: "No import decisions waiting" })).toBeVisible();
  expect(state.importDecisions).toEqual([
    { id: "review-block", action: "accept", body: "The operator reviewed and amended this introduction." },
    { id: "review-removal", action: "reject" },
    { id: "review-safety", action: "reject" },
    { id: "review-conflict", action: "accept" },
  ]);
  expect(state.violations).toEqual([]);
});

test("local constraints can be amended, retired and restored while imported requirements preserve source custody", async ({ page }) => {
  const state = await fixture(page);
  state.constraints = [
    { id: "local-requirement", body: "Keep all supporting records.", status: "active", source: "console" },
    { id: "source-requirement", body: "Never publish access credentials.", status: "retired", source: "import" },
  ];
  await page.goto("/rules");
  const local = page.getByRole("article", { name: "Constraint local-requirement" });
  const imported = page.getByRole("article", { name: "Constraint source-requirement" });
  const archive = page.locator(".constraint-archive");
  await expect(local).toBeVisible();
  await expect(imported).toBeHidden();
  await expect(archive.locator("summary")).toHaveText("Inactive constraints (1)");
  await page.screenshot({ path: "test-results/community-constraints-collapsed.png", fullPage: true });
  await archive.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(imported.getByText("Edit the source file and import it again to change this constraint.")).toBeVisible();
  await expect(imported.getByRole("button")).toHaveCount(0);
  await archive.locator("summary").click();
  await expect(imported).toBeHidden();
  await local.getByRole("button", { name: "Edit constraint" }).click();
  await local.getByLabel("Constraint wording").fill("Retain the original supporting records.");
  await local.getByRole("button", { name: "Save constraint" }).click();
  await expect(local.getByText("Retain the original supporting records.", { exact: true })).toBeVisible();
  await local.getByRole("button", { name: "Retire constraint" }).click();
  await expect(local).toBeHidden();
  await expect(page.getByRole("heading", { name: "No active constraints", exact: true })).toBeVisible();
  await expect(archive.locator("summary")).toHaveText("Inactive constraints (2)");
  await archive.locator("summary").click();
  await expect(local.getByRole("button", { name: "Restore constraint" })).toBeVisible();
  expect(state.constraints[0].status).toBe("retired");
  await page.screenshot({ path: "test-results/community-constraints.png", fullPage: true });
  await local.getByRole("button", { name: "Restore constraint" }).click();
  await expect(local.getByRole("button", { name: "Retire constraint" })).toBeVisible();
  await expect(archive.locator("summary")).toHaveText("Inactive constraints (1)");
  await expect(archive.getByRole("article", { name: "Constraint local-requirement" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "No active constraints", exact: true })).toHaveCount(0);
  expect(state.constraints[0].status).toBe("active");
  expect(state.requests).not.toContain("PATCH /api/constraints/source-requirement");
  expect(state.violations).toEqual([]);
});


test("the skills library imports a package in place and renders safe GFM documents", async ({ page }) => {
  const state = await fixture(page, { empty: true });
  await page.goto("/skills");
  await page.getByRole("button", { name: "Upload skills", exact: true }).click();
  await page.getByLabel("Skill ZIP package").setInputFiles({ name: "skills.zip", mimeType: "application/zip", buffer: Buffer.from("public-browser-fixture") });
  await page.getByRole("button", { name: "Import package", exact: true }).click();
  await expect(page.getByRole("button", { name: /^Expense review/ })).toBeVisible();
  await expect(page).toHaveURL(/\/skills\/?$/);
  state.skills[0].body = `---
name: expenses
description: Example metadata
---
# Expense review

**Keep** clear records.

| Check | Required |
| --- | --- |
| Receipt | Yes |

- [x] Check currency

\`\`\`sh
printf 'ready'
\`\`\`

[Policy](https://example.com/policy)

![tracking image](https://external.example/tracker.png)

[Unsafe](javascript:alert(1))

<script>alert('untrusted')</script>`;
  await page.getByRole("button", { name: /^Expense review/ }).click();
  const document = page.locator(".oms-prose").first();
  await expect(document.getByRole("table")).toBeVisible();
  await expect(document.locator("strong")).toHaveText("Keep");
  await expect(document.locator("pre code")).toContainText("printf 'ready'");
  await expect(document.getByRole("checkbox")).toBeChecked();
  expect((await document.getByRole("checkbox").boundingBox())!.width).toBeLessThan(24);
  await expect(document.getByRole("link", { name: "Policy" })).toHaveAttribute("href", "https://example.com/policy");
  await expect(document.locator("img, script, a[href^='javascript:']")).toHaveCount(0);
  await expect(document).not.toContainText("description: Example metadata");
  await page.getByRole("tab", { name: "Markdown source", exact: true }).click();
  await expect(page.getByRole("tabpanel").locator("pre")).toContainText("description: Example metadata");
  expect(state.requests.filter((request) => request === "POST /api/import")).toHaveLength(1);
  expect(state.violations).toEqual([]);
  await page.screenshot({ path: "test-results/community-skill-document.png", fullPage: true });
});

test("the graph draws connected nodes, expands beyond a skill seed and clears its canvas", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/graph");
  const graph = page.getByTestId("graph-canvas");
  await expect(graph).toHaveAttribute("data-ready", "true");
  await expect(graph).toHaveAttribute("data-node-count", "2");
  await expect(graph).toHaveAttribute("data-edge-count", "1");
  const nodeLayer = graph.locator('canvas[data-id="layer2-node"]');
  await expect(nodeLayer).toBeVisible();
  // Inspect the actual drawing layer, not Cytoscape's transparent event canvas.
  await expect.poll(() => nodeLayer.evaluate((canvas) => {
    const image = canvas as HTMLCanvasElement;
    const pixels = image.getContext("2d")!.getImageData(0, 0, image.width, image.height).data;
    return pixels.some((value, index) => index % 4 === 3 && value > 0);
  })).toBe(true);
  const renderer = () => graph.evaluate((element) => {
    const cy = (element as HTMLElement & { _cyreg: { cy: import("cytoscape").Core } })._cyreg.cy;
    return { zoom: cy.zoom(), labels: cy.nodes().first().style("text-opacity"), selected: cy.nodes(":selected").map((node) => node.id()) };
  });
  await page.getByRole("combobox", { name: "Layout", exact: true }).selectOption("grid");
  const initialZoom = (await renderer()).zoom;
  await page.getByRole("button", { name: "Zoom in", exact: true }).click();
  await expect.poll(async () => (await renderer()).zoom).toBeGreaterThan(initialZoom);
  await page.getByRole("button", { name: "Zoom out", exact: true }).click();
  await page.getByRole("button", { name: "Fit graph", exact: true }).click();
  await page.getByRole("checkbox", { name: "Labels", exact: true }).uncheck();
  await expect.poll(async () => (await renderer()).labels).toBe("0");
  await page.getByRole("checkbox", { name: "Labels", exact: true }).check();
  await expect.poll(async () => (await renderer()).labels).toBe("1");
  await page.getByRole("combobox", { name: "Inspect a node", exact: true }).selectOption("expenses");
  await expect(page.getByRole("link", { name: "Open skill document" })).toBeVisible();
  await expect.poll(async () => (await renderer()).selected).toEqual(["expenses"]);
  await expect(page.locator(".graph-connections")).toContainText("BELONGS_TO");
  await page.locator(".graph-connections").getByRole("button", { name: "Verify the receipt" }).click();
  await expect(page.locator(".graph-detail")).toContainText("active");
  // Select a real rendered node by pointer as well as through the keyboard path.
  const point = await graph.evaluate((element) => {
    const cy = (element as HTMLElement & { _cyreg: { cy: import("cytoscape").Core } })._cyreg.cy;
    return cy.getElementById("expenses").renderedPosition();
  });
  await graph.click({ position: point });
  await expect(page.getByRole("combobox", { name: "Inspect a node" })).toHaveValue("expenses");
  await page.screenshot({ path: "test-results/community-graph.png", fullPage: true });
  await page.getByRole("combobox", { name: "Find a skill", exact: true }).selectOption("expenses");
  await page.getByRole("button", { name: "Explore", exact: true }).click();
  await expect(graph).toHaveAttribute("data-node-count", "1");
  await expect(graph).toHaveAttribute("data-edge-count", "0");
  await expect(page.getByRole("button", { name: "Expand connections", exact: true })).toBeVisible();
  const seedPoint = await graph.evaluate((element) => {
    const cy = (element as HTMLElement & { _cyreg: { cy: import("cytoscape").Core } })._cyreg.cy;
    return cy.getElementById("expenses").renderedPosition();
  });
  await graph.dblclick({ position: seedPoint });
  await expect(page.getByText("1 of 2 direct connections loaded.", { exact: true })).toBeVisible();
  await expect(graph).toHaveAttribute("data-node-count", "2");
  await page.getByRole("button", { name: "Load more connections", exact: true }).click();
  await expect(graph).toHaveAttribute("data-node-count", "3");
  await expect(graph).toHaveAttribute("data-edge-count", "2");
  await expect(page.getByRole("button", { name: "All connections loaded", exact: true })).toBeDisabled();
  expect(state.graphPages).toEqual([0, 1]);
  await page.getByRole("button", { name: "Clear canvas", exact: true }).click();
  await expect(page.getByRole("heading", { name: "No nodes to display" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Fit graph", exact: true })).toBeDisabled();
  await expect(page.getByRole("heading", { name: "Select a node", exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "Find a skill", exact: true }).selectOption("");
  await page.getByRole("button", { name: "Explore", exact: true }).click();
  await expect(graph).toHaveAttribute("data-node-count", "2");
  expect(state.violations).toEqual([]);
});

test("Community uses the OMS brand and readable layout at desktop and phone widths", async ({ page }) => {
  const state = await fixture(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/skills");
  const productLogo = page.getByRole("img", { name: "OMS", exact: true });
  await expect(productLogo).toBeVisible();
  await expect(productLogo).toHaveAttribute("src", /oms_logo_darkthemev2/);
  await expect.poll(() => productLogo.evaluate(image => (image as HTMLImageElement).naturalWidth)).toBe(1536);
  const logo = page.getByRole("img", { name: "InoGen AI", exact: true });
  await expect(logo).toBeVisible();
  await expect.poll(() => logo.evaluate((image) => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
  await page.evaluate(() => document.fonts.ready);
  const theme = await page.locator("body").evaluate((body) => ({ background: getComputedStyle(body).backgroundColor, font: getComputedStyle(body).fontFamily, accent: getComputedStyle(body).getPropertyValue("--accent").trim() }));
  expect(theme.background).toBe("rgb(10, 11, 17)");
  expect(theme.font).toContain("Inter");
  expect(theme.accent).toBe("#00dee8");
  await page.screenshot({ path: "test-results/community-skills-desktop.png", fullPage: true });
  await page.getByRole("button", { name: "Collapse sidebar", exact: true }).click();
  await expect(productLogo).toBeVisible();
  expect((await productLogo.boundingBox())!.width).toBeLessThanOrEqual(50);
  await page.screenshot({ path: "test-results/community-logo-collapsed.png" });
  await page.getByRole("button", { name: "Expand sidebar", exact: true }).click();
  for (const width of [1100, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await expect(productLogo).toBeVisible();
    await expect(page.getByRole("button", { name: "Upload skills", exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  }
  await page.screenshot({ path: "test-results/community-skills-mobile.png", fullPage: true });
  expect(state.violations).toEqual([]);
});


test("graph and node fetch failures are recoverable without stale details", async ({ page }) => {
  const state = await fixture(page);
  let graphFails = true;
  let nodeFails = true;
  await page.route("**/api/graph?*", async (route) => {
    if (graphFails) return route.fulfill({ status: 503, json: { detail: "Graph temporarily unavailable" }, headers: { "Access-Control-Allow-Origin": UI } });
    return route.fallback();
  });
  await page.route("**/api/graph/nodes/expenses", async (route) => {
    if (nodeFails) return route.fulfill({ status: 503, json: { detail: "Node temporarily unavailable" }, headers: { "Access-Control-Allow-Origin": UI } });
    return route.fallback();
  });
  await page.goto("/graph");
  await expect(page.getByRole("alert").filter({ hasText: "Graph temporarily unavailable" })).toBeVisible();
  graphFails = false;
  await page.getByRole("button", { name: "Explore", exact: true }).click();
  await expect(page.getByTestId("graph-canvas")).toHaveAttribute("data-ready", "true");
  await page.getByRole("combobox", { name: "Inspect a node" }).selectOption("expenses");
  await expect(page.getByRole("alert").filter({ hasText: "Node temporarily unavailable" })).toBeVisible();
  nodeFails = false;
  await page.getByRole("button", { name: "Retry node details" }).click();
  await expect(page.getByRole("link", { name: "Open skill document" })).toBeVisible();
  await page.getByRole("combobox", { name: "Inspect a node" }).selectOption("");
  await expect(page.getByRole("heading", { name: "Select a node", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Open skill document" })).toHaveCount(0);
  expect(state.violations).toEqual([]);
});

test("clearing the graph ignores a pending expansion instead of repopulating the canvas", async ({ page }) => {
  const state = await fixture(page);
  let release!: () => void;
  let requested!: () => void;
  const blocked = new Promise<void>(resolve => { release = resolve; });
  const started = new Promise<void>(resolve => { requested = resolve; });
  await page.route("**/api/graph/nodes/expenses/neighbours?*", async route => {
    if (route.request().method() !== "GET") return route.fallback();
    requested();
    await blocked;
    return route.fallback();
  });
  await page.goto("/graph");
  await page.getByRole("combobox", { name: "Inspect a node", exact: true }).selectOption("expenses");
  await page.getByRole("button", { name: "Expand connections", exact: true }).click();
  await started;
  await page.getByRole("button", { name: "Clear canvas", exact: true }).click();
  await expect(page.getByRole("heading", { name: "No nodes to display", exact: true })).toBeVisible();
  const response = page.waitForResponse(response => response.url().includes("/api/graph/nodes/expenses/neighbours"));
  release();
  await (await response).finished();
  // Let the completed fetch and React commit settle before checking the race.
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.getByTestId("graph-canvas")).toHaveAttribute("data-node-count", "0");
  await expect(page.getByRole("heading", { name: "Select a node", exact: true })).toBeVisible();
  expect(state.graphPages).toEqual([0]);
  expect(state.violations).toEqual([]);
});

test("overview shows onboarding only for an empty library and real, linked changes for existing work", async ({ page }) => {
  const state = await fixture(page, { empty: true });
  state.inbox = [];
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Start with your skills" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Needs your attention" })).toBeVisible();
  state.skills = [structuredClone(skill)];
  state.inbox = [];
  state.importReviews = [{ id: "change-1", kind: "block_revision", subject_id: "intro", reason: "A source changed", skill_id: skill.id }];
  await page.reload();
  await expect(page.getByRole("heading", { name: "Workspace overview" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Start with your skills" })).toHaveCount(0);
  await expect(page.locator(".dashboard-actions").getByRole("link", { name: /Review source changes/ })).toBeVisible();
  const changes = page.locator(".dashboard-changes");
  await expect(changes.getByRole("link", { name: /Expense review/ })).toContainText("Uploaded a package");
  expect(state.requests).not.toContain("GET /api/skills/expenses");
  expect(state.requests).not.toContain("GET /api/rules");
  await page.screenshot({ path: "test-results/community-overview.png", fullPage: true });
  await changes.getByRole("link", { name: /Expense review/ }).click();
  await expect(page.locator(".page-heading").getByRole("heading", { name: "Expense review", exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "History", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Saved versions" })).toBeVisible();
  await expect(page.locator(".version-picker")).toContainText("Uploaded a package");
  await expect(page.locator(".version-picker")).not.toContainText("+0");
  await page.locator(".version-picker").getByRole("button", { name: /Uploaded a package/ }).click();
  await expect(page.getByRole("heading", { name: "No document changes", exact: true })).toBeVisible();
  await page.getByRole("group", { name: "Comparison view" }).getByRole("button", { name: "Detailed", exact: true }).click();
  await expect(page.locator(".oms-version-context")).toContainText("Keep the original introduction.");
  await expect(page.getByRole("checkbox", { name: /Restore/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Read saved version", exact: true }).click();
  await expect(page.getByRole("tabpanel").locator(".oms-prose")).toContainText("Keep the original introduction.");
  expect(state.requests).toContain("GET /api/skills/expenses/versions/version-1/compare");
  expect(state.violations).toEqual([]);
});

test("history comparison refreshes on every version click and clears stale selections after failures", async ({ page }) => {
  const state = await fixture(page);
  state.skills[0].sections[0].text = `Current wording with a long path: ${"nested-reference/".repeat(36)}guide.md`;
  let pending: Promise<void> | undefined;
  let release!: () => void;
  let fail = false;
  await page.route("**/api/skills/expenses/versions/version-1/compare", async route => {
    if (route.request().method() !== "GET") return route.fallback();
    if (pending) await pending;
    if (fail) return route.fulfill({ status: 503, json: { detail: "Comparison unavailable" }, headers: { "Access-Control-Allow-Origin": UI } });
    return route.fallback();
  });
  await page.goto("/skills");
  await page.getByRole("button", { name: /^Expense review/ }).click();
  await page.getByRole("tab", { name: "History", exact: true }).click();
  const version = page.locator(".version-picker").getByRole("button");
  await version.click();
  const view = page.getByRole("group", { name: "Comparison view", exact: true });
  const checkbox = page.getByRole("checkbox", { name: "Restore prose", exact: true });
  await expect(page.getByRole("button", { name: "Refresh comparison", exact: true })).toHaveCount(0);
  await checkbox.check();
  await view.getByRole("button", { name: "Detailed", exact: true }).focus();
  await page.keyboard.press("Space");
  await expect(checkbox).toBeChecked();
  await expect(page.locator(".oms-line-diff__line--del")).toContainText("nested-reference/");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await view.scrollIntoViewIfNeeded();
  await page.screenshot({ path: "test-results/community-history-mobile.png", fullPage: true });
  await view.getByRole("button", { name: "Overview", exact: true }).click();
  await expect(checkbox).toBeChecked();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await view.getByRole("button", { name: "Detailed", exact: true }).click();
  pending = new Promise<void>(resolve => { release = resolve; });
  fail = true;
  await version.click();
  await expect(page.getByRole("tabpanel").getByRole("status")).toHaveText("Loading…");
  await expect(checkbox).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Restore .*selected/ })).toHaveCount(0);
  release();
  await expect(page.getByRole("alert").filter({ hasText: "Comparison unavailable" })).toBeVisible();
  await expect(page.getByText("Select a saved version again to retry.", { exact: true })).toBeVisible();
  await expect(checkbox).toHaveCount(0);
  pending = undefined;
  fail = false;
  state.documentRevision++;
  state.skills[0].sections[0].text = "The current document changed while history was open.";
  await version.click();
  await expect(checkbox).toBeEnabled();
  await expect(checkbox).not.toBeChecked();
  await expect(page.getByRole("button", { name: "Restore selected changes", exact: true })).toBeDisabled();
  await expect(view.getByRole("button", { name: "Detailed", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".oms-line-diff__line--del")).toContainText("The current document changed while history was open.");
  await expect(page.getByRole("tabpanel").getByRole("alert")).toHaveCount(0);
  expect(state.violations).toEqual([]);
});

test("a recent-history outage leaves pending work usable and can be retried", async ({ page }) => {
  const state = await fixture(page);
  let unavailable = true;
  await page.route("**/api/skill-changes", (route) => unavailable
    ? route.fulfill({ status: 503, json: { detail: "Recent changes unavailable" }, headers: { "Access-Control-Allow-Origin": UI } })
    : route.fallback());
  await page.goto("/");
  await expect(page.getByRole("alert").filter({ hasText: "Recent changes unavailable" })).toBeVisible();
  await expect(page.locator(".dashboard-actions").getByRole("link", { name: /Review corrections/ })).toBeVisible();
  unavailable = false;
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(page.locator(".dashboard-changes")).toContainText("Expense review");
  expect(state.violations).toEqual([]);
});

test("labelled mobile navigation supports keyboard, dismissal and all destinations without overflow", async ({ page }) => {
  const state = await fixture(page);
  for (const width of [320, 390, 768]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/skills");
    await expect(page.getByRole("heading", { name: "Your skills library" })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    if (width > 720) {
      await expect(page.locator(".community-desktop-nav")).toBeVisible();
      await expect(page.getByRole("button", { name: "Pages, currently Skills" })).toBeHidden();
      continue;
    }
    const menu = page.getByRole("button", { name: "Pages, currently Skills" });
    await menu.click();
    const nav = page.getByRole("navigation", { name: "Main navigation" });
    await expect(nav.getByRole("link", { name: "Skills", exact: true })).toBeFocused();
    await page.keyboard.press("End");
    await expect(nav.getByRole("link", { name: "Settings", exact: true })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(menu).toBeFocused();
    await expect(menu).toHaveAttribute("aria-expanded", "false");
    await menu.click();
    await nav.getByRole("link", { name: "Graph", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Skill graph" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Pages, currently Graph" })).toHaveAttribute("aria-expanded", "false");
  }
  expect(state.violations).toEqual([]);
});
