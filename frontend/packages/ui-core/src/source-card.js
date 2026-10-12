import { createElement as h } from "react";
import { Button, EmptyState, Notice, Panel } from "./primitives.js";
import { boundedSourceText } from "./source-files.js";
import { SourceRepositoryLine } from "./source-repository.js";
import { sourceFlagLabels } from "./source-status.js";

const FP_FIELDS = ["content_generation", "binding_generation", "update_generation", "policy_version", "policy_digest", "local_digest", "candidate_digest"];
const sameFingerprint = (a, b) => !!a && !!b && FP_FIELDS.every(key => (a[key] ?? null) === (b[key] ?? null));
const TAKEN_KINDS = ["rule", "section", "example", "file"];
// A change row's element id. Every character outside [A-Za-z0-9-] is escaped, so two parts never share one.
const changeId = partId => "oms-source-change-" + [...partId].map(c => /[A-Za-z0-9-]/.test(c) ? c : `_${c.codePointAt(0).toString(16)}_`).join("");
const removes = change => change.action === "remove" || (change.incoming.kind === "absent" && change.local.kind !== "absent");

export const SOURCE_RISK_ORDER = Object.freeze(["injection", "check_failed", "check_unavailable", "personal_data", "removed_upstream", "your_edits", "unclear_identity", "script"]);
export const SOURCE_RISK_LABELS = Object.freeze({
  personal_data: "Looks like personal data", injection: "Looks like instructions to an AI", check_failed: "Failed a safety check",
  check_unavailable: "Could not be checked", your_edits: "You changed this in OMS", removed_upstream: "Removed on GitHub",
  unclear_identity: "OMS cannot match this to an existing part", script: "Script", other: "Needs a decision",
});
const CHECK_RISKS = ["injection", "check_failed", "check_unavailable"];
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
export function sourceFindingReasons(findings = []) {
  const groups = new Map();
  for (const finding of findings) groups.set(finding.category, [...(groups.get(finding.category) ?? []), finding]);
  return [...groups].map(([category, rows]) => {
    const [kind, ...rest] = category.split(":");
    const detail = words(rest.join(":"));
    if (category === "file_size") return "The file is larger than 10 MB";
    if (kind === "check_unavailable" && rest.length) return `A required check could not run (${detail})`;
    if (kind === "policy" && rest.length) return `Conflicts with a workspace safety rule (${detail})`;
    const noun = Object.hasOwn(FINDING_NOUNS, category) ? FINDING_NOUNS[category]
      : kind === "injection" && rest.length ? `instructions to an AI (${detail})` : null;
    if (!noun) return `Held by a check (${words(category)})`;
    const lines = [...new Set(rows.map(row => row.line).filter(Number.isInteger))].sort((a, b) => a - b);
    if (!lines.length) return `Looks like ${noun}`;
    const more = lines.length > 10 ? ` and ${lines.length - 10} more` : "";
    return `${lines.length} ${lines.length === 1 ? "line looks" : "lines look"} like ${noun}: ${lines.length === 1 ? "line" : "lines"} ${lines.slice(0, 10).join(", ")}${more}`;
  });
}

export function sourceChoiceOptions(update, change, part, conflict) {
  const allowed = (conflict?.allowed_choices ?? ["keep_oms", "use_upstream"]).filter(choice => choice !== "merged_text" || PROSE.includes(change.kind));
  const fresh = update.held_install === true || change.local.kind === "absent";
  const removal = removes(change);
  const options = [];
  if (allowed.includes("keep_oms")) options.push({ choice: "keep_oms", label: removal ? "Keep it" : fresh ? "Leave out" : "Keep yours" });
  if (allowed.includes("use_upstream") && !part.risks.some(risk => CHECK_RISKS.includes(risk))) {
    if (!part.risks.includes("personal_data")) options.push({ choice: "use_upstream", label: removal ? "Remove it in OMS too" : "Take GitHub's version" });
    else if (part.overridable.length) options.push({ choice: "use_upstream", acknowledged: [...part.overridable], label: "Take it anyway", note: "This keeps the personal data in the skill." });
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
  return [...SOURCE_RISK_ORDER, "other"].filter(risk => groups.has(risk)).map(risk => ({ risk, label: SOURCE_RISK_LABELS[risk], parts: groups.get(risk) }));
}

// The group forms of the spec labels (Global Constraints, ruling P5).
function groupLabel(risk, choice, count, fresh, acknowledged) {
  if (choice === "keep_oms") return risk === "removed_upstream" ? "Keep them" : risk === "script" ? "Leave them out"
    : fresh ? "Leave all out" : `Keep yours for all ${count}`;
  if (risk === "removed_upstream") return "Remove them in OMS too";
  if (risk === "script") return `Install these ${count} scripts`;
  if (acknowledged) return `Take all ${count} anyway`;
  return `Take GitHub's version for all ${count}`;
}
export function sourceGroupOptions(update, group) {
  const changes = new Map(update.plan.changes.map(change => [change.part_id, change]));
  const conflicts = new Map(update.plan.conflicts.map(conflict => [conflict.part_id, conflict]));
  const per = group.parts.map(part => sourceChoiceOptions(update, changes.get(part.part_id), part, conflicts.get(part.part_id)));
  const fresh = group.parts.every(part => update.held_install === true || changes.get(part.part_id).local.kind === "absent");
  return ["use_upstream", "keep_oms"].filter(choice => per.every(options => options.some(option => option.choice === choice)))
    .map(choice => {
      const first = per[0].find(option => option.choice === choice);
      // Every "anyway" form shows the note, the group's as well as each item's.
      return { choice, label: groupLabel(group.risk, choice, group.parts.length, fresh, !!first.acknowledged),
        ...(first.note ? { note: first.note } : {}), optionFor: index => per[index].find(option => option.choice === choice) };
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

export function sourceStatusSentence(update, state) {
  if (state.waiting) return `${plural(state.waiting, "item needs", "items need")} you.`;
  const verb = update.held_install === true ? "install" : "apply";
  if (update.summary?.whole_skill?.length) return `Held: see the reasons below before you ${verb}.`;
  return update.held_install === true ? "Ready to install." : "Ready to apply.";
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
  const groups = deletionGroups(update.plan.changes, choices);
  const saved = sameChoices(choices, update.drafts ?? []);
  // Only a complete saved selection can be held: until every decision is made, the server's hold
  // means "decisions required", which the status sentence already says.
  const held = saved && waiting === 0 && !!update.resolved_check && update.resolved_check.state !== "passed";
  // A held new skill needs at least one rule, section, example or file taken; the server refuses an empty apply anyway.
  const taken = update.held_install === true ? choices.some(choice => choice.choice !== "keep_oms" && TAKEN_KINDS.includes(changes.get(choice.part_id)?.kind)) : true;
  return { stale, validChoices, resolved, groups, saved, held, taken, choices, waiting,
    canApply: update.status === "open" && !stale && validChoices && resolved && waiting === 0 && taken && !held };
}

// Plain render helpers, called inline: the card stays hook-free and never mounts inner components.
function evidence(title, side) {
  const value = side.kind === "known" ? (typeof side.value === "string" ? side.value : JSON.stringify(side.value, null, 2))
    : side.kind === "absent" ? "Not present" : "Not known";
  const preview = boundedSourceText(value);
  return h("section", { className: "oms-source-side" }, h("h5", null, title),
    h("pre", { tabIndex: 0, "aria-label": title }, preview.text), preview.truncated ? h("p", null, "Bounded excerpt") : null);
}
function choiceLabel(option) {
  return [option.label, option.note ? h("span", { className: "oms-source-note" }, option.note) : null];
}

export function SourceUpdateCard({ update, title, draft: controlledDraft, partFingerprints = {}, ownership = {}, ref, now,
  onDraftChange, onApply, onSkip, onAdopt, onRecheck, onDeleteSkill, renderFile,
  busy = false, loading = false, error, unavailable, freshness = "fresh" }) {
  if (loading) return h(Notice, null, "Loading source comparison…");
  if (unavailable) return h(Notice, null, unavailable);
  if (!update) return error ? h(Notice, { kind: "error" }, error) : h(EmptyState, { title: "No source update" }, "There is no open update for this skill.");
  const closed = update.status !== "open";
  const draft = controlledDraft ?? initialSourceDraft(update);
  const state = sourceCardState(update, draft, partFingerprints);
  const disabled = busy || !!error || freshness !== "fresh" || state.stale || closed;
  const recheckDisabled = busy || !!error || closed;
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

  const comparison = change => change.kind === "file" && renderFile ? renderFile(change) : h("div", { className: "oms-source-columns" },
    change.base.kind === "known" ? evidence("Last installed", change.base) : null,
    heldInstall ? null : evidence("In OMS now", change.local),
    evidence("On GitHub", change.incoming));

  const item = part => {
    const change = changes.get(part.part_id);
    if (!change) return null;
    const identified = !!partFingerprints[part.part_id];
    const editable = canEdit && identified;
    const chosen = effective.get(part.part_id);
    const linked = change.linked_removals?.length ?? 0;
    const reasons = [...new Set([...part.risks.map(risk => SOURCE_RISK_LABELS[risk] ?? SOURCE_RISK_LABELS.other), ...sourceFindingReasons(part.findings),
      linked ? `Replaces ${plural(linked, "part", "parts")} that GitHub removed; taking it removes ${linked === 1 ? "that part" : "them"} in OMS.` : null])].filter(Boolean);
    return h("section", { key: part.part_id, id: changeId(part.part_id), tabIndex: -1, className: "oms-source-item", "aria-label": `Item ${part.label}` },
      h("h4", null, part.label),
      reasons.length ? h("ul", { className: "oms-source-reasons" }, reasons.map(reason => h("li", { key: reason }, reason))) : null,
      h("details", { className: "oms-source-show" }, h("summary", null, "Show"), comparison(change)),
      h("fieldset", { className: "oms-source-choices", role: "radiogroup", "aria-label": `Decision for ${part.part_id}` },
        optionsFor(part).map(option => h("label", { key: option.choice, className: "oms-source-check" },
          h("input", { type: "radio", name: `choice-${update.update_id}-${changeId(part.part_id)}`, "aria-label": `${option.label} for ${part.label}`,
            checked: isOption(chosen, option), disabled: !editable, onChange: () => decide(part.part_id, option) }),
          ...choiceLabel(option)))),
      chosen?.choice === "merged_text" ? h("label", { className: "oms-source-field" }, "Your text", h("textarea", {
        "aria-label": `Text for ${part.label}`, value: chosen.merged_text ?? "", rows: 6, disabled: !editable,
        onChange: event => decide(part.part_id, { choice: "merged_text" }, event.target.value) })) : null,
      identified ? null : h("p", null, "Part revision unavailable. Refresh before making choices."));
  };

  const group = row => {
    const members = row.parts;
    return h("section", { key: row.risk, className: "oms-source-group", "aria-label": `${row.label}: ${members.length} items` },
      h("h4", null, `${row.label} (${members.length})`),
      h("ul", null, members.map(part => h("li", { key: part.part_id }, part.label))),
      h("fieldset", { className: "oms-source-choices", role: "radiogroup", "aria-label": `Decision for every item in ${row.label}` },
        sourceGroupOptions(update, row).map(option => h("label", { key: option.choice, className: "oms-source-check" },
          h("input", { type: "radio", name: `group-${update.update_id}-${row.risk}`, "aria-label": option.label, disabled: !canEdit,
            checked: members.every((part, index) => isOption(effective.get(part.part_id), option.optionFor(index))),
            onChange: () => decideMany(members, option.optionFor) }),
          ...choiceLabel(option)))),
      h("details", { className: "oms-source-each" }, h("summary", null, "Choose each"), members.map(item)));
  };

  const needs = sourceNeedsYou(update);
  const safe = Object.values(parts).filter(part => part.attention === "safe" && changes.has(part.part_id));
  // Only automatic changes that write something are listed (ruling R8).
  const automatic = Object.values(parts).filter(part => part.attention === "automatic" && changes.has(part.part_id) && changes.get(part.part_id).action !== "keep");
  const safeSummary = sourceSafeSummary(update, state.choices);
  const safeItem = part => h("details", { key: part.part_id, className: "oms-source-safe-item" }, h("summary", null, part.label),
    comparison(changes.get(part.part_id)),
    h("label", { className: "oms-source-check" }, h("input", { type: "checkbox", "aria-label": `Leave out ${part.label}`,
      checked: effective.get(part.part_id)?.choice === "keep_oms",
      disabled: !canEdit || !partFingerprints[part.part_id] || !optionsFor(part).some(option => option.choice === "keep_oms"),
      onChange: event => leaveOut(part, event.target.checked) }), "Leave out"));
  const automaticItem = part => h("details", { key: part.part_id, className: "oms-source-safe-item" }, h("summary", null, part.label), comparison(changes.get(part.part_id)));

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

  return h(Panel, { title: heldInstall ? `New skill from GitHub: ${name}` : github ? `Update from GitHub: ${name}` : `Update from a local package: ${name}`,
    className: "oms-source-card", "aria-busy": busy },
    github ? h(SourceRepositoryLine, { repository: update.repository ?? null, now }) : null,
    closed ? h("p", null, "Recorded historical comparison. The OMS side shows content at review time. This update no longer accepts decisions.")
      : h("p", { role: "status", className: "oms-source-sentence" }, sourceStatusSentence(update, state)),
    !closed && error ? h(Notice, { kind: "error" }, error) : null,
    !closed && (state.stale || freshness === "stale") ? h(Notice, { kind: "error" }, "This card is out of date. Check safety again before deciding.") : null,
    !closed && freshness === "unavailable" ? h(Notice, null, "Freshness could not be verified. Decisions are unavailable.") : null,
    !closed && state.held ? h(Notice, null, "Required checks still hold this selection.") : null,
    !closed && !state.validChoices ? h(Notice, { kind: "error" }, "A choice is incomplete, no longer permitted or belongs to a different part revision.") : null,
    !closed && heldInstall && !state.taken ? h("p", { role: "status" }, "Choose at least one part to install, or delete this skill.") : null,
    !closed && update.summary?.whole_skill?.length ? h("section", { className: "oms-source-whole", "aria-label": "Held for the whole skill" },
      h("h3", null, "Held for the whole skill"),
      h("ul", null, update.summary.whole_skill.map((finding, index) => h("li", { key: `${finding.category}-${index}` }, finding.summary))),
      h("p", null, `These do not belong to one part. OMS checks them again when you ${verb}.`)) : null,
    !closed && needs.length ? h("section", { className: "oms-source-needs", "aria-label": "Needs you" }, h("h3", null, "Needs you"),
      needs.map(row => row.parts.length === 1 ? item(row.parts[0]) : group(row))) : null,
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
      } }, heldInstall ? "Install skill" : "Apply update") : null,
      heldInstall && onDeleteSkill ? h(Button, { variant: "secondary", disabled: busy, onClick: () => { if (!busy) onDeleteSkill(); } }, "Delete skill") : null,
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
