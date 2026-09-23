"use client";
import { createElement as h } from "react";
import { FileUp } from "lucide-react";

/** One archive picker; the consumer owns its edition's import operation. */
export function SkillPackagePicker({ onChoose, onChooseDirectory, directory = false, disabled = false, label = "Choose a package", inputLabel = "Skill ZIP package", className = "oms-package-picker", children }) {
  return h("label", { className },
    h(FileUp, { size: 16, "aria-hidden": true }),
    h("span", null, label),
    h("input", {
      type: "file", ...(directory ? { webkitdirectory: "", multiple: true } : { accept: ".zip,application/zip" }), disabled, "aria-label": inputLabel,
      onChange(event) {
        if (directory) { onChooseDirectory?.(Array.from(event.target.files || [])); event.target.value = ""; return; }
        const file = event.target.files?.[0];
        if (file) onChoose(file);
        event.target.value = "";
      },
    }), children);
}
