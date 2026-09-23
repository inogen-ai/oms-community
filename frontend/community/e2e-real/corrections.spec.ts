import { test, expect } from "@playwright/test";

const API = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || 4317}`;
const UI = `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || 4318}`;
const headers = { Origin: UI };

test("clearing a contradicted rule can be previewed, applied and restored from history", async ({ page }) => {
  const oldRule = "Changing field types requires a version bump.";
  const otherRule = "Removing endpoints requires a version bump.";
  const name = "Rule Removal Guide";
  const imported = await page.request.post(`${API}/api/import/directory`, { headers, multipart: {
    files: { name: "rule-removal-guide/SKILL.md", mimeType: "text/markdown", buffer: Buffer.from(
      `---\nname: ${name}\ndescription: Review API compatibility.\n---\n\n# ${name}\n\n## Rules\n\n### Breaking changes\n\n* ${oldRule}\n* ${otherRule}\n`) },
  } });
  expect(imported.ok()).toBeTruthy();
  const skills = await (await page.request.get(`${API}/api/skills`)).json();
  const skill = skills.find((row: { name: string }) => row.name === name);
  const path = `${API}/api/skills/${encodeURIComponent(skill.id)}`;
  const before = await (await page.request.get(`${path}/document`)).json();
  const anchor = before.parts.find((part: { edit_text: string }) => part.edit_text === oldRule).anchor;
  const versions = await (await page.request.get(`${path}/versions`)).json();
  const correction = "Changing field types is not a breaking change.";
  expect((await page.request.post(`${API}/api/ingest`, { headers, data: { transaction_id: "browser-rule-removal", correction, skill_hint: skill.id } })).status()).toBe(202);
  await page.goto("/inbox");
  await page.getByRole("button", { name: `Review correction: ${correction}`, exact: true }).click();
  await page.locator(`[data-anchor="${anchor}"]`).getByRole("textbox").clear();
  await expect(page.getByText("Marked for removal", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  const preview = page.getByRole("region", { name: "Correction change preview", exact: true });
  await expect(preview).toContainText("Remove rule");
  await expect(preview).toContainText(oldRule);
  const apply = preview.getByRole("button", { name: "Apply change and resolve correction", exact: true });
  await expect(apply).toBeEnabled();
  await apply.click();
  await expect(page.getByText("Guidance updated and correction resolved. Publish to update your agents.", { exact: true })).toBeVisible();
  const detail = await (await page.request.get(path)).json();
  expect(detail.body).not.toContain(oldRule);
  expect(detail.body).toContain(otherRule);
  const retired = detail.rules.find((rule: { body: string }) => rule.body === oldRule);
  expect(retired.status).toBe("retired");
  expect(retired.transaction_ids).toContain("browser-rule-removal");
  const historyPath = `${path}/versions/${versions[0].id}`;
  const comparison = await (await page.request.get(`${historyPath}/compare`)).json();
  const removed = comparison.rows.find((row: { anchor: string }) => row.anchor === anchor);
  expect(removed.restorable).toBe(true);
  expect((await page.request.post(`${historyPath}/restore`, { headers, data: { revision: comparison.revision, anchors: [anchor] } })).ok()).toBeTruthy();
  expect((await (await page.request.get(path)).json()).body).toContain(oldRule);
});

test("edit a breaking-change rule and its repeated explanation directly in the whole skill", async ({ page }) => {
  const correction = "Treat compatible field type changes as non-breaking.";
  const prose = "### Breaking Change Detection\n- **Type Changes**: Catches field type modifications that could break clients\n- **Endpoint Removal**: Detects removed endpoints";
  const updatedProse = "### Breaking Change Detection\n- **Type Changes**: Flag only field type changes unsupported by existing clients\n- **Endpoint Removal**: Detects removed endpoints";
  const imported = await page.request.post(`${API}/api/import/directory`, { headers, multipart: {
    files: { name: "field-type-guide/SKILL.md", mimeType: "text/markdown", buffer: Buffer.from(
      `---\nname: Field Type Guide\ndescription: Review API compatibility.\n---\n\n# Field Type Guide\n\n## Core Capabilities\n\n${prose}\n\n## Rules\n\n### Breaking changes\n\n* Changing field types\n* Removing endpoints\n`) },
  } });
  expect(imported.ok()).toBeTruthy();
  const skills = await (await page.request.get(`${API}/api/skills`)).json();
  const skill = skills.find((row: { name: string }) => row.name === "Field Type Guide");
  const skillPath = `${API}/api/skills/${encodeURIComponent(skill.id)}`;
  const before = await (await page.request.get(`${skillPath}/document`)).json();
  const beforeDetail = await (await page.request.get(skillPath)).json();
  const rulePart = before.parts.find((part: { edit_text: string }) => part.edit_text === "Changing field types");
  const prosePart = before.parts.find((part: { edit_text: string }) => part.edit_text?.includes("### Breaking Change Detection"));
  expect((await page.request.post(`${API}/api/ingest`, { headers, data: { transaction_id: "browser-field-types", correction } })).status()).toBe(202);
  await page.goto("/inbox");
  await page.getByRole("button", { name: `Review correction: ${correction}`, exact: true }).click();
  const review = page.getByRole("region", { name: "Review selected correction", exact: true });
  await expect(review.getByRole("combobox")).toHaveCount(0);
  await review.getByRole("searchbox", { name: "Find a skill to edit", exact: true }).fill("Field Type Guide");
  await review.getByRole("button", { name: "Open skill: Field Type Guide", exact: true }).click();
  const rule = review.locator(`[data-anchor="${rulePart.anchor}"]`).getByRole("textbox");
  const explanation = review.locator(`[data-anchor="${prosePart.anchor}"]`).getByRole("textbox");
  await expect(rule).toHaveValue("Changing field types");
  await expect(explanation).toHaveValue(prosePart.edit_text);
  await review.getByRole("searchbox", { name: "Find in skill", exact: true }).fill("Changing field types");
  await expect(review.getByText("1 of 1 matches", { exact: true })).toBeVisible();
  await expect(review.getByRole("searchbox", { name: "Find in skill", exact: true })).toBeFocused();
  await expect(review.getByRole("button", { name: "Replace selection with correction", exact: true })).toBeEnabled();
  await review.getByRole("button", { name: "Replace selection with correction", exact: true }).click();
  await expect(rule).toHaveValue(correction);
  await explanation.fill(updatedProse);
  await expect(review.getByText("2 unsaved edits", { exact: true })).toBeVisible();
  await expect(review.getByRole("combobox")).toHaveCount(0);
  await page.screenshot({ path: "test-results/skill-text-editor-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect.poll(() => review.locator("textarea").evaluateAll(fields => fields.every(field => field.scrollHeight <= field.clientHeight + 2))).toBe(true);
  await page.screenshot({ path: "test-results/skill-text-editor-mobile.png", fullPage: true });
  await review.getByRole("button", { name: "Preview changes", exact: true }).click();
  const preview = page.getByRole("region", { name: "Correction change preview", exact: true });
  await expect(preview).toContainText("Changing field types");
  await expect(preview).toContainText("Catches field type modifications");
  await expect(preview).toContainText(correction);
  await review.getByRole("button", { name: "Apply change and resolve correction", exact: true }).click();
  await expect(page.getByText("Guidance updated and correction resolved. Publish to update your agents.", { exact: true })).toBeVisible();
  const after = await (await page.request.get(skillPath)).json();
  expect(after.rules).toHaveLength(beforeDetail.rules.length);
  expect(after.body).toContain(correction);
  expect(after.body).toContain("Flag only field type changes unsupported by existing clients");
  expect(after.body).toContain("Detects removed endpoints");
  expect(after.body).not.toContain("Catches field type modifications");
  expect(after.body).not.toContain("* Changing field types");
  expect(after.rules.find((rule: { id: string }) => rule.id === rulePart.source_id).transaction_ids).toContain("browser-field-types");
  const versions = await (await page.request.get(`${skillPath}/versions`)).json();
  expect(versions.filter((version: { detail: string }) => version.detail?.includes("browser-field-types"))).toHaveLength(1);
});

test("declining corroboration offers an explicit in-place update, never an appended rule", async ({ page }) => {
  const original = "Always run the full test suite after every change.";
  const correction = "Run the full test suite after code changes. For documentation-only changes, run documentation checks.";
  const imported = await page.request.post(`${API}/api/import/directory`, { headers, multipart: {
    files: { name: "match-choice-guide/SKILL.md", mimeType: "text/markdown", buffer: Buffer.from(
      `---\nname: Match Choice Guide\ndescription: Choose the test suite for code and documentation changes.\n---\n\n# Match Choice Guide\n\n## Rules\n\n### Before completing a change\n\n* ${original}\n* Keep the final diff for review.\n`) },
  } });
  expect(imported.ok()).toBeTruthy();
  const skills = await (await page.request.get(`${API}/api/skills`)).json();
  const skill = skills.find((row: { name: string }) => row.name === "Match Choice Guide");
  const skillPath = `${API}/api/skills/${encodeURIComponent(skill.id)}`;
  const before = await (await page.request.get(skillPath)).json();
  const oldRule = before.rules.find((rule: { body: string }) => rule.body === original);
  const document = await (await page.request.get(`${skillPath}/document`)).json();
  expect((await page.request.post(`${API}/api/ingest`, { headers, data: { transaction_id: "browser-match-choice", correction, skill_hint: skill.id } })).status()).toBe(202);
  await page.goto("/inbox");
  await page.getByRole("button", { name: `Review correction: ${correction}`, exact: true }).click();
  await page.getByRole("tab", { name: "Add or confirm a rule", exact: true }).click();
  await expect(page.getByRole("button", { name: "Change target skills", exact: true })).toHaveAttribute("aria-expanded", "false");
  const context = page.getByRole("article", { name: `Existing rule: ${original}`, exact: true }).getByRole("region", { name: "Rule context in Match Choice Guide", exact: true });
  await expect(context.getByText("Rules › Before completing a change", { exact: true })).toBeVisible();
  await expect(context.getByText("Keep the final diff for review.", { exact: true })).not.toBeVisible();
  await context.getByText("Show full section", { exact: true }).click();
  await expect(context.getByRole("region", { name: "Full section: Rules", exact: true })).toBeVisible();
  await expect(context.getByText("Keep the final diff for review.", { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/matched-rule-context.png", fullPage: true });
  await context.getByText("Show full section", { exact: true }).click();
  await page.getByRole("radio", { name: `Select rule: ${original}`, exact: true }).check();
  const checkbox = page.getByLabel("I have compared the wording and confirm this correction supports the existing rule.");
  await checkbox.check();
  await checkbox.uncheck();
  await expect(page.getByRole("button", { name: "Reinforce existing rule", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Create rule", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Create separate rule", exact: true })).toHaveCount(0);
  expect((await (await page.request.get(skillPath)).json()).rules).toHaveLength(2);
  await page.getByRole("article", { name: `Existing rule: ${original}`, exact: true }).getByRole("button", { name: "Replace this rule", exact: true }).click();
  const preview = page.getByRole("region", { name: "Rule replacement preview", exact: true });
  await expect(preview).toContainText(original);
  await expect(preview).toContainText(correction);
  await page.getByRole("button", { name: "Replace rule and resolve correction", exact: true }).click();
  await expect(page.getByText("Guidance updated and correction resolved. Publish to update your agents.", { exact: true })).toBeVisible();
  const after = await (await page.request.get(skillPath)).json();
  expect(after.rules).toHaveLength(2);
  expect(after.rules.find((rule: { id: string }) => rule.id === oldRule.id).body).toBe(correction);
  expect(after.body).not.toContain(original);
  expect(after.body).not.toContain("## Additional rules");
  const updated = await (await page.request.get(`${skillPath}/document`)).json();
  expect(updated.rule_sections[0].rules.map((rule: { id: string }) => rule.id)).toEqual(document.rule_sections[0].rules.map((rule: { id: string }) => rule.id));
});

test("a correction amends a passage and rule, survives a lost response, and places new guidance", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const imported = await page.request.post(`${API}/api/import/directory`, { headers, multipart: {
    files: { name: "correction-guide/SKILL.md", mimeType: "text/markdown", buffer: Buffer.from(
      "---\nname: Correction Guide\ndescription: Choose checks for documentation changes.\n---\n\n# Correction Guide\n\n## Process\n\nReview every change using the complete checklist.\n\n## Rules\n\n* Run the full test suite after every change.\n* Inspect the resulting diff.\n") },
  } });
  expect(imported.ok()).toBeTruthy();
  const skills = await (await page.request.get(`${API}/api/skills`)).json();
  const skill = skills.find((row: { name: string }) => row.name === "Correction Guide");
  const skillPath = `${API}/api/skills/${encodeURIComponent(skill.id)}`;
  const originalVersions = await (await page.request.get(`${skillPath}/versions`)).json();
  const incoming = "For documentation-only changes, run documentation checks instead of the full test suite.";
  expect((await page.request.post(`${API}/api/ingest`, { headers, data: { transaction_id: "browser-amendment", correction: incoming } })).status()).toBe(202);
  await page.goto("/inbox");
  await page.getByRole("button", { name: `Review correction: ${incoming}`, exact: true }).click();
  await page.getByRole("tab", { name: "Edit skill text", exact: true }).click();
  await page.getByRole("button", { name: "Open skill: Correction Guide", exact: true }).click();

  const replacement = "Run the full test suite after code changes. For documentation-only changes, run documentation checks.";
  await page.getByRole("textbox", { name: / · Rule 1$/ }).fill(replacement);

  await page.getByRole("textbox", { name: "Process · Passage 1", exact: true }).fill("Use the checklist relevant to the files changed.");
  await page.getByRole("button", { name: "Preview changes", exact: true }).click();
  const preview = page.getByRole("region", { name: "Correction change preview", exact: true });
  await expect(preview.locator(".oms-line-diff__line--del")).toContainText(["Review every change using the complete checklist.", "Run the full test suite after every change."]);
  await expect(preview).toContainText("Affected skills: Correction Guide");
  await page.screenshot({ path: "test-results/correction-amendment-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/correction-amendment-mobile.png", fullPage: true });
  let loseResponse = true;
  await page.route("**/api/review/browser-amendment/decision", async route => {
    if (loseResponse) { loseResponse = false; const response = await route.fetch(); expect(response.status()).toBe(200); return route.abort(); }
    return route.continue();
  });
  await page.getByRole("button", { name: "Apply change and resolve correction", exact: true }).click();
  await expect(page.getByRole("button", { name: "Apply change and resolve correction", exact: true })).toBeEnabled();
  await expect(preview).toContainText(replacement);
  await page.getByRole("button", { name: "Apply change and resolve correction", exact: true }).click();
  await expect(page.getByText("Guidance updated and correction resolved. Publish to update your agents.", { exact: true })).toBeVisible();
  const detail = await (await page.request.get(skillPath)).json();
  expect(detail.body).toContain(replacement);
  expect(detail.body).not.toContain("Run the full test suite after every change.");
  expect(detail.rules).toHaveLength(2);
  expect(detail.rules.find((rule: { body: string }) => rule.body === replacement).transaction_ids).toContain("browser-amendment");
  const versions = await (await page.request.get(`${skillPath}/versions`)).json();
  expect(versions.filter((version: { detail: string }) => version.detail?.includes("browser-amendment"))).toHaveLength(1);
  const compared = await (await page.request.get(`${skillPath}/versions/${originalVersions[0].id}/compare`)).json();
  expect(compared.rows.filter((row: { state: string; restorable: boolean }) => row.state !== "same" && row.restorable)).toHaveLength(2);

  const newRule = "Check internal links after documentation changes.";
  expect((await page.request.post(`${API}/api/ingest`, { headers, data: { transaction_id: "browser-placement", correction: newRule, skill_hint: skill.id } })).status()).toBe(202);
  await page.reload();
  await page.getByRole("button", { name: `Review correction: ${newRule}`, exact: true }).click();
  await page.getByRole("tab", { name: "Add or confirm a rule", exact: true }).click();
  await page.getByRole("button", { name: "Choose where to insert · Correction Guide", exact: true }).click();
  await page.getByRole("button", { name: "Insert at start of Rules", exact: true }).click();
  await expect(page.getByRole("region", { name: "Review selected correction" }).getByRole("combobox")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "New rule placement in Correction Guide", exact: true })).toContainText(newRule);
  await page.getByRole("button", { name: "Create rule", exact: true }).click();
  await expect(page.getByText("Rule created.", { exact: true })).toBeVisible();
  const updated = await (await page.request.get(skillPath)).json();
  expect(updated.body.indexOf(newRule)).toBeLessThan(updated.body.indexOf(replacement));
  expect(updated.body).not.toContain("## Additional rules");
  expect(errors).toEqual([]);
});
