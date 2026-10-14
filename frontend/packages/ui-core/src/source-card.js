import { createElement as h, Fragment } from "react";
import { Button, EmptyState, Notice, Panel } from "./primitives.js";
import { boundedSourceText, numberedSourceText, revealSourceLine, sourceAbsentSides, sourceSideTitles, sourceTextLines } from "./source-files.js";
import { SourceRepositoryLine } from "./source-repository.js";
import { sourceFlagLabels } from "./source-status.js";

const FP_FIELDS = ["content_generation", "binding_generation", "update_generation", "policy_version", "policy_digest", "local_digest", "candidate_digest"];
const sameFingerprint = (a, b) => !!a && !!b && FP_FIELDS.every(key => (a[key] ?? null) === (b[key] ?? null));
const TAKEN_KINDS = ["rule", "section", "example", "file"];
// A change row's element id. Every character outside [A-Za-z0-9-] is escaped, so two parts never share one.
const escapeId = value => [...value].map(c => /[A-Za-z0-9-]/.test(c) ? c : `_${c.codePointAt(0).toString(16)}_`).join("");
const changeId = partId => "oms-source-change-" + escapeId(partId);
const removes = change => change.action === "remove" || (change.incoming.kind === "absent" && change.local.kind !== "absent");

export const SOURCE_RISK_ORDER = Object.freeze(["injection", "check_failed", "check_unavailable", "not_shown", "personal_data", "removed_upstream", "your_edits", "unclear_identity", "script"]);
export const SOURCE_RISK_LABELS = Object.freeze({
  personal_data: "Looks like personal data", injection: "Looks like instructions to an AI", check_failed: "Failed a safety check",
  check_unavailable: "Could not be checked", not_shown: "Some flagged words are not shown here in full, so this item cannot be taken as it is",
  your_edits: "You changed this in OMS", removed_upstream: "Removed on GitHub",
  unclear_identity: "OMS cannot match this to an existing part", script: "Script", other: "Needs a decision",
});
// A local package has no GitHub to remove a part from, so its group heading names the new package.
const LOCAL_RISK_LABELS = Object.freeze({ removed_upstream: "Removed in the new package" });
const riskLabel = (update, risk) => (update.plan.origin.kind === "local" ? LOCAL_RISK_LABELS[risk] : undefined) ?? SOURCE_RISK_LABELS[risk];
const CHECK_RISKS = ["injection", "check_failed", "check_unavailable"];
// An AI-instruction flag can be taken only where the server lists an injection category as overridable.
const ACKNOWLEDGES_INJECTION = part => part.overridable.some(name => name.startsWith("injection:"));
const BLOCKING_RISKS = part => CHECK_RISKS.filter(risk => risk !== "injection" || !ACKNOWLEDGES_INJECTION(part));
const PROSE = ["section", "rule", "example"];
const KIND_ORDER = ["rule", "section", "example", "file", "field", "placement"];
const KIND_NOUNS = { rule: ["rule", "rules"], section: ["section", "sections"], example: ["example", "examples"], file: ["file", "files"], field: ["skill detail", "skill details"], placement: ["placement change", "placement changes"] };
const OWNERSHIP = { source: "Source-owned", shared: "Shared: other owners are kept", local: "Local: local additions are kept", unknown: "Ownership not proven" };
const words = value => String(value).replaceAll("_", " ").replaceAll("-", " ");
const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`;
const listWords = items => items.length < 2 ? items.join("") : `${items.slice(0, -1).join(", ")} and ${items.at(-1)}`;
const capital = value => value.charAt(0).toUpperCase() + value.slice(1);
const acknowledgedKey = row => [...(row?.acknowledged ?? [])].sort().join(",");
const sameChoice = (a, b) => a.part_id === b.part_id && a.choice === b.choice && a.part_fingerprint === b.part_fingerprint
  && (a.merged_text ?? null) === (b.merged_text ?? null) && acknowledgedKey(a) === acknowledgedKey(b);
const sameChoices = (a, b) => a.length === b.length && a.every(choice => b.some(other => sameChoice(choice, other)));
const isOption = (choice, option) => !!choice && choice.choice === option.choice && acknowledgedKey(choice) === acknowledgedKey(option);
// "Edit the text" starts from GitHub's wording, flagged or not (ruling P4): the administrator
// redacts the flagged lines in the editor; the server checks the result before saving it.
const prefill = change => {
  for (const side of [change.incoming, change.local]) {
    if (side.kind !== "known") continue;
    if (typeof side.value === "string") return side.value;
    if (side.value && typeof side.value.body === "string") return side.value.body;
  }
  return "";
};

export function initialSourceDraft(update) {
  return { fingerprint: update.plan.fingerprint, choices: [...(update.drafts ?? [])], removal_consents: [] };
}
function deletionGroups(changes, choices) {
  const skipped = new Set(choices.filter(row => row.choice === "keep_oms").map(row => row.part_id));
  // Merged text keeps the local unit with new wording, so it is not a removal either (as on the server).
  const kept = new Set(choices.filter(row => row.choice === "keep_oms" || row.choice === "merged_text").map(row => row.part_id));
  const required = new Set(changes.filter(change => removes(change) && !kept.has(change.part_id)).map(change => change.part_id));
  for (const change of changes) if (!skipped.has(change.part_id)) {
    for (const id of change.linked_removals ?? []) if (!kept.has(id)) required.add(id);
  }
  const groups = [...required].map(id => new Set([id]));
  for (const change of changes) {
    const related = [change.part_id, ...(change.linked_removals ?? [])].filter(id => required.has(id));
    const merged = groups.filter(group => related.some(id => group.has(id)));
    if (merged.length < 2) continue;
    const combined = new Set(merged.flatMap(group => [...group]));
    for (const group of merged) groups.splice(groups.indexOf(group), 1);
    groups.push(combined);
  }
  return groups.map(group => [...group]);
}

export function sourceReviewParts(update) {
  if (update.parts) return update.parts;
  // A server without triage: every conflict or removal needs a decision, labelled by its part id.
  const conflicts = new Set(update.plan.conflicts.map(row => row.part_id));
  return Object.fromEntries(update.plan.changes.map(change => {
    const decide = conflicts.has(change.part_id) || removes(change) || !!change.linked_removals?.length;
    return [change.part_id, { part_id: change.part_id, kind: change.kind, label: change.part_id, attention: decide ? "decide" : "automatic",
      risks: removes(change) ? ["removed_upstream"] : [], default: null, overridable: [], findings: [] }];
  }));
}

const FINDING_NOUNS = { phone: "a phone number", email: "an email address" };
// Each reason as text, and for a reason that names lines, its parts: the line numbers become buttons on the card.
function findingReasonRows(findings = []) {
  const groups = new Map();
  for (const finding of findings) groups.set(finding.category, [...(groups.get(finding.category) ?? []), finding]);
  return [...groups].map(([category, rows]) => {
    const [kind, ...rest] = category.split(":");
    const detail = words(rest.join(":"));
    if (category === "file_size") return { text: "The file is larger than the size limit for one file" };
    if (kind === "check_unavailable" && rest.length) return { text: `A required check could not run (${detail})` };
    if (kind === "policy" && rest.length) return { text: `Conflicts with a workspace safety rule (${detail})` };
    const noun = Object.hasOwn(FINDING_NOUNS, category) ? FINDING_NOUNS[category]
      : kind === "injection" && rest.length ? `instructions to an AI (${detail})` : null;
    if (!noun) return { text: `Held by a check (${words(category)})` };
    const lines = [...new Set(rows.map(row => row.line).filter(Number.isInteger))].sort((a, b) => a - b);
    if (!lines.length) return { text: `Looks like ${noun}` };
    const shown = lines.slice(0, 10);
    const more = lines.length > 10 ? ` and ${lines.length - 10} more` : "";
    const prefix = `${lines.length} ${lines.length === 1 ? "line looks" : "lines look"} like ${noun}: ${lines.length === 1 ? "line" : "lines"} `;
    return { text: `${prefix}${shown.join(", ")}${more}`, prefix, lines: shown, more };
  });
}
export function sourceFindingReasons(findings = []) {
  return findingReasonRows(findings).map(row => row.text);
}

// The tag a flagged line carries in the preview: the plain words of the server's summary, never the matched text.
const LINE_TAGS = { phone: "looks like a phone number", email: "looks like an email address" };
const lineTag = category => Object.hasOwn(LINE_TAGS, category) ? LINE_TAGS[category]
  : category.split(":")[0] === "injection" ? "looks like an instruction to an AI model" : "held by a check";
export function sourceFlaggedLines(part) {
  // A part's own findings: a file's by its path, a rule's, section's or example's by its part id.
  const path = part.kind === "file" && part.part_id.startsWith("file:") ? part.part_id.slice(5) : null;
  const tags = new Map();
  for (const finding of part.findings ?? []) {
    if (!Number.isInteger(finding.line) || !(finding.part_id === part.part_id || (path !== null && finding.path === path))) continue;
    tags.set(finding.line, new Set([...(tags.get(finding.line) ?? []), lineTag(finding.category)]));
  }
  return [...tags].sort(([a], [b]) => a - b).map(([line, labels]) => ({ line, label: [...labels].join("; ") }));
}
// Opening a part's "Show" scrolls its GitHub preview to the asked-for or first flagged line (event
// handlers only: the card stays hook-free).
function revealOnToggle(event) {
  const details = event.currentTarget;
  if (details.open) revealSourceLine(details);
  else delete details.dataset.omsLine;
}
// A line-number button opens its item's "Show" at that line; the console routes by hash, so no link is used.
const showLine = line => event => {
  const details = event.currentTarget.closest(".oms-source-item")?.querySelector("details.oms-source-show");
  if (!details) return;
  details.dataset.omsLine = String(line);
  if (details.open) revealSourceLine(details);
  else details.open = true;
};
const SCRIPT_NOTE = "Agents can run these files when they use the skill (choosing them adds them to the skill's files as the author intended)";

// The take choice names where the new version comes from (ruling FB-1).
const takeLabel = update => update.plan.origin.kind === "github" ? "Take GitHub's version" : "Take the package's version";
export function sourceChoiceOptions(update, change, part, conflict) {
  const allowed = (conflict?.allowed_choices ?? ["keep_oms", "use_upstream"]).filter(choice => choice !== "merged_text" || PROSE.includes(change.kind));
  const fresh = update.held_install === true || change.local.kind === "absent";
  const removal = removes(change);
  const options = [];
  if (allowed.includes("keep_oms")) options.push({ choice: "keep_oms", label: removal ? "Keep it" : fresh ? "Leave out" : "Keep yours" });
  if (allowed.includes("use_upstream") && !part.risks.some(risk => BLOCKING_RISKS(part).includes(risk))) {
    if (!part.risks.includes("personal_data") && !part.risks.includes("injection")) options.push({ choice: "use_upstream", label: removal ? "Remove it in OMS too" : takeLabel(update) });
    else if (part.overridable.length) options.push({ choice: "use_upstream", acknowledged: [...part.overridable], label: "Take it anyway", note: ACKNOWLEDGES_INJECTION(part) ? "This keeps the flagged text in the skill." : "This keeps the personal data in the skill." });
  }
  if (allowed.includes("merged_text")) options.push({ choice: "merged_text", label: "Edit the text" });
  return options;
}

export function sourceEffectiveChoices(update, draft = initialSourceDraft(update), partFingerprints = {}) {
  const parts = sourceReviewParts(update);
  const explicit = new Set(draft.choices.map(choice => choice.part_id));
  const result = [...draft.choices];
  for (const conflict of update.plan.conflicts) {
    const part = parts[conflict.part_id];
    if (!explicit.has(conflict.part_id) && part?.attention === "safe" && part.default === "use_upstream" && partFingerprints[conflict.part_id])
      result.push({ part_id: conflict.part_id, part_fingerprint: partFingerprints[conflict.part_id], choice: "use_upstream", merged_text: null });
  }
  return result;
}

export function sourceNeedsYou(update) {
  const groups = new Map();
  for (const part of Object.values(sourceReviewParts(update))) {
    if (part.attention !== "decide") continue;
    const risk = SOURCE_RISK_ORDER.find(code => part.risks.includes(code)) ?? "other";
    groups.set(risk, [...(groups.get(risk) ?? []), part]);
  }
  return [...SOURCE_RISK_ORDER, "other"].filter(risk => groups.has(risk)).map(risk => ({ risk, label: riskLabel(update, risk), parts: groups.get(risk) }));
}

// The group forms of the spec labels (Global Constraints, ruling P5); scripts use Chris's wording (follow-up B).
function groupLabel(risk, choice, count, fresh, acknowledged, take) {
  if (choice === "keep_oms") return risk === "removed_upstream" ? "Keep them" : risk === "script" ? (count === 1 ? "Leave it out" : "Leave them out")
    : fresh ? "Leave all out" : `Keep yours for all ${count}`;
  if (risk === "removed_upstream") return "Remove them in OMS too";
  if (risk === "script") return count === 1 ? "Include this script in the skill" : `Include these ${count} scripts in the skill`;
  if (acknowledged) return `Take all ${count} anyway`;
  return `${take} for all ${count}`;
}
export function sourceGroupOptions(update, group) {
  const changes = new Map(update.plan.changes.map(change => [change.part_id, change]));
  const conflicts = new Map(update.plan.conflicts.map(conflict => [conflict.part_id, conflict]));
  const per = group.parts.map(part => sourceChoiceOptions(update, changes.get(part.part_id), part, conflicts.get(part.part_id)));
  const fresh = group.parts.every(part => update.held_install === true || changes.get(part.part_id).local.kind === "absent");
  // Taking a replacement removes the parts it replaces; the group says so, not only "Choose each" (ruling T6-4).
  const linked = new Set(group.parts.flatMap(part => changes.get(part.part_id).linked_removals ?? []));
  // AI-instruction flags are taken part by part: a group never offers "Take all N anyway" for them.
  const injected = per.some(options => options.some(option => option.choice === "use_upstream" && option.acknowledged?.some(name => name.startsWith("injection:"))));
  return ["use_upstream", "keep_oms"].filter(choice => per.every(options => options.some(option => option.choice === choice)) && !(injected && choice === "use_upstream"))
    .map(choice => {
      const first = per[0].find(option => option.choice === choice);
      // Every "anyway" form shows the note, the group's as well as each item's.
      return { choice, label: groupLabel(group.risk, choice, group.parts.length, fresh, !!first.acknowledged, takeLabel(update)),
        ...(first.note ? { note: first.note } : {}),
        ...(choice === "use_upstream" && linked.size ? { removalNote: `This also removes ${plural(linked.size, "part", "parts")} in OMS.` } : {}),
        optionFor: index => per[index].find(option => option.choice === choice) };
    });
}

export function sourceSafeSummary(update, choices) {
  const safe = Object.values(sourceReviewParts(update)).filter(part => part.attention === "safe");
  if (!safe.length) return "";
  const listed = listWords(KIND_ORDER.map(kind => [kind, safe.filter(part => part.kind === kind).length]).filter(([, count]) => count)
    .map(([kind, count]) => plural(count, ...KIND_NOUNS[kind])));
  const left = safe.filter(part => choices.some(choice => choice.part_id === part.part_id && choice.choice === "keep_oms")).length;
  return `${listed} passed the checks. ${safe.length === 1 ? "It" : "They"} will be ${update.held_install === true ? "installed" : "applied"}.${left ? ` ${left} left out.` : ""}`;
}

// Ruling T6-1: "Ready" only when the primary action is enabled; otherwise the first reason it is not.
export function sourceStatusSentence(update, state) {
  const verb = update.held_install === true ? "install" : "apply";
  if (state.stale) return "This card is out of date. Check safety again before deciding.";
  if (state.waitingItems) return `${plural(state.waitingItems, "item needs", "items need")} you.`;
  if (state.held || update.summary?.whole_skill?.length) return `Held: see the reasons below before you ${verb}.`;
  if (update.held_install === true && !state.taken) return "Choose at least one part to install, or delete this skill.";
  if (state.canApply) return `Ready to ${verb}.`;
  return `Not ready to ${verb}. See the notes below.`;
}

// Follow-up C: what the host says once a one-step install or apply succeeded and its card has left.
export function sourceInstalledMessage(update, title) {
  const name = title || update.plan.skill.skill_id;
  return update.held_install === true ? `${name} installed.` : `Update applied to ${name}.`;
}

export function sourceCardState(update, draft = initialSourceDraft(update), partFingerprints = {}) {
  const parts = sourceReviewParts(update);
  const changes = new Map(update.plan.changes.map(change => [change.part_id, change]));
  const conflicts = new Map(update.plan.conflicts.map(conflict => [conflict.part_id, conflict]));
  const stale = update.stale === true || !sameFingerprint(draft.fingerprint, update.plan.fingerprint);
  const validChoices = new Set(draft.choices.map(choice => choice.part_id)).size === draft.choices.length && draft.choices.every(choice => {
    const change = changes.get(choice.part_id), part = parts[choice.part_id];
    if (!change || !part || !partFingerprints[choice.part_id] || partFingerprints[choice.part_id] !== choice.part_fingerprint) return false;
    const option = sourceChoiceOptions(update, change, part, conflicts.get(choice.part_id)).find(row => isOption(choice, row));
    return !!option && (choice.choice === "merged_text" ? typeof choice.merged_text === "string" && !!choice.merged_text.trim() : choice.merged_text == null);
  });
  const choices = sourceEffectiveChoices(update, draft, partFingerprints);
  const chosen = new Set(choices.map(choice => choice.part_id));
  const resolved = update.plan.conflicts.every(conflict => chosen.has(conflict.part_id))
    && update.plan.changes.every(change => change.action !== "conflict" || conflicts.has(change.part_id));
  const waiting = Object.values(parts).filter(part => part.attention === "decide" && !chosen.has(part.part_id)).length;
  // The decisions still open, as the card asks them: a group answered with one choice is one item (ruling F4).
  const waitingItems = sourceNeedsYou(update).filter(row => row.parts.some(part => !chosen.has(part.part_id))).length;
  const groups = deletionGroups(update.plan.changes, choices);
  const saved = sameChoices(choices, update.drafts ?? []);
  // Only a complete saved selection can be held: until every decision is made, the server's hold
  // means "decisions required", which the status sentence already says.
  const held = saved && waiting === 0 && !!update.resolved_check && update.resolved_check.state !== "passed";
  // A held new skill needs at least one rule, section, example or file taken; the server refuses an empty apply anyway.
  const taken = update.held_install === true ? choices.some(choice => choice.choice !== "keep_oms" && TAKEN_KINDS.includes(changes.get(choice.part_id)?.kind)) : true;
  return { stale, validChoices, resolved, groups, saved, held, taken, choices, waiting, waitingItems,
    canApply: update.status === "open" && !stale && validChoices && resolved && waiting === 0 && taken && !held };
}

// Ruling F4: a local draft made on an older revision of the card keeps each choice the card still
// offers for the same part revision, drops the rest and takes the card's fingerprint, so one changed
// part never leaves the whole card out of date. A matching draft, or a card the server calls stale,
// keeps the draft as it is.
export function carrySourceDraft(draft, update, partFingerprints = {}) {
  if (!draft || !update || update.stale === true || sameFingerprint(draft.fingerprint, update.plan.fingerprint)) return draft;
  const parts = sourceReviewParts(update);
  const changes = new Map(update.plan.changes.map(change => [change.part_id, change]));
  const conflicts = new Map(update.plan.conflicts.map(conflict => [conflict.part_id, conflict]));
  const choices = draft.choices.filter(choice => {
    const change = changes.get(choice.part_id), part = parts[choice.part_id];
    return !!change && !!part && !!partFingerprints[choice.part_id] && partFingerprints[choice.part_id] === choice.part_fingerprint
      && sourceChoiceOptions(update, change, part, conflicts.get(choice.part_id)).some(option => isOption(choice, option));
  });
  return { ...draft, fingerprint: update.plan.fingerprint, choices, removal_consents: [] };
}

// Plain render helpers, called inline: the card stays hook-free and never mounts inner components.
// `flagged` is set for the GitHub side: prose text there shows line numbers and its flagged lines.
function evidence(title, side, flagged = null) {
  const value = side.kind === "known" ? (typeof side.value === "string" ? side.value : JSON.stringify(side.value, null, 2))
    : side.kind === "absent" ? "Not present" : "Not known";
  // Only prose text is numbered: the lines of a structured value's JSON are not the lines a check counted.
  const numbered = flagged && side.kind === "known" && typeof side.value === "string" ? sourceTextLines(side.value) : null;
  const preview = numbered ?? boundedSourceText(value);
  return h("section", { className: "oms-source-side" }, h("h5", null, title),
    numbered ? numberedSourceText(numbered, title, flagged) : h("pre", { tabIndex: 0, "aria-label": title }, preview.text),
    preview.truncated ? h("p", null, "Bounded excerpt") : null);
}
// A choice's label and notes; each note has an id its radio names in aria-describedby (ruling T6-4).
function choiceNotes(option, idBase) {
  const notes = [["note", option.note], ["removal", option.removalNote]].filter(([, note]) => note)
    .map(([kind, note]) => ({ id: `${idBase}-${kind}`, note }));
  return { describedBy: notes.length ? notes.map(row => row.id).join(" ") : undefined,
    children: [option.label, ...notes.map(row => h("span", { key: row.id, id: row.id, className: "oms-source-note" }, row.note))] };
}

export function SourceUpdateCard({ update, title, draft: controlledDraft, partFingerprints = {}, ownership = {}, ref, now,
  onDraftChange, onApply, onSkip, onAdopt, onRecheck, onDeleteSkill, renderFile,
  busy = false, installing = false, loading = false, error, unavailable, freshness = "fresh" }) {
  if (loading) return h(Notice, null, "Loading source comparison…");
  if (unavailable) return h(Notice, null, unavailable);
  if (!update) return error ? h(Notice, { kind: "error" }, error) : h(EmptyState, { title: "No source update" }, "There is no open update for this skill.");
  const closed = update.status !== "open";
  const draft = controlledDraft ?? initialSourceDraft(update);
  const state = sourceCardState(update, draft, partFingerprints);
  // Follow-up C: while the host's one-step install runs, every control waits, whatever else the host says.
  const working = busy || installing;
  const disabled = working || !!error || freshness !== "fresh" || state.stale || closed;
  const recheckDisabled = working || !!error || closed;
  const canEdit = !disabled && !!onDraftChange;
  const heldInstall = update.held_install === true;
  const github = update.plan.origin.kind === "github";
  const name = title || update.plan.skill.skill_id;
  const body = { skill_id: update.plan.skill.skill_id, fingerprint: update.plan.fingerprint };
  const parts = sourceReviewParts(update);
  const changes = new Map(update.plan.changes.map(change => [change.part_id, change]));
  const conflicts = new Map(update.plan.conflicts.map(conflict => [conflict.part_id, conflict]));
  const effective = new Map(state.choices.map(choice => [choice.part_id, choice]));
  const optionsFor = part => sourceChoiceOptions(update, changes.get(part.part_id), part, conflicts.get(part.part_id));

  const toChoice = (part_id, option, merged_text) => ({ part_id, part_fingerprint: partFingerprints[part_id], choice: option.choice,
    merged_text: option.choice === "merged_text" ? merged_text ?? prefill(changes.get(part_id)) : null,
    ...(option.acknowledged ? { acknowledged: [...option.acknowledged] } : {}) });
  const decide = (part_id, option, merged_text) => {
    if (!canEdit || !partFingerprints[part_id]) return;
    const choices = draft.choices.filter(row => row.part_id !== part_id);
    choices.push(toChoice(part_id, option, merged_text));
    onDraftChange({ ...draft, choices });
  };
  const decideMany = (members, optionFor) => {
    if (!canEdit) return;
    let choices = [...draft.choices];
    members.forEach((part, index) => {
      if (!partFingerprints[part.part_id]) return;
      choices = choices.filter(row => row.part_id !== part.part_id);
      choices.push(toChoice(part.part_id, optionFor(index)));
    });
    onDraftChange({ ...draft, choices });
  };
  const leaveOut = (part, checked) => {
    if (!canEdit || !partFingerprints[part.part_id]) return;
    const choices = draft.choices.filter(row => row.part_id !== part.part_id);
    if (checked) choices.push({ part_id: part.part_id, part_fingerprint: partFingerprints[part.part_id], choice: "keep_oms", merged_text: null });
    onDraftChange({ ...draft, choices });
  };
  const act = (callback, payload) => () => { if (!disabled && callback) callback(payload); };

  // The file preview comes from the host; it gets the part's flagged lines and the sides OMS has no copy of.
  const origin = github ? "github" : "local";
  const titles = sourceSideTitles(origin);
  const comparison = (change, part) => {
    const flagged = part ? sourceFlaggedLines(part) : [];
    const absent = sourceAbsentSides(change, heldInstall);
    if (change.kind === "file" && renderFile) return renderFile(change, { flagged, absent, origin });
    const fresh = absent.includes("base") && absent.includes("local");
    return h(Fragment, null,
      fresh ? h("p", { className: "oms-source-new" }, change.kind === "file" ? "This file is new. OMS does not have it yet." : "This is new. OMS does not have it yet.") : null,
      h("div", { className: fresh ? "oms-source-columns oms-source-columns-single" : "oms-source-columns" },
        fresh || change.base.kind !== "known" ? null : evidence(titles.base, change.base),
        fresh || heldInstall ? null : evidence(titles.local, change.local),
        evidence(titles.upstream, change.incoming, flagged)));
  };

  const item = part => {
    const change = changes.get(part.part_id);
    if (!change) return null;
    const identified = !!partFingerprints[part.part_id];
    const editable = canEdit && identified;
    const chosen = effective.get(part.part_id);
    const linked = change.linked_removals?.length ?? 0;
    const reasons = new Map([...part.risks.map(risk => ({ text: riskLabel(update, risk) ?? SOURCE_RISK_LABELS.other })), ...findingReasonRows(part.findings),
      linked ? { text: `Replaces ${plural(linked, "part", "parts")} that ${github ? "GitHub" : "the new package"} removed; taking it removes ${linked === 1 ? "that part" : "them"} in OMS.` } : null]
      .filter(Boolean).map(reason => [reason.text, reason]));
    // Each line number is a button that opens "Show" at that line; the reason still never quotes the flagged text (ruling P4).
    // Only a host's file preview or prose text is numbered, so other line numbers stay plain text (ruling FB-2).
    const numbered = change.incoming.kind === "known" && (change.kind === "file" ? !!renderFile : typeof change.incoming.value === "string");
    const reasonLine = reason => reason.lines && numbered ? [reason.prefix, ...reason.lines.flatMap((line, index) => [index ? ", " : null,
      h("button", { key: line, type: "button", className: "oms-source-line-link", "aria-label": `Show line ${line} of ${part.label}`, onClick: showLine(line) }, String(line))]),
    reason.more] : [reason.text];
    return h("section", { key: part.part_id, id: changeId(part.part_id), tabIndex: -1, className: "oms-source-item", "aria-label": `Item ${part.label}` },
      h("h4", null, part.label),
      reasons.size ? h("ul", { className: "oms-source-reasons" }, [...reasons.values()].map(reason => h("li", { key: reason.text }, ...reasonLine(reason)))) : null,
      h("details", { className: "oms-source-show", onToggle: revealOnToggle }, h("summary", null, "Show"), comparison(change, part)),
      h("fieldset", { className: "oms-source-choices", role: "radiogroup", "aria-label": `Decision for ${part.label}` },
        optionsFor(part).map(option => {
          const notes = choiceNotes(option, `${changeId(part.part_id)}-${escapeId(update.update_id)}-${option.choice}`);
          return h("label", { key: option.choice, className: "oms-source-check" },
            h("input", { type: "radio", name: `choice-${update.update_id}-${changeId(part.part_id)}`, "aria-label": `${option.label} for ${part.label}`,
              "aria-describedby": notes.describedBy, checked: isOption(chosen, option), disabled: !editable, onChange: () => decide(part.part_id, option) }),
            ...notes.children);
        })),
      chosen?.choice === "merged_text" ? h("label", { className: "oms-source-field" }, "Your text", h("textarea", {
        "aria-label": `Text for ${part.label}`, value: chosen.merged_text ?? "", rows: 6, disabled: !editable,
        onChange: event => decide(part.part_id, { choice: "merged_text" }, event.target.value) })) : null,
      identified ? null : h("p", null, "Part revision unavailable. Refresh before making choices."));
  };

  const group = row => {
    const members = row.parts;
    const scripts = row.risk === "script";
    const name = scripts && members.length > 1 ? "Scripts" : row.label;
    return h("section", { key: row.risk, className: "oms-source-group", "aria-label": `${name}: ${plural(members.length, "item", "items")}` },
      h("h4", null, `${name} (${members.length})`),
      scripts ? h("p", { className: "oms-source-group-note" }, SCRIPT_NOTE) : null,
      h("ul", null, members.map(part => h("li", { key: part.part_id }, part.label))),
      h("fieldset", { className: "oms-source-choices", role: "radiogroup", "aria-label": `Decision for every item in ${name}` },
        sourceGroupOptions(update, row).map(option => {
          const notes = choiceNotes(option, `oms-source-group-${escapeId(update.update_id)}-${row.risk}-${option.choice}`);
          return h("label", { key: option.choice, className: "oms-source-check" },
            h("input", { type: "radio", name: `group-${update.update_id}-${row.risk}`, "aria-label": option.label, "aria-describedby": notes.describedBy,
              disabled: !canEdit, checked: members.every((part, index) => isOption(effective.get(part.part_id), option.optionFor(index))),
              onChange: () => decideMany(members, option.optionFor) }),
            ...notes.children);
        })),
      h("details", { className: "oms-source-each" }, h("summary", null, "Choose each"), members.map(item)));
  };

  const needs = sourceNeedsYou(update);
  const safe = Object.values(parts).filter(part => part.attention === "safe" && changes.has(part.part_id));
  // Only automatic changes that write something are listed (ruling R8).
  const automatic = Object.values(parts).filter(part => part.attention === "automatic" && changes.has(part.part_id) && changes.get(part.part_id).action !== "keep");
  const safeSummary = sourceSafeSummary(update, state.choices);
  const safeItem = part => h("details", { key: part.part_id, className: "oms-source-safe-item" }, h("summary", null, part.label),
    comparison(changes.get(part.part_id), part),
    h("label", { className: "oms-source-check" }, h("input", { type: "checkbox", "aria-label": `Leave out ${part.label}`,
      checked: effective.get(part.part_id)?.choice === "keep_oms",
      disabled: !canEdit || !partFingerprints[part.part_id] || !optionsFor(part).some(option => option.choice === "keep_oms"),
      onChange: event => leaveOut(part, event.target.checked) }), "Leave out"));
  const automaticItem = part => h("details", { key: part.part_id, className: "oms-source-safe-item" }, h("summary", null, part.label), comparison(changes.get(part.part_id), part));

  // The decision column: the effective choice on an open card, the recorded one on a closed card.
  const decisionLabel = change => {
    const choice = closed ? draft.choices.find(row => row.part_id === change.part_id) : effective.get(change.part_id);
    const part = parts[change.part_id];
    if (!choice || !part) return "";
    const options = optionsFor(part);
    return (options.find(option => isOption(choice, option)) ?? options.find(option => option.choice === choice.choice))?.label ?? "";
  };
  const owner = id => Object.hasOwn(OWNERSHIP, ownership[id] ?? "") ? OWNERSHIP[ownership[id]] : OWNERSHIP.unknown;
  const flags = update.plan.flags.map(flag => Object.hasOwn(sourceFlagLabels, flag) ? sourceFlagLabels[flag] : words(flag));
  const fact = (term, ...detail) => h("div", { key: term }, h("dt", null, term), h("dd", null, ...detail));
  const fp = update.plan.fingerprint;
  const verb = heldInstall ? "install" : "apply";

  return h(Panel, { title: heldInstall ? `New skill from ${github ? "GitHub" : "a local package"}: ${name}` : github ? `Update from GitHub: ${name}` : `Update from a local package: ${name}`,
    className: "oms-source-card", "aria-busy": working },
    github ? h(SourceRepositoryLine, { repository: update.repository ?? null, now }) : null,
    closed ? h("p", null, "Recorded historical comparison. The OMS side shows content at review time. This update no longer accepts decisions.")
      // While the one-step install runs the same status line says so (follow-up C), so it is announced where the reader looks.
      : installing ? h("p", { role: "status", className: "oms-source-sentence oms-source-installing" },
        heldInstall ? `Installing ${name}…` : `Applying the update to ${name}…`)
      // The sentence carries the out-of-date and nothing-taken wording, so neither repeats below it.
      : h("p", { role: "status", className: "oms-source-sentence" }, sourceStatusSentence(update,
        { ...state, stale: state.stale || freshness === "stale", canApply: state.canApply && freshness === "fresh" && !error })),
    !closed && error ? h(Notice, { kind: "error" }, error) : null,
    !closed && freshness === "unavailable" ? h(Notice, null, "Freshness could not be verified. Decisions are unavailable.") : null,
    !closed && state.held ? h(Notice, null, "Required checks still hold this selection.") : null,
    !closed && !state.validChoices ? h(Notice, { kind: "error" }, "A choice is incomplete, no longer permitted or belongs to a different part revision.") : null,
    !closed && update.summary?.whole_skill?.length ? h("section", { className: "oms-source-whole", "aria-label": "Held for the whole skill" },
      h("h3", null, "Held for the whole skill"),
      h("ul", null, update.summary.whole_skill.map((finding, index) => h("li", { key: `${finding.category}-${index}` }, finding.summary))),
      h("p", null, `These do not belong to one part. OMS checks them again when you ${verb}.`)) : null,
    !closed && needs.length ? h("section", { className: "oms-source-needs", "aria-label": "Needs you" }, h("h3", null, "Needs you"),
      // Scripts are always one group decision, even a single script (follow-up B).
      needs.map(row => row.parts.length === 1 && row.risk !== "script" ? item(row.parts[0]) : group(row))) : null,
    !closed && (safe.length || automatic.length) ? h("section", { className: "oms-source-safe", "aria-label": "Passed the checks" },
      safeSummary ? h("p", null, safeSummary) : null,
      automatic.length ? h("p", null, `${plural(automatic.length, "other change applies", "other changes apply")} with the update.`) : null,
      h("details", { className: "oms-source-list" }, h("summary", null, "Show"),
        KIND_ORDER.map(kind => [kind, safe.filter(part => part.kind === kind)]).filter(([, members]) => members.length)
          .map(([kind, members]) => h("div", { key: kind }, h("h5", null, `${capital(KIND_NOUNS[kind][1])} (${members.length})`), members.map(safeItem))),
        automatic.length ? h("div", null, h("h5", null, "Applied with the update"), automatic.map(automaticItem)) : null)) : null,
    !closed ? h("div", { className: "oms-source-actions" },
      onApply ? h(Button, { disabled: disabled || !state.canApply, onClick: () => {
        if (!disabled && state.canApply) onApply({ ...body, choices: state.choices, removal_consents: [...new Set(state.groups.flat())], save: !state.saved });
      } }, installing ? (heldInstall ? "Installing…" : "Applying…") : heldInstall ? "Install skill" : "Apply update") : null,
      heldInstall && onDeleteSkill ? h(Button, { variant: "secondary", disabled: working, onClick: () => { if (!working) onDeleteSkill(); } }, "Delete skill") : null,
      onSkip && !heldInstall ? h(Button, { variant: "secondary", disabled, onClick: act(onSkip, body) }, "Skip this version") : null,
      onAdopt && !heldInstall ? h(Button, { variant: "secondary", disabled, onClick: act(onAdopt, body) }, "Keep my version and mark this update as seen") : null,
      onRecheck ? h(Button, { variant: "secondary", disabled: recheckDisabled, onClick: () => { if (!recheckDisabled) onRecheck(body); } }, "Check safety again") : null) : null,
    h("details", { className: "oms-source-technical" }, h("summary", null, "Technical details"),
      h("dl", { className: "oms-source-metadata" },
        fact("Origin", update.plan.origin.kind),
        fact("Base snapshot", update.plan.base?.snapshot_id || "Unknown (first reconciliation)"),
        fact("Incoming snapshot", update.plan.incoming.snapshot_id),
        fact("Review revision", `${update.generation} · content ${fp.content_generation} · binding ${fp.binding_generation}`),
        fact("Flags", flags.length ? flags.join("; ") : "None"),
        ref ? fact("Ref", `${ref.kind}: ${ref.name}`, ...(ref.commit ? [" ", h("code", null, ref.commit)] : [])) : null),
      h("div", { className: "oms-source-table" }, h("table", { className: "oms-source-parts" },
        h("thead", null, h("tr", null, ["Part", "Kind", "Action", "Reason", "Ownership", "Decision"].map(column => h("th", { key: column, scope: "col" }, column)))),
        h("tbody", null, update.plan.changes.map(change => h("tr", { key: change.part_id },
          h("td", null, change.part_id), h("td", null, change.kind), h("td", null, change.action),
          h("td", null, conflicts.get(change.part_id)?.reason ?? ""), h("td", null, owner(change.part_id)), h("td", null, decisionLabel(change)))))))));
}
