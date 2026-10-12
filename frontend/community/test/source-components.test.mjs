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
const names = tree => nodes(tree).find(node => node.type === "details" && node.props.className === "oms-source-names");
function visibleText(node) {
  if (Array.isArray(node)) return node.map(visibleText).join("");
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node !== "object") return String(node);
  if (typeof node.type === "function") return visibleText(node.type(node.props));
  if (node.type === "details") return [node.props.children].flat(Infinity).filter(child => child?.type === "summary").map(text).join("");
  return visibleText(node.props?.children);
}
const part = (part_id, kind, overrides = {}) => ({ part_id, kind, label: part_id, attention: "safe", risks: [], default: "use_upstream", overridable: [], findings: [], ...overrides });
const unknownSide = evidence(null, "unknown"), absentSide = evidence(null, "absent");
function held(overrides = {}) {
  const fresh = (part_id, kind, incoming) => change(part_id, { kind, base: unknownSide, local: absentSide, incoming });
  const changes = [
    fresh("file:visual-companion.md", "file", evidence({ path: "visual-companion.md", digest: "d1", size: 10, mode: 33188 })),
    fresh("rule:a", "rule", evidence({ body: "Always write tests" })),
    fresh("section:s", "section", evidence({ heading: "Overview", body: "Text" })),
    fresh("file:b.md", "file", evidence({ path: "b.md", digest: "d2", size: 10, mode: 33188 })),
  ];
  const conflicts = changes.map(row => ({ part_id: row.part_id, reason: "unknown_base", allowed_choices: row.kind === "file" ? ["keep_oms", "use_upstream"] : ["keep_oms", "use_upstream", "merged_text"] }));
  const findings = [263, 264, 265].map(line => ({ path: "visual-companion.md", part_id: null, line, category: "phone", summary: `visual-companion.md, line ${line}: looks like a phone number` }));
  const base = update({ base: null, changes, conflicts, flags: ["conflict", "safety_hold", "first_reconciliation"] });
  return { ...base, held_install: true,
    parts: { "file:visual-companion.md": part("file:visual-companion.md", "file", { label: "visual-companion.md", attention: "decide", risks: ["personal_data"], default: null, overridable: ["phone"], findings }),
      "rule:a": part("rule:a", "rule", { label: "Rule: Always write tests" }), "section:s": part("section:s", "section", { label: "Section: Overview" }),
      "file:b.md": part("file:b.md", "file", { label: "b.md" }) },
    summary: { decide: 1, safe: 3, automatic: 0, risks: { personal_data: 1 }, whole_skill: [] },
    resolved_check: { state: "held", code: "review_decisions_required", reasons: [] }, ...overrides };
}
const fingerprints = { "file:visual-companion.md": "fp-file:visual-companion.md", "rule:a": "fp-rule:a", "section:s": "fp-section:s", "file:b.md": "fp-file:b.md" };

test("a held install asks only about the flagged file and installs the rest in one action", () => {
  let draft, sent;
  const props = { update: held(), title: "brainstorming", partFingerprints: fingerprints, onDraftChange: value => { draft = value; }, onApply: body => { sent = body; }, onSkip() {}, onAdopt() {} };
  let tree = component("SourceUpdateCard", props);
  const shown = text(tree);
  assert.match(shown, /New skill from GitHub: brainstorming/);
  assert.match(shown, /1 item needs you\./);
  assert.match(shown, /Looks like personal data/);
  assert.match(shown, /3 lines look like a phone number: lines 263, 264, 265/);
  assert.match(shown, /This keeps the personal data in the skill\./);
  assert.match(shown, /1 rule, 1 section and 1 file passed the checks\. They will be installed\./);
  assert.equal(button(tree, "Skip this version"), undefined);
  assert.equal(button(tree, "Keep my version and mark this update as seen"), undefined);
  assert.equal(button(tree, "Install skill").props.disabled, true);
  input(tree, "Take it anyway for visual-companion.md").props.onChange({ target: { checked: true } });
  assert.deepEqual(draft.choices, [{ part_id: "file:visual-companion.md", part_fingerprint: "fp-file:visual-companion.md", choice: "use_upstream", merged_text: null, acknowledged: ["phone"] }]);
  tree = component("SourceUpdateCard", { ...props, draft });
  assert.match(text(tree), /Ready to install\./);
  button(tree, "Install skill").props.onClick();
  assert.equal(sent.save, true);
  assert.deepEqual(sent.removal_consents, []);
  assert.deepEqual(sent.choices.map(row => [row.part_id, row.choice]).sort(), [["file:b.md", "use_upstream"], ["file:visual-companion.md", "use_upstream"], ["rule:a", "use_upstream"], ["section:s", "use_upstream"]]);
});

test("choice labels say what happens in the reader's terms", () => {
  const yours = update(); yours.parts = { "section:intro": part("section:intro", "section", { label: "Section: Intro", attention: "decide", risks: ["your_edits"], default: null }) };
  let tree = component("SourceUpdateCard", { update: yours, partFingerprints: { "section:intro": "p" }, onDraftChange() {} });
  for (const label of ["Keep yours for Section: Intro", "Take GitHub's version for Section: Intro", "Edit the text for Section: Intro"]) assert.ok(input(tree, label), label);
  assert.match(text(tree), /You changed this in OMS/);
  tree = component("SourceUpdateCard", { update: held(), partFingerprints: fingerprints, onDraftChange() {} });
  assert.ok(input(tree, "Leave out for visual-companion.md"));
  assert.ok(input(tree, "Take it anyway for visual-companion.md"));
  assert.equal(input(tree, "Take GitHub's version for visual-companion.md"), undefined);
  for (const old of ["Keep OMS", "Use upstream", "Merged text", "Save choices", "Apply whole skill"]) assert.ok(!text(tree).includes(old), old);
});

test("two scripts are confirmed as a group, with each one a click away", () => {
  const paths = ["scripts/a.sh", "scripts/b.sh"];
  const changes = paths.map(path => change(`file:${path}`, { kind: "file", base: unknownSide, local: absentSide, incoming: evidence({ path, digest: path, size: 1, mode: 33261 }) }));
  const card = { ...update({ base: null, changes, conflicts: changes.map(row => ({ part_id: row.part_id, reason: "unknown_base", allowed_choices: ["keep_oms", "use_upstream"] })) }), held_install: true,
    parts: Object.fromEntries(paths.map(path => [`file:${path}`, part(`file:${path}`, "file", { label: path, attention: "decide", risks: ["script"], default: null })])) };
  let draft; const props = { update: card, partFingerprints: { "file:scripts/a.sh": "a", "file:scripts/b.sh": "b" }, onDraftChange: value => { draft = value; }, onApply() {} };
  let tree = component("SourceUpdateCard", props);
  assert.match(text(tree), /2 items need you\./);
  assert.ok(input(tree, "Install these 2 scripts"));
  const each = nodes(tree).find(node => node.type === "details" && node.props.className === "oms-source-each");
  assert.equal(text(nodes(each).find(node => node.type === "summary")), "Choose each");
  input(tree, "Leave them out").props.onChange({ target: { checked: true } });
  assert.deepEqual(draft.choices.map(row => [row.part_id, row.choice]), [["file:scripts/a.sh", "keep_oms"], ["file:scripts/b.sh", "keep_oms"]]);
  tree = component("SourceUpdateCard", { ...props, draft });
  input(tree, "Take GitHub's version for scripts/a.sh").props.onChange({ target: { checked: true } });
  assert.deepEqual(draft.choices.map(row => [row.part_id, row.choice]).sort(), [["file:scripts/a.sh", "use_upstream"], ["file:scripts/b.sh", "keep_oms"]]);
});

test("technical details are closed by default and hold the internals", () => {
  const tree = component("SourceUpdateCard", { update: held(), partFingerprints: fingerprints });
  const technical = nodes(tree).find(node => node.type === "details" && node.props.className === "oms-source-technical");
  assert.ok(technical); assert.equal(technical.props.open, undefined);
  for (const internal of [/upstream-1/, /unknown_base/, /file:visual-companion\.md/, /Review revision/]) {
    assert.match(text(technical), internal);
    assert.doesNotMatch(visibleText(tree), internal);
  }
});

test("a flagged rule's label and reasons never quote its text; Show and Edit the text start from it", () => {
  // Review Focus 4 and ruling P4: always-visible text never quotes the flagged text; a disclosure
  // the reader opens, and the editor an administrator uses to redact it, do.
  const changes = [change("rule:phone", { kind: "rule", base: unknownSide, local: absentSide, incoming: evidence({ body: "Call +44 20 7946 0958 about receipts." }) })];
  const card = { ...update({ base: null, changes, conflicts: [{ part_id: "rule:phone", reason: "unknown_base,source_text_hold", allowed_choices: ["keep_oms", "use_upstream", "merged_text"] }] }), held_install: true,
    parts: { "rule:phone": part("rule:phone", "rule", { label: "Rule with flagged text", attention: "decide", risks: ["personal_data"], default: null, overridable: ["phone"],
      findings: [{ path: null, part_id: "rule:phone", line: 1, category: "phone", summary: "SKILL.md text, line 1: looks like a phone number" }] }) } };
  let draft; const props = { update: card, partFingerprints: { "rule:phone": "p" }, onDraftChange: value => { draft = value; } };
  let tree = component("SourceUpdateCard", props);
  assert.match(visibleText(tree), /Rule with flagged text/);
  assert.match(visibleText(tree), /1 line looks like a phone number: line 1/);
  assert.doesNotMatch(visibleText(tree), /7946/);
  const show = nodes(tree).find(node => node.type === "details" && node.props.className === "oms-source-show");
  assert.equal(show.props.open, undefined); assert.match(text(show), /7946/);
  input(tree, "Edit the text for Rule with flagged text").props.onChange({ target: { checked: true } });
  assert.deepEqual(draft.choices, [{ part_id: "rule:phone", part_fingerprint: "p", choice: "merged_text", merged_text: "Call +44 20 7946 0958 about receipts." }]);
  tree = component("SourceUpdateCard", { ...props, draft });
  assert.equal(input(tree, "Text for Rule with flagged text").props.value, "Call +44 20 7946 0958 about receipts.");
});

test("several personal-data items share one 'Take all N anyway' choice, and it shows the note", () => {
  // Ruling P5: group forms of the spec labels; every "anyway" form carries the note.
  const paths = ["a.md", "b.md"];
  const changes = paths.map(path => change(`file:${path}`, { kind: "file", base: unknownSide, local: absentSide, incoming: evidence({ path, digest: path, size: 1, mode: 33188 }) }));
  const card = { ...update({ base: null, changes, conflicts: changes.map(row => ({ part_id: row.part_id, reason: "unknown_base,source_text_hold", allowed_choices: ["keep_oms", "use_upstream"] })) }), held_install: true,
    parts: Object.fromEntries(paths.map(path => [`file:${path}`, part(`file:${path}`, "file", { label: path, attention: "decide", risks: ["personal_data"], default: null, overridable: ["email", "phone"] })])) };
  let draft; const tree = component("SourceUpdateCard", { update: card, partFingerprints: { "file:a.md": "a", "file:b.md": "b" }, onDraftChange: value => { draft = value; } });
  const choices = nodes(tree).find(node => node.type === "fieldset" && node.props["aria-label"] === "Decision for every item in Looks like personal data");
  assert.match(text(choices), /Take all 2 anyway/);
  assert.match(text(choices), /This keeps the personal data in the skill\./);
  assert.match(text(choices), /Leave all out/);
  input(tree, "Take all 2 anyway").props.onChange({ target: { checked: true } });
  assert.deepEqual(draft.choices.map(row => [row.part_id, row.choice, row.acknowledged]), [["file:a.md", "use_upstream", ["email", "phone"]], ["file:b.md", "use_upstream", ["email", "phone"]]]);
});

test("a hold that names no part is shown and does not read as ready", () => {
  // Review Focus 2.
  const card = held({ summary: { decide: 0, safe: 4, automatic: 0, risks: {}, whole_skill: [{ path: null, part_id: null, line: null, category: "policy:source_constraint_conflict", summary: "Conflicts with a workspace safety rule (source constraint conflict)" }] } });
  card.parts = { ...card.parts, "file:visual-companion.md": part("file:visual-companion.md", "file", { label: "visual-companion.md" }) };
  const shown = visibleText(component("SourceUpdateCard", { update: card, partFingerprints: fingerprints, onApply() {} }));
  assert.match(shown, /Held for the whole skill/);
  assert.match(shown, /Conflicts with a workspace safety rule \(source constraint conflict\)/);
  assert.match(shown, /Held: see the reasons below before you install\./);
  assert.doesNotMatch(shown, /Ready to install/);
});

test("removals are one choice that carries their consent", () => {
  const gone = id => change(id, { kind: "rule", incoming: evidence(null, "absent"), base: evidence("Local"), local: evidence("Local") });
  const card = update({ changes: [gone("rule:a"), gone("rule:b")], conflicts: ["rule:a", "rule:b"].map(id => ({ part_id: id, reason: "deletion_consent", allowed_choices: ["keep_oms", "use_upstream", "merged_text"] })), flags: ["conflict", "deletion_consent"] });
  card.parts = Object.fromEntries(["rule:a", "rule:b"].map(id => [id, part(id, "rule", { label: id, attention: "decide", risks: ["removed_upstream"], default: null })]));
  let draft, sent; const props = { update: card, partFingerprints: { "rule:a": "a", "rule:b": "b" }, onDraftChange: value => { draft = value; }, onApply: body => { sent = body; } };
  let tree = component("SourceUpdateCard", props);
  assert.match(text(tree), /Removed on GitHub/);
  input(tree, "Remove them in OMS too").props.onChange({ target: { checked: true } });
  tree = component("SourceUpdateCard", { ...props, draft });
  button(tree, "Apply update").props.onClick();
  assert.deepEqual([...sent.removal_consents].sort(), ["rule:a", "rule:b"]);
  input(tree, "Keep them").props.onChange({ target: { checked: true } });
  tree = component("SourceUpdateCard", { ...props, draft });
  button(tree, "Apply update").props.onClick();
  assert.deepEqual(sent.removal_consents, []);
});

test("editing the text of a removed part keeps it, so it needs no removal consent", () => {
  const removed = id => change(id, { kind: "rule", incoming: evidence(null, "absent"), base: evidence("Old"), local: evidence("Old") });
  const card = update({ changes: [change("rule:new", { kind: "rule", base: evidence(null, "absent"), local: evidence(null, "absent"), linked_removals: ["rule:old", "rule:other"] }), removed("rule:old"), removed("rule:other")],
    conflicts: ["rule:new", "rule:old", "rule:other"].map(id => ({ part_id: id, reason: id === "rule:new" ? "ambiguous_replacement" : "deletion_consent", allowed_choices: ["keep_oms", "use_upstream", "merged_text"] })) });
  const choices = [{ part_id: "rule:new", part_fingerprint: "n", choice: "use_upstream", merged_text: null },
    { part_id: "rule:old", part_fingerprint: "o", choice: "merged_text", merged_text: "Merged wording" },
    { part_id: "rule:other", part_fingerprint: "t", choice: "use_upstream", merged_text: null }];
  let sent;
  const tree = component("SourceUpdateCard", { update: card, partFingerprints: { "rule:new": "n", "rule:old": "o", "rule:other": "t" }, draft: { fingerprint, choices, removal_consents: [] }, onApply: body => { sent = body; } });
  button(tree, "Apply update").props.onClick();
  assert.deepEqual(sent.removal_consents, ["rule:other"]);
});

test("a normal update offers Skip and Keep my version, and a held install offers Delete", () => {
  const calls = [];
  const normal = update(); normal.parts = { "section:intro": part("section:intro", "section", { label: "Section: Intro", attention: "decide", risks: ["your_edits"], default: null }) };
  let tree = component("SourceUpdateCard", { update: normal, title: "Writing", partFingerprints: { "section:intro": "p" }, onSkip() {}, onAdopt() {}, onRecheck() {}, onApply() {}, onDeleteSkill: () => calls.push("delete") });
  assert.match(text(tree), /Update from GitHub: Writing/);
  for (const label of ["Apply update", "Skip this version", "Keep my version and mark this update as seen", "Check safety again"]) assert.ok(button(tree, label), label);
  assert.equal(button(tree, "Delete skill"), undefined);
  tree = component("SourceUpdateCard", { update: held(), title: "brainstorming", partFingerprints: fingerprints, onApply() {}, onDeleteSkill: () => calls.push("delete") });
  button(tree, "Delete skill").props.onClick();
  assert.deepEqual(calls, ["delete"]);
  assert.equal(button(component("SourceUpdateCard", { update: held(), partFingerprints: fingerprints, onApply() {} }), "Delete skill"), undefined);
  const local = update({ origin: { kind: "local", origin_id: "o", generation: 1 } });
  assert.match(text(component("SourceUpdateCard", { update: local, title: "Writing", partFingerprints: { "section:intro": "p" } })), /Update from a local package: Writing/);
});

test("Apply waits while the saved selection is still held", () => {
  const drafts = [{ part_id: "file:visual-companion.md", part_fingerprint: "fp-file:visual-companion.md", choice: "use_upstream", merged_text: null, acknowledged: ["phone"] },
    ...["rule:a", "section:s", "file:b.md"].map(id => ({ part_id: id, part_fingerprint: `fp-${id}`, choice: "use_upstream", merged_text: null, acknowledged: [] }))];
  const card = held({ drafts, resolved_check: { state: "held", code: "source_text_hold", reasons: [] } });
  const tree = component("SourceUpdateCard", { update: card, partFingerprints: fingerprints, onApply() {} });
  assert.equal(button(tree, "Install skill").props.disabled, true);
  assert.match(text(tree), /Required checks still hold this selection\./);
  // Ruling T6-1: the sentence says "Ready" only when the primary action is enabled.
  assert.match(text(tree), /Held: see the reasons below before you install\./);
  assert.doesNotMatch(text(tree), /Ready to install/);
  const passed = component("SourceUpdateCard", { update: { ...card, resolved_check: { state: "passed", code: "resolved_writes_checked", reasons: [] } }, partFingerprints: fingerprints, onApply() {} });
  assert.equal(button(passed, "Install skill").props.disabled, false);
  assert.match(text(passed), /Ready to install\./);
});

test("a held install must take at least one content part", () => {
  // The held-install rule: Apply must install at least one rule, section, example or file; a skill detail alone is not enough.
  const base = held();
  const card = { ...base, parts: { ...base.parts, "field:description": part("field:description", "field", { label: "Description" }) },
    plan: { ...base.plan, changes: [...base.plan.changes, change("field:description", { kind: "field", base: unknownSide, local: absentSide, incoming: evidence("A description") })],
      conflicts: [...base.plan.conflicts, { part_id: "field:description", reason: "unknown_base", allowed_choices: ["keep_oms", "use_upstream"] }] } };
  const partFingerprints = { ...fingerprints, "field:description": "fp-field:description" };
  const left = Object.keys(fingerprints).map(id => ({ part_id: id, part_fingerprint: fingerprints[id], choice: "keep_oms", merged_text: null }));
  const props = { update: card, partFingerprints, onApply() {} };
  let tree = component("SourceUpdateCard", { ...props, draft: { fingerprint, choices: left, removal_consents: [] } });
  assert.equal(button(tree, "Install skill").props.disabled, true);
  assert.match(text(tree), /Choose at least one part to install, or delete this skill\./);
  assert.doesNotMatch(text(tree), /Ready to install/);
  const one = left.map(row => row.part_id === "rule:a" ? { ...row, choice: "use_upstream" } : row);
  tree = component("SourceUpdateCard", { ...props, draft: { fingerprint, choices: one, removal_consents: [] } });
  assert.equal(button(tree, "Install skill").props.disabled, false);
  assert.doesNotMatch(text(tree), /Choose at least one part/);
  assert.match(text(tree), /Ready to install\./);
});

test("no part offers GitHub's version when its conflict does not allow it or a check failed", () => {
  // Spec D2: only personal data may be kept on review; an injection, a failed check or a check that
  // could not run never can, even next to personal data.
  const rule = change("rule:r", { kind: "rule" });
  const labels = (risks, allowed, overridable = []) => ui.sourceChoiceOptions(update(), rule,
    part("rule:r", "rule", { attention: "decide", risks, default: null, overridable }), { allowed_choices: allowed }).map(option => option.label);
  assert.deepEqual(labels([], ["keep_oms", "merged_text"]), ["Keep yours", "Edit the text"]);
  assert.deepEqual(labels(["personal_data"], ["keep_oms", "merged_text"], ["phone"]), ["Keep yours", "Edit the text"]);
  assert.deepEqual(labels(["personal_data"], ["keep_oms", "use_upstream"], []), ["Keep yours"]);
  for (const risk of ["injection", "check_failed", "check_unavailable"]) {
    assert.deepEqual(labels([risk], ["keep_oms", "use_upstream", "merged_text"]), ["Keep yours", "Edit the text"], risk);
    assert.deepEqual(labels([risk, "personal_data"], ["keep_oms", "use_upstream"], ["phone"]), ["Keep yours"], risk);
  }
  const card = update(); card.parts = { "section:intro": part("section:intro", "section", { label: "Section: Intro", attention: "decide", risks: ["injection"], default: null }) };
  const tree = component("SourceUpdateCard", { update: card, partFingerprints: { "section:intro": "p" }, onDraftChange() {} });
  assert.ok(input(tree, "Keep yours for Section: Intro"));
  assert.equal(input(tree, "Take GitHub's version for Section: Intro"), undefined);
  assert.equal(input(tree, "Take it anyway for Section: Intro"), undefined);
});

test("the personal-data note describes each 'anyway' choice it belongs to", () => {
  // Ruling T6-4: the note is the consent wording, so its radio points at it.
  const described = (tree, label) => {
    const ids = input(tree, label).props["aria-describedby"]?.split(" ") ?? [];
    return ids.map(id => text(nodes(tree).find(node => node.props?.id === id)));
  };
  let tree = component("SourceUpdateCard", { update: held(), partFingerprints: fingerprints, onDraftChange() {} });
  assert.deepEqual(described(tree, "Take it anyway for visual-companion.md"), ["This keeps the personal data in the skill."]);
  assert.deepEqual(described(tree, "Leave out for visual-companion.md"), []);
  const paths = ["a.md", "b.md"];
  const changes = paths.map(path => change(`file:${path}`, { kind: "file", base: unknownSide, local: absentSide, incoming: evidence({ path, digest: path, size: 1, mode: 33188 }) }));
  const card = { ...update({ base: null, changes, conflicts: changes.map(row => ({ part_id: row.part_id, reason: "unknown_base", allowed_choices: ["keep_oms", "use_upstream"] })) }), held_install: true,
    parts: Object.fromEntries(paths.map(path => [`file:${path}`, part(`file:${path}`, "file", { label: path, attention: "decide", risks: ["personal_data"], default: null, overridable: ["phone"] })])) };
  tree = component("SourceUpdateCard", { update: card, partFingerprints: { "file:a.md": "a", "file:b.md": "b" }, onDraftChange() {} });
  assert.deepEqual(described(tree, "Take all 2 anyway"), ["This keeps the personal data in the skill."]);
  assert.deepEqual(described(tree, "Take it anyway for a.md"), ["This keeps the personal data in the skill."]);
  const ids = nodes(tree).map(node => node.props?.id).filter(Boolean);
  assert.equal(new Set(ids).size, ids.length);
});

test("a group choice that removes parts in OMS says so in the group itself", () => {
  // Ruling T6-4: taking replacements removes the parts they replace; the group says so outside "Choose each".
  const replacement = (id, old) => change(id, { kind: "rule", local: absentSide, linked_removals: [old] });
  const removed = id => change(id, { kind: "rule", incoming: absentSide, base: evidence("Old"), local: evidence("Old") });
  const changes = [replacement("rule:new1", "rule:old1"), replacement("rule:new2", "rule:old2"), removed("rule:old1"), removed("rule:old2")];
  const card = update({ changes, conflicts: changes.map(row => ({ part_id: row.part_id, reason: row.linked_removals.length ? "ambiguous_replacement" : "deletion_consent", allowed_choices: ["keep_oms", "use_upstream", "merged_text"] })) });
  card.parts = Object.fromEntries(changes.map(row => [row.part_id, part(row.part_id, "rule", { attention: "decide", default: null, risks: [row.linked_removals.length ? "unclear_identity" : "removed_upstream"] })]));
  const tree = component("SourceUpdateCard", { update: card, partFingerprints: Object.fromEntries(changes.map(row => [row.part_id, row.part_id])), onDraftChange() {} });
  const group = nodes(tree).find(node => node.type === "fieldset" && node.props["aria-label"] === "Decision for every item in OMS cannot match this to an existing part");
  assert.match(visibleText(group), /Take GitHub's version for all 2/);
  assert.match(visibleText(group), /This also removes 2 parts in OMS\./);
  const take = input(tree, "Take GitHub's version for all 2").props["aria-describedby"].split(" ");
  assert.deepEqual(take.map(id => text(nodes(tree).find(node => node.props?.id === id))), ["This also removes 2 parts in OMS."]);
  assert.equal(input(tree, "Leave all out").props["aria-describedby"], undefined);
  assert.doesNotMatch(visibleText(nodes(tree).find(node => node.type === "fieldset" && node.props["aria-label"] === "Decision for every item in Removed on GitHub")), /This also removes/);
});

test("a card that still needs a decision does not call its empty selection held", () => {
  // The server holds a card until its decisions are made; that is not a held selection.
  const card = { ...update(), resolved_check: { state: "held", code: "review_decisions_required", reasons: [] } };
  const tree = component("SourceUpdateCard", { update: card, partFingerprints: { "section:intro": "p" }, onApply() {} });
  assert.match(text(tree), /1 item needs you\./);
  assert.doesNotMatch(text(tree), /Required checks still hold this selection/);
  // With nothing left to decide, a held selection still blocks Apply and says so.
  const clean = { ...update({ conflicts: [], changes: [change("section:intro", { action: "replace" })] }), resolved_check: { state: "held", code: "global_check_hold", reasons: [] } };
  const cleanTree = component("SourceUpdateCard", { update: clean, partFingerprints: { "section:intro": "p" }, onApply() {} });
  assert.match(text(cleanTree), /Required checks still hold this selection\./);
  assert.equal(button(cleanTree, "Apply update").props.disabled, true);
});

test("safe parts are listed under Show and can be left out one by one", () => {
  let draft; const props = { update: held(), partFingerprints: fingerprints, onDraftChange: value => { draft = value; } };
  let tree = component("SourceUpdateCard", props);
  input(tree, "Leave out Rule: Always write tests").props.onChange({ target: { checked: true } });
  assert.deepEqual(draft.choices, [{ part_id: "rule:a", part_fingerprint: "fp-rule:a", choice: "keep_oms", merged_text: null }]);
  tree = component("SourceUpdateCard", { ...props, draft });
  assert.match(text(tree), /1 rule, 1 section and 1 file passed the checks\. They will be installed\. 1 left out\./);
  input(tree, "Leave out Rule: Always write tests").props.onChange({ target: { checked: false } });
  assert.deepEqual(draft.choices, []);
});

test("Edit the text writes the reader's own wording and is never offered for a file", () => {
  let draft; const card = update(); card.parts = { "section:intro": part("section:intro", "section", { label: "Section: Intro", attention: "decide", risks: ["your_edits"], default: null }) };
  const props = { update: card, partFingerprints: { "section:intro": "part-1" }, onDraftChange: value => { draft = value; } };
  let tree = component("SourceUpdateCard", props);
  input(tree, "Edit the text for Section: Intro").props.onChange({ target: { checked: true } });
  assert.equal(draft.choices[0].merged_text, "Upstream text");
  tree = component("SourceUpdateCard", { ...props, draft });
  input(tree, "Text for Section: Intro").props.onChange({ target: { value: "Human wording" } });
  assert.equal(draft.choices[0].merged_text, "Human wording");
  const file = update({ changes: [change("file:script.sh", { kind: "file" })], conflicts: [{ part_id: "file:script.sh", reason: "local_divergence", allowed_choices: ["keep_oms", "use_upstream", "merged_text"] }] });
  assert.equal(input(component("SourceUpdateCard", { update: file, partFingerprints: { "file:script.sh": "f" } }), "Edit the text for file:script.sh"), undefined);
});

test("a stale card keeps Check safety again and a closed card has no controls", () => {
  let called;
  let tree = component("SourceUpdateCard", { update: { ...held(), stale: true }, partFingerprints: fingerprints, onApply() {}, onRecheck: body => { called = body; } });
  assert.equal(button(tree, "Install skill").props.disabled, true);
  assert.equal(input(tree, "Leave out for visual-companion.md").props.disabled, true);
  button(tree, "Check safety again").props.onClick();
  assert.deepEqual(called, { skill_id: "skill-1", fingerprint });
  // Ruling T6-1: a stale card's sentence is the out-of-date wording, never "Ready", even with every choice made.
  const sentence = tree => text(nodes(tree).find(node => node.props?.className === "oms-source-sentence"));
  const choices = Object.keys(fingerprints).map(id => ({ part_id: id, part_fingerprint: fingerprints[id], choice: id === "file:visual-companion.md" ? "keep_oms" : "use_upstream", merged_text: null }));
  tree = component("SourceUpdateCard", { update: { ...held(), stale: true }, partFingerprints: fingerprints, draft: { fingerprint, choices, removal_consents: [] }, onApply() {} });
  assert.equal(sentence(tree), "This card is out of date. Check safety again before deciding.");
  assert.doesNotMatch(text(tree), /Ready to install/);
  tree = component("SourceUpdateCard", { update: held(), partFingerprints: fingerprints, draft: { fingerprint, choices, removal_consents: [] }, freshness: "stale", onApply() {} });
  assert.equal(sentence(tree), "This card is out of date. Check safety again before deciding.");
  tree = component("SourceUpdateCard", { update: held(), partFingerprints: fingerprints, draft: { fingerprint: { ...fingerprint, content_generation: 9 }, choices: [], removal_consents: [] }, onApply() {} });
  assert.equal(button(tree, "Install skill").props.disabled, true); assert.match(text(tree), /out of date/i);
  for (const status of ["applied", "skipped", "adopted", "superseded", "closed"]) {
    tree = component("SourceUpdateCard", { update: { ...held(), status }, onApply() {}, onSkip() {}, onAdopt() {}, onRecheck() {} });
    assert.match(text(tree), /recorded historical comparison/i);
    assert.equal(nodes(tree).filter(node => ["button", "select", "textarea", "input"].includes(node.type)).length, 0);
  }
});

test("a card from a server without triage still asks about every conflict", () => {
  let draft;
  const tree = component("SourceUpdateCard", { update: update(), partFingerprints: { "section:intro": "part-1" }, onDraftChange: value => { draft = value; } });
  assert.match(text(tree), /1 item needs you\./);
  input(tree, "Keep yours for section:intro").props.onChange({ target: { checked: true } });
  assert.equal(draft.choices[0].choice, "keep_oms");
});

test("finding reasons count lines and never need the matched text", () => {
  const f = (category, line) => ({ path: "a.md", part_id: null, line, category, summary: "" });
  assert.deepEqual(ui.sourceFindingReasons([f("phone", 263), f("phone", 264), f("phone", 265), f("phone", 265)]), ["3 lines look like a phone number: lines 263, 264, 265"]);
  assert.deepEqual(ui.sourceFindingReasons([f("email", 4)]), ["1 line looks like an email address: line 4"]);
  assert.deepEqual(ui.sourceFindingReasons([f("injection:instruction-override", null)]), ["Looks like instructions to an AI (instruction override)"]);
  assert.deepEqual(ui.sourceFindingReasons([f("file_size", null)]), ["The file is larger than 10 MB"]);
  assert.deepEqual(ui.sourceFindingReasons([f("check_unavailable:text_check_limit", null)]), ["A required check could not run (text check limit)"]);
  assert.deepEqual(ui.sourceFindingReasons([f("policy:source_constraint_conflict", null)]), ["Conflicts with a workspace safety rule (source constraint conflict)"]);
  assert.deepEqual(ui.sourceFindingReasons(Array.from({ length: 12 }, (_, index) => f("phone", index + 1))), ["12 lines look like a phone number: lines 1, 2, 3, 4, 5, 6, 7, 8, 9, 10 and 2 more"]);
});

test("the repository line shows facts and concerns, or says they are unavailable", () => {
  const now = new Date("2026-10-12T12:00:00Z");
  const repository = { name: "obra/superpowers", url: "https://github.com/obra/superpowers", concerns: [{ code: "few_stars", label: "Fewer than 10 stars" }],
    facts: { full_name: "obra/superpowers", description: null, stars: 3, forks: 0, archived: false, disabled: false, is_fork: false, licence: "MIT", created_at: "2025-01-01T00:00:00Z", pushed_at: "2026-10-10T12:00:00Z", default_branch: "main", fetched_at: "2026-10-12T11:00:00Z" } };
  let html = renderToStaticMarkup(h(ui.SourceRepositoryLine, { repository, now }));
  assert.match(html, /href="https:\/\/github\.com\/obra\/superpowers"/);
  for (const fact of ["obra/superpowers", "3 stars", "MIT", "updated 2 days ago", "Fewer than 10 stars"]) assert.ok(html.includes(fact), fact);
  html = renderToStaticMarkup(h(ui.SourceRepositoryLine, { repository: { ...repository, facts: null, concerns: [] }, now }));
  assert.match(html, /Repository facts unavailable/);
  assert.equal(ui.sourceUpdatedAgo("2026-10-12T11:59:30Z", now), "updated just now");
  assert.equal(ui.sourceUpdatedAgo("2026-10-12T11:00:00Z", now), "updated 1 hour ago");
  assert.equal(ui.sourceUpdatedAgo("2026-08-01T12:00:00Z", now), "updated 2 months ago");
  assert.equal(ui.sourceRepositoryFacts({ ...repository.facts, stars: 1, licence: "NOASSERTION", pushed_at: null }, now).join(" · "), "1 star · Licence not recognised");
  assert.match(renderToStaticMarkup(h(ui.SourceUpdateCard, { update: held({ repository }), partFingerprints: fingerprints, now })), /3 stars/);
  assert.match(renderToStaticMarkup(h(ui.SourceDiscovery, { discovery: { ...discovery, repository }, now, domains: ["docs"], onSelectionsChange() {} })), /Fewer than 10 stars/);
});

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
  assert.equal(text(nodes(names(tree)).find(node => node.type === "summary")), "Edit names and areas"); assert.equal("open" in names(tree).props, false);
  for (const label of ["Local name for a", "Domain for a", "Local name for b", "Domain for b"]) assert.ok(input(names(tree), label), label);
  assert.equal(input(tree, "Local name for a").props.value, "Same"); assert.equal(nodes(tree).filter(node => node.props?.className === "oms-source-names").length, 1);
  assert.equal(button(tree, "Install selected skills").props.disabled, true); assert.match(text(tree), /unique local name/i);
  tree = component("SourceDiscovery", { ...props, selections: [{ ...selections[0], domain: "" }] });
  assert.equal(button(tree, "Install selected skills").props.disabled, true);
});
test("the names disclosure opens itself when the selection needs attention and otherwise stays as the user left it", () => {
  const opened = selections => { const element = { open: false }; names(component("SourceDiscovery", { discovery, domains: ["docs"], selections, onSelectionsChange() {} })).props.ref(element); return element.open; };
  assert.equal(opened([{ package_path: "a", local_name: "Fine", domain: "docs" }]), false);
  assert.equal(opened([{ package_path: "a", local_name: "Fine", domain: "" }]), true);
  assert.equal(opened([{ package_path: "a", local_name: "Same", domain: "docs" }, { package_path: "b", local_name: "Same", domain: "docs" }]), true);
});
test("installation sends only current selected paths, names and domains", () => {
  const selections = [{ package_path: "a", local_name: "Local review", domain: "docs" }]; let sent;
  const tree = component("SourceDiscovery", { discovery, selections, domains: ["docs"], onSelectionsChange() {}, onInstall: value => { sent = value; } });
  assert.equal(button(tree, "Install selected skills").props.disabled, false);
  button(tree, "Install selected skills").props.onClick(); assert.deepEqual(sent, { discovery_id: "d", selections });
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
  assert.equal(input(names(tree), "Local name for p-0").props.disabled, false);
  button(tree, "Clear selection").props.onClick();
  assert.deepEqual(selected, []);
});
test("a lone valid package on a complete listing is preselected; several, or an unfinished listing, are not", () => {
  const listing = (items, next_cursor = null, preselected_path = null) => ({ ...discovery, preselected_path, packages: { items, next_cursor } });
  const pick = (value, options = { defaultDomain: "docs" }) => ui.initialSourceSelections(value, options).map(row => row.package_path);
  assert.deepEqual(pick(listing([pkg("a"), pkg("broken", false)])), ["a"]);
  assert.deepEqual(pick(listing([pkg("a"), pkg("b"), pkg("broken", false)])), []);
  assert.deepEqual(pick(listing([pkg("a")], "cursor-2")), []);
  assert.deepEqual(pick(listing([pkg("a"), pkg("b")], null, "b")), ["b"]);
  assert.deepEqual(pick(listing([pkg("broken", false)])), []);
  assert.deepEqual(pick(null), []);
});

test("the selection count is out of the packages found, and the request limit shows only when it can bite", () => {
  const props = { domains: ["docs"], onSelectionsChange() {} };
  let shown = text(component("SourceDiscovery", { ...props, discovery }));
  assert.match(shown, /0 of 3 packages selected\./);
  assert.doesNotMatch(shown, /at most 100/);
  const many = { ...discovery, packages: { items: Array.from({ length: 101 }, (_, index) => pkg(`skill-${index}`)), next_cursor: null } };
  shown = text(component("SourceDiscovery", { ...props, discovery: many }));
  assert.match(shown, /0 of 101 packages selected\. Install at most 100 packages per request\./);
  const partial = { ...discovery, packages: { ...discovery.packages, next_cursor: "more" } };
  shown = text(component("SourceDiscovery", { ...props, discovery: partial }));
  assert.match(shown, /0 of 3 loaded packages selected\. Install at most 100 packages per request\./);
});

test("source status shows the install summary, per-skill detail and an edition link", () => {
  const operation = { operation_id: "op", state: "awaiting_review", committed: true, outcomes: [
    { skill_id: "good", state: "applied", update_id: null, code: null, detail: null },
    { skill_id: "held-one", state: "awaiting_review", update_id: "update-1", code: "safety_review_required", detail: "Held: visual-companion.md, line 214: looks like a phone number" },
    { skill_id: "bad", state: "failed", update_id: null, code: "package_unreadable", detail: "SKILL.md is missing" },
  ] };
  const tree = component("SourceStatus", { status: "awaiting_review", operation, summary: "install", outcomeLink: outcome => outcome.update_id ? { href: "/x/" + outcome.update_id, label: "Open in review inbox" } : null });
  const shown = text(tree);
  assert.match(shown, /1 installed, 1 held for review, 1 failed/);
  assert.match(shown, /line 214: looks like a phone number/); assert.match(shown, /SKILL\.md is missing/);
  assert.equal(nodes(tree).find(node => node.type === "a" && node.props.href === "/x/update-1") !== undefined, true);
  assert.equal(nodes(tree).filter(node => node.type === "a").length, 1);
  assert.doesNotMatch(text(component("SourceStatus", { status: "complete", summary: "install", operation: { ...operation, outcomes: [] } })), /installed/);
  // A skill that was already installed is counted, and only when there is one.
  const again = { ...operation, outcomes: [...operation.outcomes, { skill_id: "same", state: "unchanged", update_id: null, code: "already_installed", detail: null }] };
  assert.match(text(component("SourceStatus", { status: "awaiting_review", summary: "install", operation: again })),
    /1 installed, 1 already installed, 1 held for review, 1 failed/);
});
test("source status shows no install summary unless the screen asks for it", () => {
  // Apply, check, undo, bulk apply, local import and recovery results use the same
  // outcome rows, and "installed" or "held for review" would misname them.
  const results = {
    apply: { operation_id: "a", state: "complete", committed: true, outcomes: [{ skill_id: "s", state: "applied", update_id: null, code: null }] },
    check: { operation_id: "c", state: "awaiting_review", committed: false, outcomes: [{ skill_id: "s", state: "awaiting_review", update_id: "u", code: null }] },
    bulk: { operation_id: "b", state: "failed", committed: true, outcomes: [{ skill_id: "s", state: "applied", update_id: null, code: null }, { skill_id: "t", state: "failed", update_id: null, code: "stale" }] },
  };
  for (const [name, operation] of Object.entries(results)) {
    const shown = text(component("SourceStatus", { status: operation.state, operation }));
    assert.doesNotMatch(shown, /installed|held for review/, name);
    assert.match(shown, /s: /, name);
  }
});
