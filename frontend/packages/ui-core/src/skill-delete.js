import { createElement as h, useId } from "react";
import { Button } from "./primitives.js";

/** Both editions share the confirmation; each caller owns its delete request. */
export function SkillDeleteConfirmation({ name, confirmation, onConfirmationChange, onConfirm, onCancel, busy = false, hasUnsavedChanges = false }) {
  const descriptionId = useId();
  return h("section", { className: "oms-skill-delete", "aria-label": "Delete skill confirmation",
    onKeyDown: event => { if (event.key === "Escape" && !busy) { event.stopPropagation(); onCancel(); } } },
    h("h3", null, `Delete “${name}”?`),
    h("div", { id: descriptionId },
      h("p", null, "This permanently removes the skill, its document, attached files and saved versions from this workspace. Rules and correction evidence are retained, including rules used by other skills."),
      h("p", null, "Publish again to remove this skill from your agents. This cannot be undone."),
      hasUnsavedChanges ? h("p", { className: "oms-skill-delete__warning" }, "Your unsaved edits to this skill will also be discarded.") : null),
    h("label", null, "Type the skill name to confirm deletion",
      h("input", { value: confirmation, placeholder: name, autoComplete: "off", autoFocus: true, disabled: busy, "aria-describedby": descriptionId,
        onChange: event => onConfirmationChange(event.target.value) })),
    h("div", { className: "oms-skill-delete__actions" },
      h(Button, { variant: "danger", disabled: busy || confirmation !== name, onClick: () => { if (!busy && confirmation === name) onConfirm(); } }, busy ? "Deleting…" : "Delete permanently"),
      h(Button, { variant: "secondary", disabled: busy, onClick: onCancel }, "Cancel")));
}
