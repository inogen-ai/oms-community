import { test, expect, type Page } from "@playwright/test";
import type { ReviewItem, Skill } from "@inogen/oms-client";
import { allowedRequest } from "./route-contract.mjs";

const API = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || "4317"}`;
const UI = `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}`;
const capabilities = {
  edition: "community", api_contract_version: "1.1", schema_version: 1,
  manual_learning: true, semantic_compilation: false, multi_user_identity: false,
  team_scoping: false, personal_mutes: false, contributor_portal: false, redaction_vault: false,
  model_settings: false, scheduled_publish: false, managed_publish: false,
  graph_query_console: false, advanced_review: false, usage_analytics: false,
};
const number = (index: number) => String(index).padStart(3, "0");

async function largeWorkspace(page: Page, options: { held?: number[]; longCorrection?: boolean } = {}) {
  const skills: Skill[] = Array.from({ length: 240 }, (_, index) => ({
    id: `skill-${number(index + 1)}`, name: `Skill ${number(index + 1)}`,
    description: `Practical guidance for task ${index + 1}.`, domain: index % 2 ? "operations" : "engineering",
    body: `# Skill ${number(index + 1)}\n\nKeep the original guidance.`,
    sections: [{ id: "introduction", heading: "Introduction", text: "Keep the original guidance.", editable: true }],
    rules: [], artefacts: [], versions: [],
  }));
  const items: ReviewItem[] = Array.from({ length: 201 }, (_, index) => ({
    txn_id: `correction-${number(index + 1)}`, text: `Correction ${number(index + 1)}: verify the recorded evidence.`,
    signal_type: "explicit_correction", source_ref: `agent:project-${index % 5}`,
    state: options.held?.includes(index + 1) ? "held_safety" : "awaiting_manual_review",
    held_reason: options.held?.includes(index + 1) ? "Inspect the screened source before continuing." : undefined,
    skill_hint: `task-${number(index + 1)}`, candidate_skill_ids: [], exact_matches: [], similar_matches: [],
    skill_suggestions: [{ id: `skill-${number(index + 1)}`, name: `Skill ${number(index + 1)}`, score: 0.6, reason: "Correction shares words with the skill name or description." }],
  }));
  if (options.longCorrection) items[0].text += " Preserve the complete source wording when judging this correction.".repeat(80);
  const state = {
    skills, items, violations: [] as string[], requests: [] as string[], failNextDecision: false,
    decisions: [] as { id: string; action: string; body: string; skill_ids: string[]; rule_id?: string }[],
    imports: [] as { bytes: Buffer; contentType: string }[],
    documentRevisions: {} as Record<string, number>,
    documentSaves: [] as { id: string; revision: string; parts: { anchor: string; text: string }[] }[],
  };
  const document = (skill: Skill) => ({ skill_id: skill.id, path: `${skill.id}/SKILL.md`, revision: `document-${state.documentRevisions[skill.id] ?? 0}`, parts: [
    { anchor: "title", kind: "title", editable: true, edit_text: skill.name, lines: [`# ${skill.name}`] },
    { anchor: "block:introduction", kind: "prose", editable: true, edit_text: skill.sections![0].text!, lines: [skill.sections![0].text!] },
  ] });
  await page.route("**/*", async (route) => {
    const request = route.request(); const url = new URL(request.url()); const method = request.method();
    if (url.origin === UI && !url.pathname.startsWith("/api/")) return route.continue();
    if (url.origin !== API) { state.violations.push(`Unexpected origin: ${method} ${url.origin}${url.pathname}`); return route.abort(); }
    const path = url.pathname;
    const reply = (value: unknown, status = 200) => route.fulfill({ status, json: value, headers: { "Access-Control-Allow-Origin": UI, "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET, POST, PATCH, PUT, OPTIONS" } });
    if (method === "OPTIONS") return reply({});
    state.requests.push(`${method} ${path}`);
    if (!allowedRequest(method, path)) { state.violations.push(`${method} ${path}`); return reply({ detail: "Unsupported Community operation" }, 418); }
    const body = request.headers()["content-type"]?.includes("application/json") ? request.postDataJSON() : {};
    if (path === "/api/capabilities") return reply(capabilities);
    if (path === "/api/health") return reply({ status: "healthy" });
    if (path === "/api/skill-changes") return reply([]);
    if (path === "/api/import" && method === "POST") {
      state.imports.push({ bytes: request.postDataBuffer() ?? Buffer.alloc(0), contentType: request.headers()["content-type"] || "" });
      return reply({ skills: 1, artefacts: 0 });
    }
    if (path === "/api/skills" && method === "GET") return reply([...state.skills].reverse());
    if (path === "/api/skills" && method === "POST") {
      const skill = { ...state.skills[0], ...body, id: "skill-created", rules: [], artefacts: [], versions: [] };
      state.skills.push(skill); return reply(skill);
    }
    if (path.startsWith("/api/skills/")) {
      const segments = path.split("/"); const skill = state.skills.find((entry) => entry.id === segments[3]);
      if (!skill) return reply({ detail: "Skill not found" }, 404);
      if (segments[4] === "document" && method === "GET") return reply(document(skill));
      if (segments[4] === "document" && method === "PUT") {
        if (body.revision !== document(skill).revision) return reply({ detail: "This document has changed. Reload it before saving your edits." }, 409);
        for (const part of body.parts as { anchor: string; text: string }[]) {
          if (part.anchor === "block:introduction") skill.sections![0].text = part.text;
          else if (part.anchor === "title") skill.name = part.text;
          else return reply({ detail: "This document part cannot be edited." }, 422);
        }
        state.documentSaves.push({ id: skill.id, ...body });
        state.documentRevisions[skill.id] = (state.documentRevisions[skill.id] ?? 0) + 1;
        skill.body = `# ${skill.name}\n\n${skill.sections![0].text}`;
        return reply(document(skill));
      }
      if (segments[4] === "versions" && method === "GET") return reply(skill.versions ?? []);
      if (segments.length === 4 && method === "PATCH") {
        Object.assign(skill, body);
        state.documentRevisions[skill.id] = (state.documentRevisions[skill.id] ?? 0) + 1;
        return reply(skill);
      }
      if (segments.length === 4 && method === "GET") return reply(skill);
    }
    if (path === "/api/review") return reply([...state.items].reverse());
    if (path.startsWith("/api/review/") && method === "POST") {
      const id = path.split("/")[3];
      if (state.failNextDecision) { state.failNextDecision = false; return reply({ detail: "The decision could not be saved. Try again." }, 503); }
      state.decisions.push({ id, ...body });
      if (body.action === "release_safety") {
        const item = state.items.find((entry) => entry.txn_id === id)!;
        item.state = "awaiting_manual_review"; item.held_reason = undefined;
      } else state.items = state.items.filter((item) => item.txn_id !== id);
      return reply({ state: body.action === "release_safety" ? "awaiting_manual_review" : body.action === "reject" ? "rejected" : "applied" });
    }
    state.violations.push(`Unhandled ${method} ${path}`);
    return reply({ detail: "Uncovered scale fixture operation" }, 500);
  });
  return state;
}

test("240 skills remain searchable and paged while detail drafts survive view changes", async ({ page }) => {
  const state = await largeWorkspace(page);
  await page.goto("/skills");
  const list = page.getByRole("list", { name: "Skills", exact: true });
  const rows = list.locator(".oms-collection-list__row");
  await expect(rows).toHaveCount(24);
  await expect(rows.first()).toContainText("Skill 001");
  await expect(page.getByRole("button", { name: "Compact list", exact: true })).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("combobox", { name: "Skills page", exact: true }).selectOption("10");
  await expect(rows.last()).toContainText("Skill 240");
  await page.getByRole("button", { name: "Cards", exact: true }).click();
  await expect(page.locator(".skill-card")).toHaveCount(24);
  await expect(page.getByRole("combobox", { name: "Skills page", exact: true })).toHaveValue("10");
  await page.getByRole("button", { name: /^Skill 239/ }).click();
  await page.getByRole("button", { name: "Edit details", exact: true }).click();
  await page.getByRole("textbox", { name: "Description", exact: true }).fill("An unsaved description for the last library page.");
  await page.getByRole("button", { name: "Edit details", exact: true }).click();
  await page.getByRole("button", { name: "Edit details", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Description", exact: true })).toHaveValue("An unsaved description for the last library page.");
  await page.getByRole("tab", { name: "Edit document", exact: true }).click();
  const prose = page.locator('[data-anchor="block:introduction"]').getByRole("textbox");
  await prose.fill("An unsaved section kept while browsing.");
  await page.getByRole("tab", { name: "Preview", exact: true }).click();
  await page.getByRole("tab", { name: "Edit document", exact: true }).click();
  await expect(prose).toHaveValue("An unsaved section kept while browsing.");
  await page.getByRole("button", { name: "All skills" }).click();
  await expect(page.getByRole("combobox", { name: "Skills page", exact: true })).toHaveValue("10");
  await page.getByRole("searchbox", { name: "Find a skill", exact: true }).fill("no-matching-skill");
  await expect(page.getByRole("heading", { name: "No skills match your filters", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Clear skill filters", exact: true }).click();
  await page.getByRole("button", { name: "Compact list", exact: true }).click();
  await page.getByRole("combobox", { name: "Skills page", exact: true }).selectOption("10");
  await list.getByRole("button", { name: /^Skill 239/ }).click();
  await page.getByRole("button", { name: "Edit details", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Description", exact: true })).toHaveValue("An unsaved description for the last library page.");
  await page.getByRole("button", { name: "Save details", exact: true }).click();
  await expect.poll(() => state.skills.find((skill) => skill.id === "skill-239")?.description).toBe("An unsaved description for the last library page.");
  await page.getByRole("tab", { name: "Edit document", exact: true }).click();
  await expect(prose).toHaveValue("An unsaved section kept while browsing.");
  await expect(page.getByRole("alert").filter({ hasText: "The document changed while you were editing" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save document", exact: true })).toBeDisabled();
  expect(state.documentSaves).toEqual([]);
  await page.getByRole("button", { name: "Discard document edits", exact: true }).click();
  await prose.fill("An unsaved section kept while browsing.");
  await page.getByRole("button", { name: "Save document", exact: true }).click();
  await expect.poll(() => state.skills.find((skill) => skill.id === "skill-239")?.sections?.[0].text).toBe("An unsaved section kept while browsing.");
  await page.getByRole("button", { name: "All skills" }).click();
  await page.getByRole("combobox", { name: "Filter by domain", exact: true }).selectOption("operations");
  await expect(page.getByRole("group", { name: "Skills pagination", exact: true })).toContainText("120 skills");
  await expect(rows.first()).toContainText("Skill 002");
  await page.getByRole("searchbox", { name: "Find a skill", exact: true }).fill("Skill 240");
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText("Skill 240");
  await expect(page.getByRole("group", { name: "Skills pagination", exact: true })).toHaveText("1–1 of 1 skill");
  await page.getByRole("button", { name: "Clear skill filters", exact: true }).click();
  await page.getByRole("combobox", { name: "Sort skills", exact: true }).selectOption("name-desc");
  await expect(rows.first()).toContainText("Skill 240");
  await page.screenshot({ path: "test-results/community-library-scale.png", fullPage: true });
  expect(state.violations).toEqual([]);
});

test("new skill and ZIP selections survive library controls and form toggles", async ({ page }) => {
  const state = await largeWorkspace(page);
  await page.goto("/skills");
  await page.getByRole("button", { name: "Create skill", exact: true }).click();
  await page.getByRole("textbox", { name: "Name", exact: true }).fill("Zebra final-page guidance");
  await page.getByRole("textbox", { name: "Description", exact: true }).fill("Keep this creation draft.");
  await page.getByRole("button", { name: "Upload skills", exact: true }).click();
  await page.getByLabel("Skill ZIP package", { exact: true }).setInputFiles({ name: "retained.zip", mimeType: "application/zip", buffer: Buffer.from("fixture") });
  await page.getByRole("button", { name: "Create skill", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Name", exact: true })).toHaveValue("Zebra final-page guidance");
  await page.getByRole("button", { name: "Next skills page", exact: true }).click();
  await page.getByRole("button", { name: "Cards", exact: true }).click();
  await expect(page.getByRole("textbox", { name: "Description", exact: true })).toHaveValue("Keep this creation draft.");
  await page.getByRole("button", { name: "Upload skills", exact: true }).click();
  await expect(page.getByText(/retained.zip ·/)).toBeVisible();
  await page.getByRole("button", { name: "Import package", exact: true }).click();
  await expect(page.getByText("Skill package imported.", { exact: true })).toBeVisible();
  await expect.poll(() => state.imports.length).toBe(1);
  expect(state.imports[0].contentType).toContain("multipart/form-data; boundary=");
  expect(state.imports[0].bytes.toString()).toContain('filename="retained.zip"');
  expect(state.imports[0].bytes.toString()).toContain("\r\n\r\nfixture\r\n");
  await page.getByRole("button", { name: "Create skill", exact: true }).click();
  await page.getByRole("button", { name: "Save new skill", exact: true }).click();
  await expect(page.getByText("Skill created.", { exact: true })).toBeVisible();
  await expect.poll(() => state.skills.length).toBe(241);
  await page.getByRole("searchbox", { name: "Find a skill", exact: true }).fill("Zebra");
  await expect(page.getByRole("button", { name: /^Zebra final-page guidance/ })).toBeVisible();
  expect(state.violations).toEqual([]);
});

test("201 collapsed corrections retain drafts across cards, pages, filters and failed saves", async ({ page }) => {
  const state = await largeWorkspace(page);
  await page.goto("/inbox");
  const queue = page.getByRole("region", { name: "Correction list", exact: true });
  const summaries = queue.locator(".correction-card__summary");
  await expect(summaries).toHaveCount(20);
  await expect(summaries.first()).toContainText("Correction 001");
  await expect(page.locator(".oms-manual-card")).toHaveCount(0);
  expect(state.requests.filter(request => request.endsWith("/document"))).toEqual([]);
  await summaries.first().click();
  await page.getByRole("tab", { name: "Add or confirm a rule", exact: true }).click();
  const targets = page.getByRole("region", { name: "Target skills", exact: true });
  await expect(targets.getByRole("checkbox")).toHaveCount(8);
  const wording = "Retained wording for a decision on a large queue.";
  const editor = page.getByRole("textbox", { name: "Proposed rule wording", exact: true });
  await editor.fill(wording);
  await page.getByRole("checkbox", { name: "Skill 001", exact: true }).check();
  const targetSearch = page.getByRole("searchbox", { name: "Find target skills", exact: true });
  await targetSearch.fill("Skill 239");
  await expect(targets.getByRole("checkbox")).toHaveCount(1);
  await page.getByRole("checkbox", { name: "Skill 239", exact: true }).check();
  await page.getByRole("combobox", { name: "Corrections page", exact: true }).selectOption("2");
  await expect(editor).toHaveValue(wording);
  await page.getByRole("button", { name: /^Review correction: Correction 021:/ }).click();
  await expect(page.locator(".oms-manual-card")).toHaveCount(1);
  await page.getByRole("tab", { name: "Add or confirm a rule", exact: true }).click();
  await expect(editor).toHaveValue("Correction 021: verify the recorded evidence.");
  await page.getByRole("searchbox", { name: "Find a correction", exact: true }).fill("correction-001");
  await page.getByRole("button", { name: /^Review correction: Correction 001:/ }).click();
  await expect(editor).toHaveValue(wording);
  await expect(page.getByRole("checkbox", { name: "Skill 001", exact: true })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "Skill 239", exact: true })).toBeChecked();
  await page.getByRole("searchbox", { name: "Find a correction", exact: true }).fill("no-such-correction");
  await expect(page.getByRole("heading", { name: "No corrections match your filters", exact: true })).toBeVisible();
  await expect(editor).toHaveValue(wording);
  await page.getByRole("button", { name: "Clear correction filters", exact: true }).click();
  await page.getByRole("combobox", { name: "Corrections page", exact: true }).selectOption("11");
  await page.getByRole("button", { name: /^Review correction: Correction 201:/ }).click();
  await page.getByRole("button", { name: "Reject correction", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "Corrections page", exact: true })).toHaveValue("10");
  await expect(summaries).toHaveCount(20);
  await page.getByRole("searchbox", { name: "Find a correction", exact: true }).fill("correction-001");
  await page.getByRole("button", { name: /^Review correction: Correction 001:/ }).click();
  state.failNextDecision = true;
  await page.getByRole("button", { name: "Create rule", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "The decision could not be saved" })).toBeVisible();
  await expect(editor).toHaveValue(wording);
  await page.getByRole("button", { name: "Create rule", exact: true }).click();
  await expect.poll(() => state.decisions.length).toBe(2);
  expect(state.decisions[1]).toEqual({ id: "correction-001", action: "create", body: wording, skill_ids: ["skill-001", "skill-239"] });
  await page.getByRole("button", { name: "Clear correction filters", exact: true }).click();
  await expect(page.getByRole("group", { name: "Corrections pagination", exact: true })).toContainText("199 corrections");
  await expect(summaries.first()).toContainText("Correction 002");
  await page.screenshot({ path: "test-results/community-inbox-scale.png", fullPage: true });
  expect(state.violations).toEqual([]);
});

test("safety filters and phone cards reveal the complete correction", async ({ page }) => {
  const state = await largeWorkspace(page, { held: [1, 101], longCorrection: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/inbox");
  const summaries = page.locator(".correction-card__summary");
  await expect(summaries).toHaveCount(20);
  const rowTitle = page.locator(".correction-card__title").first();
  expect(await rowTitle.evaluate(element => element.getBoundingClientRect().height)).toBeLessThan(65);
  await page.getByRole("combobox", { name: "Review status", exact: true }).selectOption("held");
  await expect(summaries).toHaveCount(2);
  const first = page.getByRole("button", { name: /^Review correction: Correction 001:/ });
  await first.click();
  const editor = page.getByRole("region", { name: "Review selected correction", exact: true });
  await expect(editor.locator(".oms-correction-context blockquote")).toHaveText(state.items[0].text);
  await page.getByRole("button", { name: "Release for manual review", exact: true }).click();
  await expect(page.getByRole("tab", { name: "Add or confirm a rule", exact: true })).toBeVisible();
  expect(state.decisions).toHaveLength(1);
  expect(state.decisions[0].action).toBe("release_safety");
  await first.click();
  await expect(editor).toHaveCount(0);
  await page.getByRole("combobox", { name: "Review status", exact: true }).selectOption("review");
  await page.getByRole("combobox", { name: "Filter by skill", exact: true }).selectOption("skill-200");
  await expect(summaries).toHaveCount(1);
  await expect(summaries).toContainText("Correction 200");
  await expect(page.getByRole("group", { name: "Corrections pagination", exact: true })).toHaveText("1–1 of 1 correction");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/community-inbox-mobile-scale.png", fullPage: true });
  expect(state.violations).toEqual([]);
});

test("context, mode tabs and centred search support a long skill without losing drafts", async ({ page }) => {
  const state = await largeWorkspace(page);
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  state.items = [{ ...state.items[0], text: "Treat compatible field type changes as non-breaking.",
    candidate_skill_ids: ["skill-001"], skill_hint: "Skill 001", repo: "github.com/acme/api", posted_at: "2026-09-22T09:00:00Z",
    source_agent_id: "local-agent", source_runtime: "mcp", context: { user_input: "Review this API change.", agent_output: "Changing field types breaks every client." } }];
  const prose = "Read the full compatibility guidance before making a decision. ".repeat(100)
    + "**Changing field types** should be reviewed against client capabilities. "
    + "Retain endpoint removal checks and document API changes. ".repeat(100);
  state.skills[0].sections![0].text = prose;
  await page.goto("/inbox");
  const card = page.locator(".correction-card__summary");
  await expect(card).toHaveAttribute("aria-expanded", "false");
  await card.click();
  const context = page.getByRole("region", { name: "Posted correction and context", exact: true });
  await expect(context).toContainText("github.com/acme/api");
  await expect(context.locator("time")).toHaveAttribute("datetime", "2026-09-22T09:00:00Z");
  await context.getByText("Source and interaction context", { exact: true }).click();
  await expect(context).toContainText("Review this API change.");
  await expect(context).toContainText("Changing field types breaks every client.");
  await context.getByText("Source and interaction context", { exact: true }).click();
  const editTab = page.getByRole("tab", { name: "Edit skill text", exact: true });
  await expect(editTab).toHaveAttribute("aria-selected", "true");
  await editTab.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Add or confirm a rule", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("button", { name: "Change target skills", exact: true })).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("searchbox", { name: "Find target skills", exact: true })).toHaveCount(0);
  await page.keyboard.press("ArrowLeft");
  const field = page.getByRole("textbox", { name: "Skill 001 · Passage 1", exact: true });
  await expect(field).toHaveValue(prose);
  const search = page.getByRole("searchbox", { name: "Find in skill", exact: true });
  await search.fill("Changing field types");
  await expect(search).toBeFocused();
  await expect(page.getByText("1 of 1 matches", { exact: true })).toBeVisible();
  const pane = page.getByRole("region", { name: "Skill text editor", exact: true });
  const centreDistance = () => pane.evaluate(element => {
    const mark = element.querySelector("mark.is-active") || element.querySelector("mark");
    const hit = mark!.getBoundingClientRect(); const frame = element.getBoundingClientRect();
    return Math.abs(hit.top + Math.min(hit.height, 24) / 2 - frame.top - element.clientHeight / 2);
  });
  await expect.poll(centreDistance).toBeLessThan(25);
  await expect.poll(() => pane.evaluate(element => {
    const mark = element.querySelector("mark.is-active") || element.querySelector("mark");
    const hit = mark!.getBoundingClientRect();
    return hit.top > 40 && hit.bottom < window.innerHeight - 40;
  })).toBe(true);
  await expect.poll(() => pane.evaluate(element => element.scrollTop)).toBeGreaterThan(500);
  await page.getByRole("group", { name: "Skill view", exact: true }).getByRole("button", { name: "Preview", exact: true }).click();
  await expect(pane.getByRole("textbox")).toHaveCount(0);
  await expect(pane.locator("strong")).toHaveText("Changing field types");
  await expect.poll(centreDistance).toBeLessThan(25);
  await expect.poll(() => pane.evaluate(element => {
    const mark = element.querySelector("mark.is-active") || element.querySelector("mark");
    const hit = mark!.getBoundingClientRect();
    return hit.top > 40 && hit.bottom < window.innerHeight - 40;
  })).toBe(true);
  await page.screenshot({ path: "test-results/correction-search-preview-desktop.png", fullPage: true });
  await page.getByRole("button", { name: "Markdown", exact: true }).click();
  await expect.poll(centreDistance).toBeLessThan(25);
  await expect.poll(() => pane.evaluate(element => {
    const mark = element.querySelector("mark.is-active") || element.querySelector("mark");
    const hit = mark!.getBoundingClientRect();
    return hit.top > 40 && hit.bottom < window.innerHeight - 40;
  })).toBe(true);
  await search.clear();
  await field.fill(prose.replace("should be reviewed", "must be assessed"));
  await page.getByRole("group", { name: "Skill view", exact: true }).getByRole("button", { name: "Preview", exact: true }).click();
  await expect(pane).toContainText("must be assessed");
  await card.click();
  await expect(page.locator(".oms-manual-card")).toHaveCount(0);
  await expect(card).toContainText("Draft retained");
  await card.click();
  await expect(field).toHaveValue(prose.replace("should be reviewed", "must be assessed"));
  await page.setViewportSize({ width: 390, height: 844 });
  await search.fill("Changing field types");
  await expect.poll(centreDistance).toBeLessThan(25);
  await expect.poll(() => pane.evaluate(element => {
    const mark = element.querySelector("mark.is-active") || element.querySelector("mark");
    const hit = mark!.getBoundingClientRect();
    return hit.top > 40 && hit.bottom < window.innerHeight - 40;
  })).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/correction-search-visible-mobile.png" });
  await expect.poll(centreDistance).toBeLessThan(25);
  await page.screenshot({ path: "test-results/correction-search-markdown-mobile.png", fullPage: true });
  expect(state.decisions).toEqual([]);
  expect(errors).toEqual([]);
  expect(state.violations).toEqual([]);
});
