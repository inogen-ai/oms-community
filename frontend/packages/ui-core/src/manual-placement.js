"use client";
import { createElement as h, useEffect, useState } from "react";
import { Button, Notice } from "./primitives.js";
import { Markdown } from "./markdown.js";

/** Pick an insertion point in context, using the same revision-checked contract. */
export function ManualPlacement({ skill, body, placement, loadDocument, onChange, onReady, busy }) {
  const [open, setOpen] = useState(Boolean(placement));
  const [document, setDocument] = useState(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setDocument(null); setError("");
    Promise.resolve().then(() => loadDocument(skill.id)).then(result => { if (!cancelled) setDocument(result); })
      .catch(failure => { if (!cancelled) setError(failure instanceof Error ? failure.message : "Could not load this skill."); });
    return () => { cancelled = true; };
  }, [open, skill.id, loadDocument, reload]);
  const stale = Boolean(placement && document && placement.revision !== document.revision);
  const ready = !placement || Boolean(document && !stale && !error);
  useEffect(() => { onReady(ready); }, [ready]);
  const sections = document?.rule_sections ?? [];
  const chosen = sections.find(section => section.id === placement?.section_id);
  function slot(section, after) {
    const position = after ? "after" : "start";
    const selected = placement?.section_id === section.id && (placement.position === position && placement.after_rule_id === after?.id
      || placement.position === "end" && after?.id === section.rules.at(-1)?.id);
    return h("div", { className: "oms-insertion-slot", key: `slot-${section.id}-${after?.id || "start"}` },
      selected ? h("div", { className: "oms-placement-added" }, h("strong", null, "New rule here"),
        h(Markdown, { body: body || "Write the proposed rule above.", inert: true })) : null,
      h(Button, { variant: "secondary", disabled: busy, "aria-pressed": selected,
        "aria-label": after ? `Insert after: ${after.body}` : `Insert at start of ${section.heading || "Instructions"}`,
        onClick: () => onChange({ skill_id: skill.id, revision: document.revision, section_id: section.id, position,
          ...(after ? { after_rule_id: after.id } : {}) }) }, selected ? "Selected position" : "Insert here"));
  }
  return h("section", { className: "oms-manual-placement", "aria-label": `Placement in ${skill.name}` },
    h("div", { className: "oms-target-skills__summary" },
      h("div", null, h("h3", null, `New rule in ${skill.name}`), h("p", { className: "oms-manual-help" },
        placement ? `Selected position in ${chosen?.heading || "the skill"}` : "Added at the end of the skill")),
      h(Button, { variant: "secondary", disabled: busy, "aria-expanded": open, onClick: () => setOpen(!open) }, open ? "Done placing rule" : `Choose where to insert · ${skill.name}`)),
    !open ? null : h("div", null,
      error ? h(Notice, { kind: "error" }, error, h(Button, { variant: "secondary", onClick: () => setReload(value => value + 1) }, "Retry placement choices")) : null,
      !document && !error ? h("p", { role: "status" }, "Loading skill…") : null,
      stale ? h(Notice, { kind: "error" }, "The skill changed. Select an insertion point again before creating the rule.") : null,
      document ? h("div", { className: "oms-placement-preview", role: "region", "aria-label": `New rule placement in ${skill.name}` },
        h("p", { className: "oms-manual-help" }, "Choose Insert here between the existing rules."),
        h(Button, { variant: "secondary", disabled: busy, "aria-pressed": !placement, onClick: () => onChange(undefined) }, "Use end of skill"),
        ...sections.map(section => h("section", { key: section.id }, h("h4", null, section.heading || "Instructions"), slot(section),
          ...section.rules.flatMap(rule => [h("div", { key: rule.id, className: "oms-placement-existing" },
            rule.group ? h("p", { className: "oms-manual-help" }, rule.group) : null, h(Markdown, { body: rule.body, inert: true })), slot(section, rule)]))),
        !sections.length ? h("p", null, "This skill has no existing rule section. New rules will appear at the end. To change its paragraphs, use Edit skill text.") : null) : null,
      h(Button, { variant: "secondary", disabled: busy, onClick: () => { onReady(false); setReload(value => value + 1); } }, "Refresh placement choices")));
}
