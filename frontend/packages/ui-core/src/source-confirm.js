import { createElement as h } from "react";
import { Button } from "./primitives.js";

/** Both editions share the wording; each caller owns the remove or relocate request. */
export function SourceChangeConfirmation({ action, destination = "", skills, busy = false, onConfirm, onCancel }) {
  const remove = action === "remove";
  return h("section", { className: "oms-source-confirm", role: "alertdialog",
    "aria-label": remove ? "Remove source confirmation" : "Change repository URL confirmation",
    onKeyDown: event => { if (event.key === "Escape" && !busy) { event.stopPropagation(); onCancel(); } } },
    h("p", null, remove
      ? "Warning: This action will remove the source from all skills that are part of this repository. Do you still want to remove it?"
      : `Warning: This action will move the source for all skills that are part of this repository to ${destination}. Do you still want to move it?`),
    h("details", null, h("summary", null, `Affected skills (${skills.length})`),
      h("ul", null, skills.map(skill => h("li", { key: skill.id }, skill.name)))),
    h("div", { className: "oms-source-actions" },
      h(Button, { variant: remove ? "danger" : "primary", disabled: busy, onClick: () => { if (!busy) onConfirm(); } }, "Yes"),
      h(Button, { variant: "secondary", autoFocus: true, disabled: busy, onClick: () => { if (!busy) onCancel(); } }, "No")));
}
