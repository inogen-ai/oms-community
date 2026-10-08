import test from "node:test";
import assert from "node:assert/strict";
import { createElement as h } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import * as ui from "@inogen/oms-ui-core";
import { historySentence, HistoryTimeline } from "@inogen/oms-ui-core/history";
const fingerprint = { content_generation: 1, binding_generation: 2, update_generation: 3, policy_version: "p", policy_digest: "policy", local_digest: "local", candidate_digest: "upstream" };
const evidence = (value, kind = "known") => ({ kind, value, source_digest: null, policy_version: "p" });
const change = (part_id, overrides = {}) => ({ part_id, kind: "section", action: "conflict", base: evidence("Base text"), local: evidence("Local text"), incoming: evidence("Upstream text"), linked_removals: [], ...overrides });
function update(overrides = {}) {
  return { update_id: "update-1", generation: 3, status: "open", drafts: [], plan: {
    skill: { tenant_id: "t", skill_id: "skill-1" }, origin: { kind: "github", origin_id: "origin-1", generation: 2 },
    base: { snapshot_id: "base-1" }, incoming: { snapshot_id: "upstream-1" }, fingerprint,
    changes: [change("section:intro")], conflicts: [{ part_id: "section:intro", reason: "Both changed", allowed_choices: ["keep_oms", "use_upstream", "merged_text"] }], flags: ["conflict"], ...overrides,
  } };
}
const pkg = (path, valid = true) => ({ path, valid, upstream_name: "Review", description: "Review guide", reasons: valid ? [] : ["Missing SKILL.md"], unsupported_metadata: ["custom-key"], file_count: 2, total_bytes: 40 });
const discovery = { discovery_id: "d", resolved_ref: { kind: "branch", name: "main", commit: "a".repeat(40) }, preselected_path: null, packages: { items: [pkg("a"), pkg("b"), pkg("broken", false)], next_cursor: null } };
function component(name, props) { assert.equal(typeof ui[name], "function"); return ui[name](props); }
function nodes(node) {
  if (Array.isArray(node)) return node.flatMap(nodes);
  if (!node || typeof node !== "object") return [];
  if (typeof node.type === "function") return nodes(node.type(node.props));
  return [node, ...nodes(node.props?.children)];
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join("");
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node !== "object") return String(node);
  if (typeof node.type === "function") return text(node.type(node.props));
  return text(node.props?.children);
}
const button = (tree, label) => nodes(tree).find(node => node.type === "button" && text(node) === label);
const input = (tree, label) => nodes(tree).find(node => node.props?.["aria-label"] === label);

test("source status is inert and only the declared field flag uses the licence label", () => {
  assert.equal(typeof ui.SourceStatus, "function");
  const html = renderToStaticMarkup(h(ui.SourceStatus, { status: "failed", message: "<script>window.bad = true</script>", flags: ["script_changes"], retryNeeded: true }));
  assert.ok(!html.includes("<script>")); assert.match(html, /&lt;script&gt;/); assert.match(html, /Retry needed/);
  assert.ok(!html.includes("Declared licence changed"));
  assert.match(renderToStaticMarkup(h(ui.SourceStatus, { status: "updates_available", flags: ["declared_licence_changed"] })), /Declared licence changed/);
});
test("stored binding status shows its recorded failure code after reload", () => {
  const html = renderToStaticMarkup(h(ui.SourceStatus, { status: { state: "failed", code: "section_structure_changed", checked_at: "2026-10-07T10:00:00Z" } }));
  assert.match(html, /Failed/); assert.match(html, /section structure changed/);
  assert.doesNotMatch(renderToStaticMarkup(h(ui.SourceStatus, { status: { state: "up_to_date", code: null } })), /Reason/);
});
test("discovery selects only valid explicit folders and displays invalid and unsupported entries", () => {
  let tree = component("SourceDiscovery", { discovery, domains: ["docs"], onSelectionsChange() {} });
  assert.equal(nodes(tree).filter(node => node.type === "input" && node.props.type === "checkbox" && node.props.checked).length, 0);
  assert.match(text(tree), /Missing SKILL.md/); assert.match(text(tree), /custom-key/);
  tree = component("SourceDiscovery", { discovery: { ...discovery, preselected_path: "b" }, domains: ["docs"], defaultDomain: "docs", onSelectionsChange() {} });
  assert.equal(input(tree, "Select b").props.checked, true); assert.equal(input(tree, "Select a").props.checked, false);
  assert.equal(input(tree, "Select broken").props.disabled, true);
});
test("select-all retains edited names and new names are unique; duplicates and empty domains block install", () => {
  let selections = [{ package_path: "a", local_name: "Hand edited", domain: "docs" }];
  const props = { discovery, domains: ["docs"], defaultDomain: "docs", existingNames: ["Review"], onSelectionsChange: next => { selections = next; }, onInstall() {} };
  let tree = component("SourceDiscovery", { ...props, selections });
  button(tree, "Select all shown").props.onClick();
  assert.deepEqual(selections, [{ package_path: "a", local_name: "Hand edited", domain: "docs" }, { package_path: "b", local_name: "Review 2", domain: "docs" }]);
  tree = component("SourceDiscovery", { ...props, selections: selections.map(row => ({ ...row, local_name: "Same" })) });
  assert.equal(button(tree, "Install selected skills").props.disabled, true); assert.match(text(tree), /unique local name/i);
  tree = component("SourceDiscovery", { ...props, selections: [{ ...selections[0], domain: "" }] });
  assert.equal(button(tree, "Install selected skills").props.disabled, true);
});
test("installation sends only current selected paths, names and domains", () => {
  const selections = [{ package_path: "a", local_name: "Local review", domain: "docs" }]; let sent;
  const tree = component("SourceDiscovery", { discovery, selections, domains: ["docs"], onSelectionsChange() {}, onInstall: value => { sent = value; } });
  assert.equal(button(tree, "Install selected skills").props.disabled, false);
  button(tree, "Install selected skills").props.onClick(); assert.deepEqual(sent, { discovery_id: "d", selections });
});
test("Apply requires saved allowed decisions tied to the displayed fingerprint", () => {
  const card = update();
  const props = { update: card, partFingerprints: { "section:intro": "part-1" }, ownership: { "section:intro": "shared" }, onDraftChange() {}, onApply() {}, onSaveDraft() {} };
  let tree = component("SourceUpdateCard", props);
  assert.equal(button(tree, "Apply whole skill").props.disabled, true);
  for (const label of ["Shared ownership", "Base (B)", "Current OMS (L)", "Incoming (U)"]) assert.ok(text(tree).includes(label));
  const choices = [{ part_id: "section:intro", part_fingerprint: "part-1", choice: "keep_oms", merged_text: null }];
  const draft = { fingerprint, choices, removal_consents: [] };
  tree = component("SourceUpdateCard", { ...props, draft });
  assert.equal(button(tree, "Apply whole skill").props.disabled, true); assert.match(text(tree), /Unsaved choices/);
  tree = component("SourceUpdateCard", { ...props, update: { ...card, drafts: choices }, draft });
  assert.equal(button(tree, "Apply whole skill").props.disabled, false);
  tree = component("SourceUpdateCard", { ...props, update: { ...card, drafts: choices }, draft: { ...draft, fingerprint: { ...fingerprint, content_generation: 9 } } });
  assert.equal(button(tree, "Apply whole skill").props.disabled, true); assert.match(text(tree), /out of date/i);
});
test("merged prose is controlled and restricted to permitted prose decisions", () => {
  let draft; const props = { update: update(), partFingerprints: { "section:intro": "part-1" }, onDraftChange: value => { draft = value; }, onSaveDraft() {} };
  let tree = component("SourceUpdateCard", props);
  input(tree, "Decision for section:intro").props.onChange({ target: { value: "merged_text" } });
  tree = component("SourceUpdateCard", { ...props, draft });
  input(tree, "Merged text for section:intro").props.onChange({ target: { value: "Human wording" } });
  assert.equal(draft.choices[0].merged_text, "Human wording");
  tree = component("SourceUpdateCard", { ...props, update: update({ conflicts: [{ part_id: "section:intro", reason: "Safety hold", allowed_choices: ["merged_text"] }], flags: ["safety_hold"] }), onApply() {} });
  assert.ok(!text(input(tree, "Decision for section:intro")).includes("Use upstream"));
  assert.equal(button(tree, "Apply whole skill").props.disabled, true);
  tree = component("SourceUpdateCard", { ...props, update: update({ changes: [change("file:script.sh", { kind: "file" })], conflicts: [{ part_id: "file:script.sh", reason: "Changed", allowed_choices: ["keep_oms", "use_upstream", "merged_text"] }] }), partFingerprints: { "file:script.sh": "f" } });
  assert.ok(!text(input(tree, "Decision for file:script.sh")).includes("Merged text"));
});
test("deletion groups need consent and individual Keep excludes only that part", () => {
  const card = update({ changes: [change("section:a", { action: "remove", incoming: evidence(null, "absent"), linked_removals: ["section:b"] }), change("section:b", { action: "remove", incoming: evidence(null, "absent") })], conflicts: [], flags: ["deletion_consent"] });
  let draft = { fingerprint, choices: [], removal_consents: [] };
  const props = { update: card, partFingerprints: { "section:a": "a", "section:b": "b" }, onDraftChange: next => { draft = next; }, onApply() {}, onSaveDraft() {} };
  let tree = component("SourceUpdateCard", { ...props, draft });
  assert.equal(button(tree, "Apply whole skill").props.disabled, true);
  input(tree, "Consent to remove group 1").props.onChange({ target: { checked: true } });
  assert.deepEqual(draft.removal_consents.sort(), ["section:a", "section:b"]);
  tree = component("SourceUpdateCard", { ...props, draft }); assert.equal(button(tree, "Apply whole skill").props.disabled, false);
  input(tree, "Keep section:a").props.onChange({ target: { checked: true } });
  assert.equal(draft.choices.find(row => row.part_id === "section:a").choice, "keep_oms"); assert.deepEqual(draft.removal_consents, ["section:b"]);
});
test("a merged-text choice on a linked removal keeps it and drops its consent", () => {
  const card = update({ changes: [change("rule:new", { kind: "rule", action: "add", local: evidence(null, "absent"), linked_removals: ["rule:old", "rule:other"] }),
    change("rule:old", { kind: "rule", action: "remove", incoming: evidence(null, "absent") }),
    change("rule:other", { kind: "rule", action: "remove", incoming: evidence(null, "absent") })],
    conflicts: [{ part_id: "rule:old", reason: "deletion_consent", allowed_choices: ["keep_oms", "use_upstream", "merged_text"] }], flags: ["deletion_consent"] });
  const merged = { part_id: "rule:old", choice: "merged_text", merged_text: "Merged wording", part_fingerprint: "old" };
  card.drafts = [merged];
  let draft = { fingerprint, choices: [merged], removal_consents: [] };
  const props = { update: card, partFingerprints: { "rule:new": "n", "rule:old": "old", "rule:other": "other" }, onDraftChange: next => { draft = next; }, onApply() {}, onSaveDraft() {} };
  let tree = component("SourceUpdateCard", { ...props, draft });
  assert.equal(button(tree, "Apply whole skill").props.disabled, true);
  input(tree, "Consent to remove group 1").props.onChange({ target: { checked: true } });
  assert.deepEqual(draft.removal_consents, ["rule:other"]);
  tree = component("SourceUpdateCard", { ...props, draft }); assert.equal(button(tree, "Apply whole skill").props.disabled, false);
});
test("oversized previews are capped before diff; binary metadata and publication warnings stay visible", () => {
  let tree = component("SourceFileComparison", { path: "guide.md", base: { text: "base" }, local: { text: "local" }, upstream: { text: "line\n".repeat(5000), size: 90000 }, onDownload() {} });
  assert.match(text(tree), /excerpt/i); assert.ok(!renderToStaticMarkup(tree).includes("oms-line-diff__line")); assert.ok(text(tree).length < 12000);
  tree = component("SourceFileComparison", { path: "asset.bin", upstream: { binary: true, text: null, size: 42, digest: "sha256:fixture" }, publicationWarning: "Reserved filename: retained but excluded from publication.", onDownload() {} });
  assert.match(text(tree), /sha256:fixture/); assert.match(text(tree), /excluded from publication/);
  assert.equal(nodes(tree).some(node => node.type === "textarea"), false); assert.equal(button(tree, "Download incoming").props.disabled, false);
});
test("small file diffs stay inert and downloads use the supplied side and manifest path", () => {
  let sent; const tree = component("SourceFileComparison", { path: "guide.md", base: { text: "old" }, local: { text: "local" }, upstream: { text: "<img src=x onerror=bad()>" }, onDownload: (side, path) => { sent = [side, path]; } });
  const html = renderToStaticMarkup(tree); assert.ok(html.includes("oms-line-diff__line")); assert.ok(!html.includes("<img"));
  button(tree, "Download incoming").props.onClick(); assert.deepEqual(sent, ["upstream", "guide.md"]);
});
test("bulk exclusions, pinned refs, first reconciliation and per-skill partial results are explicit", () => {
  assert.equal(typeof ui.sourceBulkExclusions, "function");
  assert.ok(ui.sourceBulkExclusions(update({ changes: [], conflicts: [], flags: ["script_changes"] })).includes("Scripts or executable files"));
  const tree = component("SourceStatus", { status: "up_to_date", firstReconciliation: true, ref: { kind: "tag", name: "v1", commit: "a".repeat(40) }, operation: { operation_id: "op", state: "complete", committed: true, outcomes: [{ skill_id: "a", state: "applied" }, { skill_id: "b", state: "failed", code: "stale" }] }, exclusions: [{ skill_id: "c", reasons: ["Scripts or executable files"] }] });
  for (const label of ["Pinned", "Awaiting first reconciliation", "a: Applied", "b: Failed", "c"]) assert.ok(text(tree).includes(label));
  assert.ok(nodes(tree).some(node => node.props.role === "status"));
});
test("undo needs current guards and confirmation and preserves the supplied guard", () => {
  const guard = { undo_id: "undo-1", expected_generations: [{ skill_id: "skill", content: 2, binding: 3 }], policy_version: "p" }; let sent;
  const props = { guard, eligible: true, confirmed: false, onConfirmedChange() {}, onUndo: value => { sent = value; } };
  let tree = component("SourceUndo", props); assert.equal(button(tree, "Undo source update").props.disabled, true);
  tree = component("SourceUndo", { ...props, confirmed: true }); button(tree, "Undo source update").props.onClick(); assert.deepEqual(sent, guard);
  tree = component("SourceUndo", { ...props, confirmed: true, eligible: false, reason: "Affected content has changed." });
  assert.equal(button(tree, "Undo source update").props.disabled, true); assert.match(text(tree), /Affected content has changed/);
  assert.equal(historySentence({ cause: "source_undo" }), "Undid a source update");
});
test("unavailable, loading, error and empty discovery have no enabled mutations", () => {
  for (const props of [{ loading: true }, { unavailable: "Sources are disabled." }, { error: "Unavailable" }, { discovery: { ...discovery, packages: { items: [], next_cursor: null } } }]) {
    const tree = component("SourceDiscovery", { domains: [], onSelectionsChange() {}, onInstall() {}, ...props });
    assert.ok(nodes(tree).filter(node => node.type === "button").every(node => node.props.disabled)); assert.ok(text(tree).length > 0);
  }
});

test("server-stale cards disable decisions while recheck remains available", () => {
  let called;
  const tree = component("SourceUpdateCard", { update: { ...update(), stale: true }, partFingerprints: { "section:intro": "p" }, onDraftChange() {}, onApply() {}, onSkip() {}, onAdopt() {}, onRecheck: body => { called = body; } });
  assert.equal(button(tree, "Apply whole skill").props.disabled, true);
  assert.equal(button(tree, "Skip this version").props.disabled, true);
  assert.equal(input(tree, "Decision for section:intro").props.disabled, true);
  assert.equal(button(tree, "Recheck safety").props.disabled, false);
  button(tree, "Recheck safety").props.onClick();
  assert.deepEqual(called, { skill_id: "skill-1", fingerprint });
});

test("preview bounds also enforce byte limits for multibyte source text", () => {
  assert.equal(typeof ui.boundedSourceText, "function");
  const result = ui.boundedSourceText("😀".repeat(40000));
  assert.equal(result.truncated, true);
  assert.ok(new TextEncoder().encode(result.text).length <= 65536);
  assert.ok(!result.text.includes("�"));
});

test("history distinguishes source evidence from ordinary document restoration", () => {
  assert.equal(historySentence({ cause: "source_update" }), "Applied a source update");
  assert.equal(historySentence({ cause: "restore" }), "Restored an earlier version");
  const html = renderToStaticMarkup(h(HistoryTimeline, { entries: [{ id: "v", cause: "source_update", source_operation_id: "op-1", source_origin_id: "origin-1", source_revision: "commit-1" }] }));
  assert.match(html, /op-1/); assert.match(html, /origin-1/); assert.match(html, /commit-1/);
});

test("server-passed saved safety decisions enable Apply without hiding original warnings", () => {
  for (const choice of [{ choice: "keep_oms", merged_text: null }, { choice: "merged_text", merged_text: "Safe human replacement" }]) {
    const drafts = [{ part_id: "section:intro", part_fingerprint: "part-1", ...choice }];
    const card = { ...update({ flags: ["conflict", "safety_hold", "required_check_unavailable"] }), drafts,
      resolved_check: { format_version: 1, state: "passed", code: "resolved_writes_checked", reasons: [] } };
    const props = { update: card, partFingerprints: { "section:intro": "part-1" }, onApply() {} };
    let tree = component("SourceUpdateCard", props);
    assert.equal(button(tree, "Apply whole skill").props.disabled, false);
    assert.match(text(tree), /Safety review required/);
    assert.match(text(tree), /Required check unavailable/);
    tree = component("SourceUpdateCard", { ...props, draft: { fingerprint, choices: [], removal_consents: [] } });
    assert.equal(button(tree, "Apply whole skill").props.disabled, true);
    tree = component("SourceUpdateCard", { ...props, update: { ...card, stale: true } });
    assert.equal(button(tree, "Apply whole skill").props.disabled, true);
  }
});

test("server-held or unavailable resolved checks block Apply even without original safety flags", () => {
  const drafts = [{ part_id: "section:intro", part_fingerprint: "part-1", choice: "keep_oms", merged_text: null }];
  for (const state of ["held", "unavailable"]) {
    const card = { ...update(), drafts, resolved_check: { format_version: 1, state, code: "global_check_hold", reasons: ["policy_hold"] } };
    const tree = component("SourceUpdateCard", { update: card, partFingerprints: { "section:intro": "part-1" }, onApply() {} });
    assert.equal(button(tree, "Apply whole skill").props.disabled, true);
  }
  const oldCard = { ...update({ flags: ["safety_hold"] }), drafts };
  const oldTree = component("SourceUpdateCard", { update: oldCard, partFingerprints: { "section:intro": "part-1" }, onApply() {} });
  assert.equal(button(oldTree, "Apply whole skill").props.disabled, true);
});

test("discovery rejects backend identity collisions, empty IDs and reserved repository IDs", () => {
  const props = { discovery, domains: ["docs"], onSelectionsChange() {}, onInstall() { assert.fail("Invalid identity must not install"); } };
  for (const [first, second] of [["Review guide", "Review-guide"], ["REVIEW__guide", "review guide"], ["Å review", "review"]]) {
    const tree = component("SourceDiscovery", { ...props, selections: [{ package_path: "a", local_name: first, domain: "docs" }, { package_path: "b", local_name: second, domain: "docs" }] });
    assert.equal(button(tree, "Install selected skills").props.disabled, true);
    button(tree, "Install selected skills").props.onClick();
  }
  for (const local_name of ["!!!", "中文", "repo-guide", " Repo: guide "]) {
    const tree = component("SourceDiscovery", { ...props, selections: [{ package_path: "a", local_name, domain: "docs" }] });
    assert.equal(button(tree, "Install selected skills").props.disabled, true);
  }
  const existing = component("SourceDiscovery", { ...props, existingNames: ["Review-guide"], selections: [{ package_path: "a", local_name: "Review guide", domain: "docs" }] });
  assert.equal(button(existing, "Install selected skills").props.disabled, true);
});

test("automatic discovery names avoid slug collisions without rewriting display names", () => {
  const result = ui.initialSourceSelections({ ...discovery, preselected_path: "a", packages: { ...discovery.packages, items: [pkg("a"), { ...pkg("b"), upstream_name: "Review guide" }] } }, { defaultDomain: "docs", existingNames: ["Review!"] });
  assert.equal(result[0].local_name, "Review 2");
  let selections;
  const tree = component("SourceDiscovery", { discovery: { ...discovery, packages: { ...discovery.packages, items: [{ ...pkg("a"), upstream_name: "Review guide" }, { ...pkg("b"), upstream_name: "Review-guide" }] } }, domains: ["docs"], defaultDomain: "docs", onSelectionsChange: value => { selections = value; } });
  button(tree, "Select all shown").props.onClick();
  assert.deepEqual(selections.map(row => row.local_name), ["Review guide", "Review-guide 2"]);
});

test("closed stale cards describe a historical comparison and keep decisions disabled", () => {
  for (const status of ["applied", "skipped", "adopted", "superseded", "closed"]) {
    const tree = component("SourceUpdateCard", { update: { ...update(), status, stale: true }, onApply() {}, onSkip() {}, onAdopt() {}, onRecheck() {} });
    assert.match(text(tree), /recorded historical comparison/i);
    assert.ok(!text(tree).includes("This draft is out of date"));
    assert.ok(!text(tree).includes("before deciding"));
    assert.equal(nodes(tree).filter(node => ["button", "select", "textarea", "input"].includes(node.type)).length, 0);
  }
});

test("discovery caps Select all at 100 and lets oversized restored selections be corrected", () => {
  const many = { ...discovery, packages: { items: Array.from({ length: 101 }, (_, index) => pkg(`p-${index}`)), next_cursor: null } };
  let selected = [];
  const props = { discovery: many, defaultDomain: "docs", onSelectionsChange: value => { selected = value; }, onInstall() { assert.fail("Oversized selection must not install"); } };
  let tree = component("SourceDiscovery", props);
  button(tree, "Select all shown").props.onClick();
  assert.equal(selected.length, 100);
  tree = component("SourceDiscovery", { ...props, selections: selected });
  assert.match(text(tree), /100.*100/);
  assert.equal(input(tree, "Select p-100").props.disabled, true);
  assert.equal(input(tree, "Select p-0").props.disabled, false);
  input(tree, "Select p-0").props.onChange({ target: { checked: false } });
  assert.equal(selected.length, 99);
  tree = component("SourceDiscovery", { ...props, selections: many.packages.items.map((row, index) => ({ package_path: row.path, local_name: `Skill ${index}`, domain: "docs" })) });
  assert.equal(button(tree, "Install selected skills").props.disabled, true);
  button(tree, "Install selected skills").props.onClick();
  assert.match(text(tree), /at most 100/i);
  assert.equal(input(tree, "Local name for p-0").props.disabled, false);
  button(tree, "Clear selection").props.onClick();
  assert.deepEqual(selected, []);
});
