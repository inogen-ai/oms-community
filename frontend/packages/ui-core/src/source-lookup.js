import { createElement as h } from "react";
import { Button, Notice } from "./primitives.js";

const IMPORT_PATHS = new Set(["/api/skill-source-discoveries", "/api/skill-source-installations", "/api/skill-local-imports"]);
const REF_OPTIONS = [
  ["", "Default branch (recommended)"],
  ["branch", "A branch: follow new commits on it"],
  ["tag", "A tag: stay on this tag; if the tag is moved, the next check picks it up"],
  ["commit", "A commit: pin exactly this commit; it never updates"],
];
const NOTICE_TEXT = {
  import: "A previous import did not confirm. Your browser sent the request but did not get the answer. Check its result before trying again; checking does not start a new import.",
  request: "A previous request did not confirm. Your browser sent it but did not get the answer. Check its result before trying again; checking does not start a new request.",
};

// Opened by a value arriving, never closed by one, and not forced open again while the same value stays:
// a user who closed it with a tag chosen must not see it reopen on the next URL keystroke. The form is
// hook-free, so the previous state lives per DOM element, which cannot leak between instances.
const wasFilled = new WeakMap();
function openOnFirstValue(element, filled) {
  if (!element) return;
  if (filled && !wasFilled.get(element)) element.open = true;
  wasFilled.set(element, filled);
}

export function sourceLookupValue(initialUrl = "") {
  return { url: initialUrl, kind: "", name: "", path: "", profile: "" };
}
export function sourceLookupBody(value) {
  const path = value.path.trim(), profile = value.profile.trim();
  return { url: value.url.trim(), ...(value.kind ? { ref_kind: value.kind, ref_name: value.name.trim() } : {}),
    ...(path ? { package_path: path } : {}), ...(profile ? { credential_profile_id: profile } : {}) };
}
export function sourcePendingSubject(path) {
  return IMPORT_PATHS.has(String(path).split("?")[0]) ? "import" : "request";
}

function profileRow(profile, value, set, disabled) {
  if (!profile) return null;
  if (profile.mode === "text") return [
    h("label", { key: "profile" }, "Access profile for a private repository (optional)",
      h("input", { value: value.profile, disabled, autoComplete: "off", onChange: event => set({ profile: event.target.value }) })),
    h("p", { key: "help", className: "muted" }, "If this OMS server has an access profile configured for private repositories, enter its id here. The profile holds the GitHub token on the server; the token never appears in OMS."),
  ];
  if (profile.mode === "select" && profile.profiles.length) return h("label", null, "Private repository access: choose a profile",
    h("select", { value: value.profile, disabled, onChange: event => set({ profile: event.target.value }) },
      h("option", { value: "" }, "Public repository"), profile.profiles.map(id => h("option", { key: id, value: id }, id))));
  return h("p", null, "Importing from a private repository needs an access profile. Ask your administrator.");
}

export function SourceLookupForm({ value, onChange, onSubmit, profile, busy = false, error, submitLabel, children }) {
  const set = change => onChange({ ...value, ...change });
  const filled = !!(value.kind || value.path || value.profile);
  const incomplete = !value.url.trim() || (!!value.kind && !value.name.trim());
  return h("form", { className: "form-grid oms-source-lookup", onSubmit: event => { event.preventDefault(); onSubmit(sourceLookupBody(value)); } },
    h("label", null, "GitHub repository or skill folder URL",
      h("input", { type: "url", required: true, value: value.url, disabled: busy, onChange: event => set({ url: event.target.value }) })),
    h("details", { className: "oms-source-advanced", ref: element => openOnFirstValue(element, filled) }, h("summary", null, "Advanced"),
      h("label", null, "What to follow",
        h("select", { value: value.kind, disabled: busy, onChange: event => set({ kind: event.target.value }) },
          REF_OPTIONS.map(([option, label]) => h("option", { key: option, value: option }, label)))),
      value.kind ? h("label", null, "Branch, tag or commit",
        h("input", { required: true, value: value.name, disabled: busy, onChange: event => set({ name: event.target.value }) })) : null,
      h("label", null, "Skill folder (optional)",
        h("input", { value: value.path, disabled: busy, onChange: event => set({ path: event.target.value }) })),
      profileRow(profile, value, set, busy)),
    error ? h(Notice, { kind: "error" }, error) : null,
    children,
    h(Button, { type: "submit", disabled: busy || incomplete }, busy ? "Discovering…" : submitLabel ?? "Discover packages"));
}

export function SourcePendingNotice({ subject = "request", onCheck, busy = false, children }) {
  return h("div", { className: "oms-source-pending" },
    h(Notice, null, h("p", null, NOTICE_TEXT[subject])),
    h(Button, { variant: "secondary", disabled: busy, onClick: onCheck }, "Check result"),
    children);
}
