"use client";
import { createElement as h, useEffect, useId, useRef, useState } from "react";
import { Button, EmptyState, Notice } from "./primitives.js";
import { SkillTextEditor } from "./document.js";
import { DiffLines, lineDiff } from "./diff.js";

/** Human-authored edits; the application supplies a public document reader. */
export function ManualAmendment({ skills, suggestions = [], initialSkillId, draft, onChange, loadDocument, onDecision, busy, correction }) {
  const skillId = draft?.skill_id ?? initialSkillId ?? "";
  const [live, setLive] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [skillQuery, setSkillQuery] = useState("");
  const [limit, setLimit] = useState(12);
  const [preview, setPreview] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const previewRef = useRef(null);
  const blockerId = useId();
  useEffect(() => {
    let cancelled = false;
    setLive(null); setError(""); setLoading(Boolean(skillId));
    if (skillId) Promise.resolve().then(() => loadDocument(skillId)).then(document => {
      if (!cancelled) setLive(document);
    }).catch(failure => { if (!cancelled) setError(failure instanceof Error ? failure.message : "The skill could not be loaded."); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [skillId, loadDocument, reload]);
  useEffect(() => { if (preview) previewRef.current?.scrollIntoView?.({ block: "nearest", behavior: "smooth" }); }, [preview]);

  const values = draft?.values ?? {};
  const dirty = Object.keys(values).length > 0;
  const document = dirty ? draft.document : live?.skill_id === skillId ? live : null;
  const stale = Boolean(dirty && live && (draft.document.revision !== live.revision ||
    draft.document.parts.some(part => part.anchor in values && part.edit_text !== live.parts.find(current => current.anchor === part.anchor)?.edit_text) ||
    JSON.stringify(draft.document.parts.map(part => part.affected_skills)) !== JSON.stringify(live.parts.map(part => part.affected_skills))));
  const parts = document?.parts ?? [];
  const affected = new Map([[skillId, skills.find(skill => skill.id === skillId)?.name ?? skillId]]);
  for (const part of parts.filter(part => part.anchor in values)) {
    for (const skill of part.affected_skills ?? []) affected.set(skill.id, skill.name);
  }
  const shared = affected.size > 1;
  const changedParts = parts.filter(part => part.anchor in values);
  const isRemoval = part => ["rule", "overflow-rule"].includes(part.kind) && !values[part.anchor].trim();
  const removals = changedParts.filter(isRemoval);
  const emptyPassages = changedParts.filter(part => !values[part.anchor].trim() && !isRemoval(part));
  const blockers = [];
  if (loading || !live && !error) blockers.push("Checking the latest skill before applying your changes…");
  if (error) blockers.push(`The latest skill could not be checked: ${error} Retry loading the skill above.`);
  if (stale) blockers.push("The skill changed while you were editing. Keep a copy of your edits, then discard the draft and reload the latest version.");
  if (emptyPassages.length) blockers.push("A passage is empty. Enter replacement text or undo that passage's edit before applying. Entire rules can be removed by clearing their text.");
  if (shared && !confirmed) blockers.push("Confirm the change to every listed skill using the checkbox above.");
  if (busy) blockers.push("Saving your changes…");
  const ready = dirty && !blockers.length;
  const skill = skills.find(skill => skill.id === skillId);
  const suggestionById = new Map(suggestions.map(suggestion => [suggestion.id, suggestion]));
  const choices = [...skills].filter(skill => `${skill.name} ${skill.id}`.toLocaleLowerCase().includes(skillQuery.trim().toLocaleLowerCase()))
    .sort((left, right) => (suggestionById.get(right.id)?.score ?? -1) - (suggestionById.get(left.id)?.score ?? -1) || left.name.localeCompare(right.name));
  let heading = "Introduction";
  const locations = new Map();
  for (const part of parts) {
    if (["heading", "group", "title"].includes(part.kind)) heading = part.edit_text || part.lines.join(" ").replace(/^#+\s*/, "").trim();
    locations.set(part.anchor, heading);
  }

  function update(anchor, text) {
    const next = { ...values };
    if (text === parts.find(part => part.anchor === anchor)?.edit_text) delete next[anchor];
    else next[anchor] = text;
    onChange({ skill_id: skillId, document, values: next, focus_anchor: draft?.focus_anchor });
    setPreview(false); setConfirmed(false);
  }
  function discard() {
    onChange({ skill_id: skillId, values: {} });
    setPreview(false); setConfirmed(false); setReload(value => value + 1);
  }

  return h("div", { className: "oms-amendment" },
    !skillId ? h("div", { className: "oms-skill-picker" },
      h("h3", null, "Choose the skill to edit"),
      h("p", { className: "oms-manual-help" }, "Open a skill, then edit any rule or passage directly. Suggested skills appear first."),
      h("label", { className: "oms-manual-field" }, "Find a skill to edit",
        h("input", { type: "search", value: skillQuery, placeholder: "Search skill names", onChange: event => { setSkillQuery(event.target.value); setLimit(12); } })),
      h("div", { className: "oms-skill-picker__results" }, ...choices.slice(0, limit).map(choice => h(Button, {
        key: choice.id, variant: "secondary", disabled: busy, "aria-label": `Open skill: ${choice.name}`,
        onClick: () => { onChange({ skill_id: choice.id, values: {} }); setPreview(false); setConfirmed(false); }
      }, h("strong", null, choice.name), suggestionById.has(choice.id) ? h("span", null, suggestionById.get(choice.id).reason) : null))),
      choices.length > limit ? h(Button, { variant: "secondary", onClick: () => setLimit(value => value + 12) }, "Show more skills") : null,
      !choices.length ? h(EmptyState, { title: "No matching skills" }, "Try a different name.") : null) :
      h("div", { className: "oms-amendment__heading" }, h("h3", null, skill?.name ?? skillId),
        h(Button, { variant: "secondary", disabled: busy || dirty, onClick: () => onChange({ skill_id: "", values: {} }) }, "Change skill")),
    loading ? h("p", { role: "status" }, "Loading skill…") : null,
    error ? h(Notice, { kind: "error" }, error, h(Button, { variant: "secondary", onClick: () => setReload(value => value + 1) }, "Retry loading skill")) : null,
    stale ? h(Notice, { kind: "error" }, "This document or its affected skills changed. Your draft is kept below. Copy any text you need, then discard the draft and reload before applying.") : null,
    document ? h("div", { className: "oms-amendment__workspace" },
      h("div", { className: "oms-amendment__main" },
        h(SkillTextEditor, { key: skillId, parts, values, disabled: busy, onChange: update,
          guidanceOnly: true, allowRuleRemoval: true, focusAnchor: draft?.focus_anchor, replacementText: correction }),
        h("div", { className: "oms-amendment__save" },
          h("span", { role: "status" }, `${Object.keys(values).length} unsaved ${Object.keys(values).length === 1 ? "edit" : "edits"}`),
          h(Button, { disabled: !dirty || busy, onClick: () => { setPreview(true); setConfirmed(false); } }, "Preview changes"),
          dirty ? h(Button, { variant: "secondary", disabled: busy, onClick: discard }, "Discard draft and reload") : null,
          h(Button, { variant: "secondary", disabled: busy || loading, onClick: () => setReload(value => value + 1) }, "Check latest version")),
        preview && dirty ? h("div", { ref: previewRef, className: "oms-amendment-preview", role: "region", "aria-label": "Correction change preview" },
          h("h3", null, "Review all changes"),
          h("p", { className: "oms-manual-help" }, "− Current wording · + Your replacement"),
          ...changedParts.map(part => h("section", { key: part.anchor },
            h("h4", null, locations.get(part.anchor), isRemoval(part) ? " · Remove rule" : ""),
            h(DiffLines, { lines: lineDiff(part.edit_text ?? "", isRemoval(part) ? "" : values[part.anchor]) }))),
          removals.length ? h("p", { className: "oms-manual-help" }, `${removals.length} ${removals.length === 1 ? "rule will" : "rules will"} be removed from the affected skills. The original wording and correction evidence stay in history, and the rules can be restored.`) : null,
          h("p", null, "Affected skills: ", [...affected.values()].join(", ")),
          shared ? h("label", { className: "oms-manual-match" },
            h("input", { type: "checkbox", checked: confirmed, disabled: busy, onChange: event => setConfirmed(event.target.checked) }),
            "I confirm this shared guidance should change in every listed skill.") : null,
          h("p", { className: "oms-manual-help" }, "This saves every edit together and resolves the correction. Publish when you are ready to update your agents."),
          blockers.length ? h("div", { id: blockerId, role: "status", className: "oms-manual-help" }, ...blockers.map(message => h("p", { key: message }, message))) : null,
          h(Button, { disabled: !ready, "aria-describedby": blockers.length ? blockerId : undefined, onClick: () => void onDecision({ action: "amend", body: "", skill_ids: [skillId],
            amendment: { skill_id: skillId, revision: document.revision,
              parts: Object.entries(values).map(([anchor, text]) => ({ anchor, text, expected_text: parts.find(part => part.anchor === anchor).edit_text })),
              affected_skill_ids: [...affected.keys()].sort() } }) }, "Apply change and resolve correction")) : null)) : null);
}
