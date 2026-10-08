import { createElement as h } from "react";
import { Badge, Button, Notice } from "./primitives.js";

const STATES = {
  unchecked: "Not checked", up_to_date: "Up to date", updates_available: "Updates available",
  fetching: "Fetching source", planning: "Comparing changes", applying: "Applying whole skill",
  awaiting_review: "Awaiting review", complete: "Operation complete", blocked: "Operation blocked",
  failed: "Failed", missing: "Source missing", redirect: "Source redirect needs review",
  loading: "Loading source", unavailable: "Source unavailable",
};
export const sourceFlagLabels = Object.freeze({
  conflict: "Decisions required", unknown_identity: "Unproven content identity", deletion_consent: "Removal consent required",
  script_changes: "Scripts or executable files", declared_licence_changed: "Declared licence changed",
  unproven_history: "History could not be proven", rewritten_history: "Source history was rewritten",
  first_reconciliation: "Awaiting first reconciliation", safety_hold: "Safety review required",
  required_check_unavailable: "Required check unavailable",
});
const label = (values, key) => Object.hasOwn(values, key) ? values[key] : String(key).replaceAll("_", " ");
const OUTCOMES = { applied: "Applied", awaiting_review: "Awaiting review", blocked: "Blocked", failed: "Failed", unchanged: "Unchanged" };

export function SourceStatus({ status = "unchecked", message, flags = [], retryNeeded = false,
  firstReconciliation = false, ref, operation, exclusions = [] }) {
  const state = typeof status === "string" ? status : status?.state ?? "unchecked";
  const code = typeof status === "string" ? null : status?.code;
  return h("div", { className: "oms-source-status", role: "status", "aria-live": "polite", "aria-atomic": true },
    h("div", { className: "oms-source-badges" }, h(Badge, null, label(STATES, state)),
      retryNeeded ? h(Badge, null, "Retry needed") : null,
      firstReconciliation && !flags.includes("first_reconciliation") ? h(Badge, null, "Awaiting first reconciliation") : null,
      flags.map(flag => h(Badge, { key: flag }, label(sourceFlagLabels, flag)))),
    code ? h("p", { className: "oms-source-code" }, "Reason: ", code.replaceAll("_", " ")) : null,
    message ? h("p", null, message) : null,
    retryNeeded ? h("p", null, "The previous attempt was refused. Submit a new explicit attempt after the blocker is resolved; nothing is queued.") : null,
    ref ? h("p", { className: "oms-source-ref" }, ref.kind === "branch" ? "Branch: " : "Pinned ", `${ref.kind === "branch" ? "" : `${ref.kind}: `}${ref.name}`, ref.commit ? h("code", null, ref.commit) : null) : null,
    operation ? h("div", { className: "oms-source-operation" },
      h("p", null, label(STATES, operation.state), operation.committed ? ". Committed results are retained." : ". No committed content is reported."),
      h("ul", null, operation.outcomes?.map((row, index) => h("li", { key: `${row.skill_id}-${index}` }, `${row.skill_id}: ${label(OUTCOMES, row.state)}`, row.code ? ` — ${row.code.replaceAll("_", " ")}` : "")))) : null,
    exclusions.length ? h("div", null, h("h4", null, "Excluded from bulk Apply"), h("ul", null,
      exclusions.map(row => h("li", { key: row.skill_id }, `${row.skill_id}: ${row.reasons.join("; ")}`)))) : null);
}

export function sourceBulkExclusions(update) {
  const reasons = [];
  if (update.status !== "open") reasons.push("Update is no longer open");
  if (update.plan.conflicts.length) reasons.push("Decisions required");
  for (const flag of update.plan.flags) reasons.push(label(sourceFlagLabels, flag));
  if (update.plan.changes.some(change => change.action === "remove" || change.linked_removals?.length)) reasons.push("Removal consent required");
  if (update.drafts?.length) reasons.push("Manual choices are saved");
  return [...new Set(reasons)];
}

export function SourceUndo({ guard, eligible = false, reason, confirmed = false, onConfirmedChange, onUndo, busy = false }) {
  const guarded = !!guard?.undo_id && !!guard.policy_version && guard.expected_generations?.length > 0;
  const disabled = busy || !eligible || !guarded || !confirmed || !onUndo;
  return h("section", { className: "oms-source-undo", "aria-label": "Undo source update", "aria-busy": busy },
    h("h3", null, "Undo source update"),
    h("p", null, "Undo is available only while all affected content and bindings still match. It restores this source update and its baseline; other changes are retained. Publishing is separate."),
    reason ? h(Notice, null, reason) : null,
    h("label", { className: "oms-source-check" }, h("input", { type: "checkbox", checked: confirmed,
      disabled: busy || !eligible || !guarded || !onConfirmedChange,
      onChange: event => { if (!busy && eligible && guarded) onConfirmedChange?.(event.target.checked); } }), "Confirm undo of this source update"),
    h(Button, { variant: "secondary", disabled, onClick: () => { if (!disabled) onUndo(guard); } }, "Undo source update"));
}
