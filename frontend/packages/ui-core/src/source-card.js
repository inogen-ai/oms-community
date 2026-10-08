import { createElement as h } from "react";
import { Button, EmptyState, Notice, Panel } from "./primitives.js";
import { boundedSourceText } from "./source-files.js";
import { SourceStatus, sourceFlagLabels } from "./source-status.js";

const CHOICES = { keep_oms: "Keep OMS", use_upstream: "Use upstream", merged_text: "Merged text" };
const OWNERSHIP = { source: "Source-owned content", shared: "Shared ownership — other owners are preserved", local: "Local ownership — preserve local additions", unknown: "Ownership not proven" };
const FP_FIELDS = ["content_generation", "binding_generation", "update_generation", "policy_version", "policy_digest", "local_digest", "candidate_digest"];
const sameFingerprint = (a, b) => !!a && !!b && FP_FIELDS.every(key => (a[key] ?? null) === (b[key] ?? null));
const sameChoices = (a, b) => a.length === b.length && a.every(choice => b.some(other =>
  choice.part_id === other.part_id && choice.choice === other.choice && choice.part_fingerprint === other.part_fingerprint
  && (choice.merged_text ?? null) === (other.merged_text ?? null)));
const allowedChoices = (change, conflict) => (conflict?.allowed_choices ?? ["keep_oms", "use_upstream"])
  .filter(choice => Object.hasOwn(CHOICES, choice) && (choice !== "merged_text" || ["section", "rule", "example"].includes(change.kind)));
const removes = change => change.action === "remove" || (change.incoming.kind === "absent" && change.local.kind !== "absent");

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
export function sourceCardState(update, draft = initialSourceDraft(update), partFingerprints = {}) {
  const changes = new Map(update.plan.changes.map(change => [change.part_id, change]));
  const conflicts = new Map(update.plan.conflicts.map(conflict => [conflict.part_id, conflict]));
  const stale = update.stale === true || !sameFingerprint(draft.fingerprint, update.plan.fingerprint);
  const validChoices = draft.choices.every(choice => {
    const change = changes.get(choice.part_id);
    return change && partFingerprints[choice.part_id] && partFingerprints[choice.part_id] === choice.part_fingerprint
      && allowedChoices(change, conflicts.get(choice.part_id)).includes(choice.choice)
      && (choice.choice === "merged_text" ? typeof choice.merged_text === "string" && !!choice.merged_text.trim() : choice.merged_text == null);
  }) && new Set(draft.choices.map(choice => choice.part_id)).size === draft.choices.length;
  const resolved = update.plan.conflicts.every(conflict => draft.choices.some(choice => choice.part_id === conflict.part_id))
    && update.plan.changes.every(change => change.action !== "conflict" || conflicts.has(change.part_id));
  const groups = deletionGroups(update.plan.changes, draft.choices);
  const consented = groups.every(group => group.every(id => draft.removal_consents.includes(id)));
  const saved = sameChoices(draft.choices, update.drafts ?? []);
  const held = update.resolved_check
    ? update.resolved_check.state !== "passed"
    : update.plan.flags.some(flag => ["safety_hold", "required_check_unavailable"].includes(flag));
  return { stale, validChoices, resolved, groups, consented, saved, held,
    canSave: update.status === "open" && !stale && validChoices && !saved,
    canApply: update.status === "open" && !stale && validChoices && resolved && consented && saved && !held };
}
function Evidence({ title, evidence }) {
  const value = evidence.kind === "known" ? (typeof evidence.value === "string" ? evidence.value : JSON.stringify(evidence.value, null, 2))
    : evidence.kind === "absent" ? "Known absence" : "Unknown — no proven evidence";
  const preview = boundedSourceText(value);
  return h("section", { className: "oms-source-side" }, h("h5", null, title),
    h("pre", { tabIndex: 0, "aria-label": title }, preview.text), preview.truncated ? h("p", null, "Bounded excerpt") : null);
}

export function SourceUpdateCard({ update, title, draft: controlledDraft, partFingerprints = {}, ownership = {}, ref,
  onDraftChange, onSaveDraft, onApply, onSkip, onAdopt, onRecheck, renderFile,
  busy = false, loading = false, error, unavailable, freshness = "fresh" }) {
  if (loading) return h(Notice, null, "Loading source comparison…");
  if (unavailable) return h(Notice, null, unavailable);
  if (!update) return error ? h(Notice, { kind: "error" }, error) : h(EmptyState, { title: "No source update" }, "There is no open update for this skill.");
  const closed = update.status !== "open";
  const draft = controlledDraft ?? initialSourceDraft(update);
  const state = sourceCardState(update, draft, partFingerprints);
  const disabled = busy || !!error || freshness !== "fresh" || state.stale || update.status !== "open";
  const recheckDisabled = busy || !!error || update.status !== "open";
  const canEdit = !disabled && !!onDraftChange;
  const body = { skill_id: update.plan.skill.skill_id, fingerprint: update.plan.fingerprint };
  const edit = next => { if (canEdit) onDraftChange(next); };
  const decide = (part_id, choice, merged_text = null) => {
    const change = update.plan.changes.find(row => row.part_id === part_id);
    const conflict = update.plan.conflicts.find(row => row.part_id === part_id);
    if (!change || !partFingerprints[part_id] || (choice && !allowedChoices(change, conflict).includes(choice))) return;
    const choices = draft.choices.filter(row => row.part_id !== part_id);
    if (choice) choices.push({ part_id, part_fingerprint: partFingerprints[part_id], choice, merged_text: choice === "merged_text" ? merged_text ?? "" : null });
    edit({ ...draft, choices, removal_consents: draft.removal_consents.filter(id => id !== part_id) });
  };
  const action = (callback, enabled, payload) => () => { if (!disabled && enabled && callback) callback(payload); };
  return h(Panel, { title: title || update.plan.skill.skill_id, className: "oms-source-card", "aria-busy": busy },
    h(SourceStatus, { status: closed ? update.status : "awaiting_review", flags: closed ? [] : update.plan.flags, ref }),
    closed && update.plan.flags.length ? h("p", null, "Flags recorded at review: ", update.plan.flags.map(flag => Object.hasOwn(sourceFlagLabels, flag) ? sourceFlagLabels[flag] : flag.replaceAll("_", " ")).join("; ")) : null,
    h("p", null, closed ? "Recorded historical comparison. The OMS side shows content at review time. This update no longer accepts decisions."
      : "Review the whole skill. Local name and domain stay unchanged. Saving choices does not change live content; publishing is separate."),
    h("dl", { className: "oms-source-metadata" },
      h("div", null, h("dt", null, "Origin"), h("dd", null, update.plan.origin.kind)),
      h("div", null, h("dt", null, "Base snapshot"), h("dd", null, update.plan.base?.snapshot_id || "Unknown — first reconciliation")),
      h("div", null, h("dt", null, "Incoming snapshot"), h("dd", null, update.plan.incoming.snapshot_id)),
      h("div", null, h("dt", null, "Review revision"), h("dd", null, `${update.generation} · content ${update.plan.fingerprint.content_generation} · binding ${update.plan.fingerprint.binding_generation}`))),
    error ? h(Notice, { kind: "error" }, error) : null,
    closed ? null : state.stale || freshness === "stale" ? h(Notice, { kind: "error" }, "This draft is out of date. Refresh the comparison before deciding.")
      : freshness === "unavailable" ? h(Notice, null, "Freshness could not be verified. Decisions are unavailable.")
        : h("p", { role: "status" }, state.saved ? "Choices match the saved review." : "Unsaved choices — save before Apply."),
    !closed && update.resolved_check && state.saved ? h("p", { role: "status" }, update.resolved_check.state === "passed"
      ? "Saved choices passed the required checks. Original source warnings remain visible."
      : update.resolved_check.state === "held" ? "Required checks still hold this selection." : "Required checks for this selection are unavailable.") : null,
    !closed && !state.validChoices ? h(Notice, { kind: "error" }, "A choice is incomplete, no longer permitted or belongs to a different part revision.") : null,
    update.plan.changes.map(change => {
      const conflict = update.plan.conflicts.find(row => row.part_id === change.part_id);
      const choice = draft.choices.find(row => row.part_id === change.part_id);
      const allowed = allowedChoices(change, conflict);
      const identified = !!partFingerprints[change.part_id];
      const removable = removes(change) || change.linked_removals?.includes(change.part_id);
      const editable = canEdit && identified;
      return h("section", { key: change.part_id, className: "oms-source-part", "aria-label": `Change ${change.part_id}` },
        h("h3", null, change.part_id), h("p", null, `${change.kind} · ${change.action}`),
        h("p", null, Object.hasOwn(OWNERSHIP, ownership[change.part_id]) ? OWNERSHIP[ownership[change.part_id]] : OWNERSHIP.unknown),
        conflict ? h("p", null, conflict.reason) : null,
        h("div", { className: "oms-source-columns" }, h(Evidence, { title: "Base (B)", evidence: change.base }),
          h(Evidence, { title: "Current OMS (L)", evidence: change.local }), h(Evidence, { title: "Incoming (U)", evidence: change.incoming })),
        change.kind === "file" && renderFile ? renderFile(change) : null,
        !closed && conflict ? h("label", { className: "oms-source-field" }, "Decision", h("select", { value: choice?.choice ?? "", disabled: !editable, "aria-label": `Decision for ${change.part_id}`,
          onChange: event => decide(change.part_id, event.target.value) }, h("option", { value: "" }, "Choose a decision"), allowed.map(value => h("option", { key: value, value }, CHOICES[value])))) : null,
        !closed && !identified && (conflict || removable) ? h("p", null, "Part revision unavailable. Refresh before making choices.") : null,
        closed && choice ? h("p", null, `Recorded decision: ${CHOICES[choice.choice]}`) : null,
        closed && choice?.choice === "merged_text" ? h("section", { className: "oms-source-side" }, h("h5", null, "Recorded merged text"), h("pre", { tabIndex: 0, "aria-label": `Recorded merged text for ${change.part_id}` }, boundedSourceText(choice.merged_text).text)) : null,
        !closed && choice?.choice === "merged_text" && allowed.includes("merged_text") ? h("label", { className: "oms-source-field" }, "Human-written replacement", h("textarea", {
          value: choice.merged_text ?? "", rows: 6, disabled: !editable, "aria-label": `Merged text for ${change.part_id}`,
          onChange: event => decide(change.part_id, "merged_text", event.target.value),
        })) : null,
        !closed && removable && allowed.includes("keep_oms") ? h("label", { className: "oms-source-check" }, h("input", { type: "checkbox", checked: choice?.choice === "keep_oms", disabled: !editable,
          "aria-label": `Keep ${change.part_id}`, onChange: event => decide(change.part_id, event.target.checked ? "keep_oms" : "") }), "Keep this current OMS part") : null);
    }),
    !closed && state.groups.length ? h("fieldset", { className: "oms-source-removals", disabled: !canEdit }, h("legend", null, "Explicit removal consent"),
      h("p", null, "Linked removals are reviewed together. Keep an individual part above to exclude it from removal."),
      state.groups.map((group, index) => h("label", { key: group.join("|"), className: "oms-source-check" }, h("input", { type: "checkbox", checked: group.every(id => draft.removal_consents.includes(id)),
        "aria-label": `Consent to remove group ${index + 1}`, onChange: event => edit({ ...draft, removal_consents: event.target.checked
          ? [...new Set([...draft.removal_consents, ...group])] : draft.removal_consents.filter(id => !group.includes(id)) }) }), `Remove group ${index + 1}: ${group.join(", ")}`))) : null,
    !closed && onAdopt ? h("p", null, "Keep-current-and-adopt advances only this source baseline. Current content and existing safety holds are preserved.") : null,
    !closed ? h("div", { className: "oms-source-actions" },
      onSaveDraft ? h(Button, { variant: "secondary", disabled: disabled || !state.canSave, onClick: action(onSaveDraft, state.canSave, { ...body, choices: draft.choices }) }, "Save choices") : null,
      onApply ? h(Button, { disabled: disabled || !state.canApply, onClick: action(onApply, state.canApply, { ...body, removal_consents: draft.removal_consents }) }, "Apply whole skill") : null,
      onRecheck ? h(Button, { variant: "secondary", disabled: recheckDisabled, onClick: () => { if (!recheckDisabled) onRecheck(body); } }, "Recheck safety") : null,
      onSkip ? h(Button, { variant: "secondary", disabled, onClick: action(onSkip, true, body) }, "Skip this version") : null,
      onAdopt ? h(Button, { variant: "secondary", disabled, onClick: action(onAdopt, true, body) }, "Keep all current OMS content and adopt this revision") : null) : null);
}
