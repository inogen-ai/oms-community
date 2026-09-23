"use client";
import { createElement as h, useId, useState } from "react";
import { Badge, Button, Notice, Panel } from "./primitives.js";
import { DiffLines, lineDiff } from "./diff.js";
import { ManualAmendment } from "./manual-amendment.js";
import { ManualPlacement } from "./manual-placement.js";
import { RuleMatchContext, useMatchDocuments } from "./manual-match-context.js";

function CorrectionContext({ item }) {
  const date = item.posted_at && new Date(item.posted_at);
  return h("section", { className: "oms-correction-context", "aria-label": "Posted correction and context" },
    h("div", { className: "oms-correction-context__quote" },
      h("h2", null, "Correction"), h("blockquote", null, item.text)),
    h("dl", { className: "oms-correction-facts" },
      h("div", null, h("dt", null, "Skill hint"), h("dd", null, item.skill_hint || "Not provided")),
      h("div", null, h("dt", null, "Posted"), h("dd", null, date && !Number.isNaN(date.getTime()) ? h("time", { dateTime: item.posted_at }, date.toLocaleString()) : "Not recorded")),
      h("div", null, h("dt", null, "Repository"), h("dd", null, item.repo || "Not provided"))),
    h("details", { className: "oms-correction-evidence" }, h("summary", null, "Source and interaction context"),
      h("dl", { className: "oms-correction-facts" },
        h("div", null, h("dt", null, "Source"), h("dd", null, item.source_ref || "Not provided")),
        h("div", null, h("dt", null, "Agent / channel"), h("dd", null, [item.source_agent_id, item.source_runtime].filter(Boolean).join(" · ") || "Not provided")),
        h("div", null, h("dt", null, "Signal"), h("dd", null, item.signal_type.replaceAll("_", " ")))),
      item.context?.user_input || item.context?.agent_output ? h("div", null,
        item.context_truncated ? h("p", { className: "oms-manual-help" }, "Showing an excerpt of the captured interaction.") : null,
        item.context.user_input ? h("div", null, h("h3", null, "Original request"), h("pre", null, item.context.user_input)) : null,
        item.context.agent_output ? h("div", null, h("h3", null, "Agent response"), h("pre", null, item.context.agent_output)) : null)
        : h("p", { className: "oms-manual-help" }, "No interaction context is available for this correction."),
      h("p", { className: "oms-manual-help" }, "Correction ID: ", h("code", null, item.txn_id))));
}

function TargetSkills({ item, skills, selected, suggestions, busy, onChange, createSkillHref }) {
  const matchedHint = Boolean(item.skill_hint && item.candidate_skill_ids.length === 1 && selected.includes(item.candidate_skill_ids[0]));
  const [open, setOpen] = useState(!matchedHint);
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(8);
  const ranks = new Map(suggestions.map(row => [row.id, row]));
  const names = skills.filter(skill => selected.includes(skill.id)).map(skill => skill.name);
  const choices = skills.filter(skill => `${skill.name} ${skill.id}`.toLowerCase().includes(query.trim().toLowerCase())).sort((a, b) =>
    Number(selected.includes(b.id)) - Number(selected.includes(a.id)) || (ranks.get(b.id)?.score ?? -1) - (ranks.get(a.id)?.score ?? -1) || a.name.localeCompare(b.name));
  return h("section", { className: "oms-target-skills", "aria-label": "Target skills" },
    h("div", { className: "oms-target-skills__summary" },
      h("div", null, h("h3", null, "Target skills"), h("p", null, names.length ? names.join(", ") : "Choose where this guidance belongs"),
        matchedHint ? h("span", { className: "oms-manual-help" }, "Matched from the submitted skill hint") : null),
      h(Button, { variant: "secondary", disabled: busy, "aria-expanded": open, onClick: () => setOpen(!open) }, open ? "Done choosing skills" : "Change target skills")),
    open ? h("div", { className: "oms-target-skills__choices" },
      !skills.length ? h("p", null, h("a", { href: createSkillHref }, "Create a skill"), " before applying this correction.") : null,
      h("label", { className: "oms-manual-field" }, "Find target skills", h("input", { type: "search", value: query, disabled: busy,
        onChange: event => { setQuery(event.target.value); setLimit(8); }, placeholder: "Search skill names" })),
      ...choices.slice(0, limit).map(skill => h("label", { key: skill.id, className: "oms-manual-match" },
        h("input", { type: "checkbox", disabled: busy, checked: selected.includes(skill.id), "aria-label": skill.name,
          onChange: event => onChange(event.target.checked ? [...selected, skill.id] : selected.filter(id => id !== skill.id)) }),
        h("span", null, h("strong", null, skill.name), ranks.has(skill.id) ? h("span", { className: "oms-manual-help" }, ranks.get(skill.id).reason) : null))),
      !choices.length && skills.length ? h("p", null, "No matching skills.") : null,
      choices.length > limit ? h(Button, { variant: "secondary", onClick: () => setLimit(value => value + 8) }, "Show more skills") : null) : null);
}

/** Manual learning is shared behaviour; applications supply data and transport. */
export function ManualReviewCard({ item, skills, onDecision, createSkillHref = "/skills", draft, onDraftChange, loadDocument }) {
  const [localDraft, setLocalDraft] = useState(() => ({ body: item.text, skill_ids: item.candidate_skill_ids.filter(id => skills.some(skill => skill.id === id)), rule_id: "" }));
  const currentDraft = draft ?? localDraft;
  const body = currentDraft.body;
  const skillIds = currentDraft.skill_ids;
  const ruleId = currentDraft.rule_id || "";
  const amending = Boolean(loadDocument && currentDraft.mode !== "rule");
  const tabId = useId();
  function updateDraft(change) {
    const next = { ...currentDraft,
      ...(("body" in change || "rule_id" in change || "skill_ids" in change) ? { confirm_reinforcement: false, expected_rule_body: undefined, confirm_shared_replacement: false } : {}),
      ...(("rule_id" in change || "skill_ids" in change) ? { separate_rule: false, replacement: undefined } : {}), ...change };
    setLocalDraft(next); onDraftChange?.(next);
  }
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [placementReady, setPlacementReady] = useState({});
  const held = item.state === "held_safety" || Boolean(item.held_reason);
  const supportsSimilarity = Array.isArray(item.similar_matches);
  const matches = (supportsSimilarity ? item.similar_matches : (item.exact_matches ?? [])).filter(match =>
    !skillIds.length || !Array.isArray(match.skill_ids) || match.skill_ids.some(id => skillIds.includes(id)));
  const matchDocuments = useMatchDocuments({ active: !held && !amending, matches, selected: skillIds, loadDocument });
  const suggestions = (item.skill_suggestions ?? []).filter(suggestion => skills.some(skill => skill.id === suggestion.id));
  const selectedMatch = matches.find(match => (typeof match === "string" ? match : match.id) === ruleId);
  const normalise = value => value.normalize("NFC").trim().replace(/\s+/g, " ");
  const needsConfirmation = supportsSimilarity && Boolean(selectedMatch) && (!selectedMatch.exact || normalise(item.text) !== normalise(body));
  const confirmed = currentDraft.confirm_reinforcement && currentDraft.expected_rule_body === selectedMatch?.body;
  const ready = Boolean(body.trim() && skillIds.length && !busy);
  const placements = (currentDraft.placements ?? []).filter(placement => skillIds.includes(placement.skill_id));
  const canCreate = ready && !ruleId && placements.every(placement => placementReady[placement.skill_id]);
  const canReinforce = ready && Boolean(selectedMatch) && (!needsConfirmation || confirmed);
  const replacement = currentDraft.replacement;
  const replacementPart = replacement?.document.parts.find(part => part.anchor === replacement.anchor);
  const affected = replacementPart ? [...new Set([replacement.skill_id, ...(replacementPart.affected_skills ?? []).map(skill => skill.id)])].sort() : [];
  const canReplace = replacementPart && !busy && body.trim() && body.trim() !== replacementPart.edit_text.trim() && (affected.length === 1 || currentDraft.confirm_shared_replacement);

  async function submit(decision) {
    setBusy(true); setError("");
    try { await onDecision(decision); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "The decision could not be recorded."); }
    finally { setBusy(false); }
  }
  async function decide(action) {
    if (action === "create" && !canCreate || action === "reinforce" && !canReinforce) return;
    await submit({ action, body: body.trim(), skill_ids: skillIds,
      ...(action === "create" && placements.length ? { placements } : {}),
      ...(action === "reinforce" ? { rule_id: ruleId } : {}),
      ...(action === "reinforce" && needsConfirmation ? { confirm_reinforcement: true, expected_rule_body: selectedMatch.body } : {}) });
  }
  async function reviewReplacement(match) {
    if (!loadDocument || busy) return;
    const id = typeof match === "string" ? match : match.id;
    const anchor = `rule:${id}`;
    setBusy(true); setError("");
    try {
      const candidates = (skillIds.length ? skillIds : match.skill_ids ?? []).filter(id =>
        skills.some(skill => skill.id === id) && (!Array.isArray(match.skill_ids) || match.skill_ids.includes(id)));
      for (const skillId of candidates) {
        const document = await loadDocument(skillId);
        const part = document.parts.find(part => part.anchor === anchor && part.editable);
        if (!part) continue;
        if (typeof match !== "string" && part.edit_text !== match.body) throw new Error("The matched rule changed. Refresh the inbox and compare its latest wording before replacing it.");
        updateDraft({ rule_id: id, skill_ids: skillIds.length ? skillIds : [skillId], replacement: { skill_id: skillId, document, anchor } });
        return;
      }
      throw new Error("Choose a target skill containing this rule before replacing it.");
    } catch (failure) { setError(failure instanceof Error ? failure.message : "The matched rule could not be loaded."); }
    finally { setBusy(false); }
  }
  function editRelated() {
    if (Object.keys(currentDraft.amendment?.values ?? {}).length) { setError("You have an existing text-editing draft. Open Edit skill text to apply or discard it first."); return; }
    updateDraft({ mode: "amend", amendment: { skill_id: replacement.skill_id, document: replacement.document,
      focus_anchor: replacement.anchor, values: { [replacement.anchor]: body } } });
  }
  const mode = amending ? "edit" : "rule";
  return h(Panel, { className: "oms-manual-card", "aria-label": `Correction ${item.txn_id}` },
    h(CorrectionContext, { item }),
    item.held_reason || item.warning || item.warnings?.length ? h(Notice, null, item.held_reason || item.warning,
      item.warnings?.map(warning => h("p", { key: warning }, warning))) : null,
    error ? h(Notice, { kind: "error" }, error) : null,
    held ? h("div", null, h("p", null, "Review the safety hold before applying this correction."),
      h("div", { className: "oms-manual-actions" },
        h(Button, { disabled: busy, onClick: () => void decide("release_safety") }, "Release for manual review"),
        h(Button, { variant: "danger", disabled: busy, onClick: () => void decide("reject") }, "Reject correction"))) : h("div", null,
      loadDocument ? h("div", { className: "oms-review-modes" }, h("h3", null, "How to apply this correction"),
        h("div", { role: "tablist", "aria-label": "How to apply this correction", className: "oms-mode-tabs" },
          ...[["edit", "Edit skill text"], ["rule", "Add or confirm a rule"]].map(([key, label], index) => h("button", {
            key, type: "button", role: "tab", id: `${tabId}-${key}`, "aria-controls": `${tabId}-panel`, "aria-selected": mode === key,
            tabIndex: mode === key ? 0 : -1, disabled: busy,
            onClick: () => updateDraft({ mode: key === "edit" ? "amend" : "rule" }),
            onKeyDown: event => {
              if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
              event.preventDefault(); const next = event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index;
              updateDraft({ mode: next ? "rule" : "amend" });
              event.currentTarget.parentElement.children[next].focus();
            }
          }, label))),
        h("p", { className: "oms-manual-help" }, amending ? "Change rules and related passages, then review all your edits together." : "Add new guidance, replace a conflicting rule, or confirm that an existing rule is correct.")) : null,
      h("div", { id: `${tabId}-panel`, ...(loadDocument ? { role: "tabpanel", "aria-labelledby": `${tabId}-${mode}` } : {}) },
        amending ? h(ManualAmendment, { skills, suggestions, initialSkillId: skillIds[0], draft: currentDraft.amendment,
          onChange: amendment => updateDraft({ amendment }), loadDocument, busy, onDecision: submit, correction: body }) : h("div", null,
          h(TargetSkills, { item, skills, selected: skillIds, suggestions, busy, createSkillHref, onChange: ids => updateDraft({ skill_ids: ids }) }),
          h("label", { className: "oms-manual-field" }, "Proposed rule wording",
            h("textarea", { rows: 3, value: body, disabled: busy, onChange: event => updateDraft({ body: event.target.value }) })),
          matches.length ? h("section", { className: "oms-rule-comparisons", "aria-label": "Compare existing rules" },
            h("h3", null, "Compare existing rules"),
            h("p", { className: "oms-manual-help" }, "Replace a rule when the correction changes its meaning. Reinforce it only when its wording should stay."),
            ...matches.map(match => {
              const id = typeof match === "string" ? match : match.id;
              return h("article", { key: id, className: "oms-manual-candidate", "aria-label": `Existing rule: ${typeof match === "string" ? id : match.body}` },
                h("div", { className: "oms-rule-comparison__heading" }, h(Badge, null, match.exact ? "Exact match" : "Possible match"),
                  h("span", { className: "oms-manual-help" }, (match.skill_ids ?? []).map(id => skills.find(skill => skill.id === id)?.name || id).join(", "))),
                loadDocument ? h(RuleMatchContext, { match, skills, selected: skillIds, documents: matchDocuments })
                  : h("blockquote", null, typeof match === "string" ? id : match.body),
                typeof match !== "string" ? h("p", { className: "oms-manual-help" }, match.reason) : null,
                h("div", { className: "oms-manual-actions" },
                  loadDocument ? h(Button, { disabled: busy || !body.trim(), onClick: () => void reviewReplacement(match) }, "Replace this rule") : null,
                  h("label", { className: "oms-manual-match" }, h("input", { type: "radio", name: `match-${item.txn_id}`, value: id,
                    checked: ruleId === id && !replacement, disabled: busy, "aria-label": `Select rule: ${typeof match === "string" ? id : match.body}`,
                    onChange: () => updateDraft({ rule_id: id, ...(!supportsSimilarity && typeof match !== "string" ? { body: match.body } : {}) }) }), "Keep this rule and reinforce it")),
                replacement && ruleId === id && replacementPart ? h("div", { className: "oms-replacement-preview", role: "region", "aria-label": "Rule replacement preview" },
                  h("h4", null, "Review replacement"), h(DiffLines, { lines: lineDiff(replacementPart.edit_text, body) }),
                  h("p", null, "This replaces the rule in its current position."),
                  h("p", null, "Affected skills: ", affected.map(id => skills.find(skill => skill.id === id)?.name || id).join(", ")),
                  affected.length > 1 ? h("label", { className: "oms-manual-match" }, h("input", { type: "checkbox", checked: Boolean(currentDraft.confirm_shared_replacement), disabled: busy,
                    onChange: event => updateDraft({ confirm_shared_replacement: event.target.checked }) }), "Replace this shared rule in every listed skill") : null,
                  h("div", { className: "oms-manual-actions" },
                    h(Button, { disabled: !canReplace, onClick: () => void submit({ action: "amend", body: "", skill_ids: [replacement.skill_id], amendment: {
                      skill_id: replacement.skill_id, revision: replacement.document.revision,
                      parts: [{ anchor: replacement.anchor, text: body, expected_text: replacementPart.edit_text }], affected_skill_ids: affected } }) }, "Replace rule and resolve correction"),
                    h(Button, { variant: "secondary", disabled: busy, onClick: editRelated }, "Edit this and related passages"))) : null);
            }),
            needsConfirmation && !replacement ? h("label", { className: "oms-manual-match oms-manual-confirmation" },
              h("input", { type: "checkbox", disabled: busy, checked: Boolean(confirmed), onChange: event => updateDraft({ confirm_reinforcement: event.target.checked, expected_rule_body: selectedMatch.body }) }),
              "I have compared the wording and confirm this correction supports the existing rule.") : null) : null,
          ruleId && !selectedMatch ? h(Notice, null, "Your selected rule is outside these target skills or is no longer available. Choose another match or explicitly choose a separate rule.") : null,
          currentDraft.separate_rule ? h(Notice, null, "You chose a separate rule. Existing rules will remain unchanged.") : null,
          !ruleId && loadDocument ? skills.filter(skill => skillIds.includes(skill.id)).map(skill => h(ManualPlacement, {
            key: skill.id, skill, body, loadDocument, busy, placement: placements.find(placement => placement.skill_id === skill.id),
            onReady: value => setPlacementReady(current => ({ ...current, [skill.id]: value })),
            onChange: placement => updateDraft({ placements: [...placements.filter(value => value.skill_id !== skill.id), ...(placement ? [placement] : [])] }) })) : null,
          h("div", { className: "oms-manual-actions" },
            !ruleId ? h(Button, { disabled: !canCreate, onClick: () => void decide("create") }, currentDraft.separate_rule ? "Create separate rule" : "Create rule") : null,
            matches.length && !replacement ? h(Button, { variant: "secondary", disabled: !canReinforce, onClick: () => void decide("reinforce") }, supportsSimilarity ? "Reinforce existing rule" : "Reinforce exact match") : null,
            ruleId ? h(Button, { variant: "secondary", disabled: busy, onClick: () => updateDraft({ rule_id: "", separate_rule: true }) }, "Choose a separate rule instead") : null))),
      h("div", { className: "oms-review-reject" }, h(Button, { variant: "danger", disabled: busy, onClick: () => void decide("reject") }, "Reject correction"))));
}
