import { expect, test, type Page } from "@playwright/test";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { allowedRequest } from "../e2e/route-contract.mjs";
import type { Skill } from "@inogen/oms-client";

const API = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || 4317}`;
const UI = `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || 4318}`;
const wording = "Check every expense total against its receipt before submitting the claim.";
const rejected = "Always submit expenses without checking their receipts.";
const root = process.env.OMS_BROWSER_TEST_DATA!;

function files(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => entry.isDirectory() ? files(join(directory, entry.name)) : [join(directory, entry.name)]);
}

async function contribute(page: Page, text: string) {
  await page.getByRole("button", { name: "Submit a correction", exact: true }).click();
  await page.getByLabel("Correction", { exact: true }).fill(text);
  await page.getByLabel("Skill hint").selectOption({ label: "Expense Review" });
  const saved = page.waitForResponse((response) => response.url() === `${API}/api/ingest` && response.request().method() === "POST");
  await page.getByRole("button", { name: "Send to inbox" }).click();
  const response = await saved;
  expect(response.status()).toBe(202);
  const contribution = await response.json();
  await page.getByRole("button", { name: `Review correction: ${text}`, exact: true }).click();
  return contribution.transaction_id as string;
}

test("real installed core imports, captures, decides, preserves lineage and publishes original files", async ({ page }) => {
  const violations: string[] = [];
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(error.message));
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === UI && !url.pathname.startsWith("/api/")) return route.continue();
    if (url.origin === API && (request.method() === "OPTIONS" || allowedRequest(request.method(), url.pathname))) return route.continue();
    violations.push(`${request.method()} ${url.origin}${url.pathname}`);
    return route.abort();
  });
  expect(JSON.parse(readFileSync(join(root, "installed-core.json"), "utf8")).origin).toContain("site-packages");
  await page.goto("/skills");
  await page.getByRole("button", { name: "Upload skills", exact: true }).click();
  await page.getByLabel("Skill ZIP package").setInputFiles(join(root, "skills.zip"));
  await page.getByRole("button", { name: "Import package", exact: true }).click();
  await expect(page.getByText("Skill package imported.", { exact: true })).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Skills", exact: true }).click();
  const initialResponse = page.waitForResponse((response) => response.url().startsWith(`${API}/api/skills/`) && response.request().method() === "GET");
  await page.getByRole("button", { name: /^Expense Review/ }).click();
  const initial = await (await initialResponse).json() as Skill;
  await page.getByRole("tab", { name: /^Files/ }).click();
  await expect(page.getByText("references/policy.md", { exact: true })).toBeVisible();
  await expect(page.getByText("scripts/check.sh", { exact: true })).toBeVisible();

  await page.getByRole("navigation").getByRole("link", { name: "Correction inbox" }).click();
  const createdId = await contribute(page, "Check the expense total before submitting the claim.");
  await page.getByRole("tab", { name: "Add or confirm a rule", exact: true }).click();
  await page.getByLabel("Proposed rule wording").fill(wording);
  await expect(page.getByRole("button", { name: "Change target skills", exact: true })).toHaveAttribute("aria-expanded", "false");
  await page.getByRole("button", { name: "Create rule", exact: true }).click();
  await expect(page.getByRole("heading", { name: "You’re all caught up" })).toBeVisible();

  const reinforcedId = await contribute(page, wording);
  await page.getByRole("tab", { name: "Add or confirm a rule", exact: true }).click();
  await page.getByRole("radio", { name: `Select rule: ${wording}`, exact: true }).check();
  await page.getByRole("button", { name: "Reinforce existing rule" }).click();
  await expect(page.getByRole("heading", { name: "You’re all caught up" })).toBeVisible();

  const rejectedId = await contribute(page, rejected);
  await page.getByRole("button", { name: "Reject correction" }).click();
  await expect(page.getByRole("heading", { name: "You’re all caught up" })).toBeVisible();

  await page.getByRole("navigation").getByRole("link", { name: "Skills", exact: true }).click();
  const updatedResponse = page.waitForResponse((response) => response.url() === `${API}/api/skills/${encodeURIComponent(initial.id)}`);
  await page.getByRole("button", { name: /^Expense Review/ }).click();
  const updated = await (await updatedResponse).json() as Skill;
  expect(updated.versions!.length).toBeGreaterThan(initial.versions!.length);
  const learned = updated.rules!.find((rule) => rule.body === wording)!;
  expect(learned.corroboration_count).toBe(2);
  expect(learned.transaction_ids).toEqual(expect.arrayContaining([createdId, reinforcedId]));
  await page.getByRole("tab", { name: "Edit document", exact: true }).click();
  await page.getByText("Correction evidence", { exact: true }).last().click();
  await expect(page.getByText(`Corrections: ${createdId}, ${reinforcedId}`, { exact: true })).toBeVisible();
  await expect(page.getByText(rejected, { exact: true })).toHaveCount(0);
  await expect(page.getByText(rejectedId)).toHaveCount(0);
  await page.getByRole("tab", { name: "History", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Saved versions", exact: true })).toBeVisible();

  await page.getByRole("navigation").getByRole("link", { name: "Preview & publish" }).click();
  await page.getByRole("combobox", { name: "Skill", exact: true }).selectOption(initial.id);
  await page.getByRole("button", { name: "Preview skill", exact: true }).click();
  await expect(page.getByLabel("Published skill preview")).toContainText(wording);
  expect((await page.getByLabel("Published skill preview").innerText()).split(wording)).toHaveLength(2);
  await expect(page.getByLabel("Published skill preview")).toContainText("Keep clear records for every expense claim.");
  await expect(page.getByLabel("Published skill preview")).not.toContainText(rejected);
  await page.getByRole("button", { name: "Publish skills", exact: true }).click();
  await expect(page.getByText("Workspace published.", { exact: true })).toBeVisible();
  const published = files(join(root, "state/published"));
  const document = published.find((file) => file.endsWith("/SKILL.md") && readFileSync(file, "utf8").includes("# Expense Review"))!;
  expect(readFileSync(document, "utf8").split(wording)).toHaveLength(2);
  expect(readFileSync(published.find((file) => file.endsWith("/references/policy.md"))!)).toEqual(readFileSync(join(root, "reference.original")));
  expect(readFileSync(join(dirname(document), "scripts/check.sh"))).toEqual(readFileSync(join(root, "script.original")));
  expect(existsSync(join(root, "script-must-not-run"))).toBe(false);

  await page.getByRole("navigation").getByRole("link", { name: "Constraints" }).click();
  await page.getByLabel("Required guidance").fill("Keep expense amounts in their original currency.");
  const constraintSaved = page.waitForResponse((response) => response.url() === `${API}/api/constraints` && response.request().method() === "POST");
  await page.getByRole("button", { name: "Add constraint" }).click();
  const constraint = await (await constraintSaved).json();
  const record = page.getByRole("article", { name: `Constraint ${constraint.id}` });
  await record.getByRole("button", { name: "Edit constraint" }).click();
  await record.getByLabel("Constraint wording").fill("Preserve the original expense currency in every record.");
  await record.getByRole("button", { name: "Save constraint" }).click();
  await expect(record.getByText("Preserve the original expense currency in every record.", { exact: true })).toBeVisible();
  await record.getByRole("button", { name: "Retire constraint" }).click();
  await expect(record).toBeHidden();
  await page.locator(".constraint-archive summary").click();
  await expect(record.getByRole("button", { name: "Restore constraint" })).toBeVisible();
  await record.getByRole("button", { name: "Restore constraint" }).click();
  await expect(record.getByRole("button", { name: "Retire constraint" })).toBeVisible();
  await expect(page.locator(".constraint-archive")).toHaveCount(0);

  // A source change needs a decision only after the same prose was also
  // amended locally; ordinary source-only changes remain an automatic import.
  await page.getByRole("navigation").getByRole("link", { name: "Skills", exact: true }).click();
  await page.getByRole("button", { name: /^Expense Review/ }).click();
  await page.getByRole("tab", { name: "Edit document", exact: true }).click();
  const editor = page.locator("textarea").filter({ hasText: "Use the expense review checklist for each claim." });
  await expect(editor).toHaveValue(/Use the expense review checklist for each claim\./);
  await editor.fill("Use the operator-reviewed checklist for every expense claim.");
  await page.getByRole("button", { name: "Save document", exact: true }).click();
  await expect(page.getByText("Document saved.", { exact: true })).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Import", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Bring your existing skills", exact: true })).toBeVisible();
  await page.getByLabel("Skill ZIP package").setInputFiles(join(root, "skills-updated.zip"));
  await page.getByRole("button", { name: "Import package", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Source text changed" })).toBeVisible();
  await page.getByRole("button", { name: "Apply source text", exact: true }).click();
  await expect(page.getByRole("heading", { name: "No import decisions waiting" })).toBeVisible();
  await page.getByRole("navigation").getByRole("link", { name: "Preview & publish" }).click();
  await page.getByRole("combobox", { name: "Skill", exact: true }).selectOption(initial.id);
  await page.getByRole("button", { name: "Preview skill", exact: true }).click();
  await expect(page.getByLabel("Published skill preview")).toContainText("Use the full expense review checklist for each claim.");
  await page.getByRole("button", { name: "Publish skills", exact: true }).click();
  await expect(page.getByText("Workspace published.", { exact: true })).toBeVisible();
  expect(readFileSync(document, "utf8").split(wording)).toHaveLength(2);
  expect(readFileSync(join(root, "state/published/AGENTS.md"), "utf8")).toContain("Preserve the original expense currency in every record.");
  expect(readFileSync(published.find((file) => file.endsWith("/references/policy.md"))!)).toEqual(readFileSync(join(root, "reference.original")));
  expect(readFileSync(join(dirname(document), "scripts/check.sh"))).toEqual(readFileSync(join(root, "script.original")));
  expect(existsSync(join(root, "script-must-not-run"))).toBe(false);
  await page.screenshot({ path: "test-results/community-real-publication.png", fullPage: true });
  expect(violations).toEqual([]);
  expect(failures).toEqual([]);
});
