"use client";
import { createElement as h, cloneElement, isValidElement, Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Markdown } from "./markdown.js";
import { DiffLines, historyDiffLines, inlineDiff } from "./diff.js";
import { Button } from "./primitives.js";

/** Grow with the text; preserve the browser's manual resize affordance. */
export function AutoTextarea({ value, inputRef, ...props }) {
  const ref = useRef(null);
  function fit() { if (ref.current) { ref.current.style.height = "auto"; ref.current.style.height = `${ref.current.scrollHeight + 2}px`; } }
  useEffect(fit, [value]);
  useEffect(() => {
    if (typeof ResizeObserver === "undefined" || !ref.current) return;
    let width = ref.current.getBoundingClientRect().width;
    const observer = new ResizeObserver(entries => {
      const next = entries[0].contentRect.width;
      if (next !== width) { width = next; fit(); }
    });
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return h("textarea", { ...props, value, ref: element => { ref.current = element; inputRef?.(element); } });
}

/** One document surface: every passage is editable without opening a sub-editor.
 * Stable part identities keep edits attached to their original rules and prose.
 */
export function SkillTextEditor({ parts, values, onChange, disabled = false, focusAnchor,
  replacementText, actionsForPart, guidanceOnly = false, allowRuleRemoval = false }) {
  const fields = useRef(new Map());
  const viewport = useRef(null);
  const activeHit = useRef(null);
  const focusSearchHit = useRef(false);
  const [view, setView] = useState("markdown");
  const passages = useRef(new Map());
  const pendingSelection = useRef(null);
  const [query, setQuery] = useState("");
  const [matchIndex, setMatchIndex] = useState(-1);
  const [selection, setSelection] = useState(null);
  let heading = "Introduction";
  const counts = new Map();
  const rows = parts.filter(part => !guidanceOnly || !["name", "description", "frontmatter"].includes(part.kind)).map(part => {
    if (["heading", "group", "title"].includes(part.kind)) heading = part.edit_text || part.lines.join(" ").replace(/^#+\s*/, "").trim();
    const editable = part.editable && (!guidanceOnly || ["rule", "overflow-rule", "prose"].includes(part.kind));
    const kind = ["rule", "overflow-rule"].includes(part.kind) ? "Rule" : part.kind === "prose" ? "Passage" : "Text";
    const key = `${heading}:${kind}`;
    counts.set(key, (counts.get(key) || 0) + 1);
    return { part, heading, kind, editable, label: `${heading} · ${kind} ${counts.get(key)}`,
      text: values[part.anchor] ?? part.edit_text ?? part.lines.join("\n") };
  });
  const matches = [];
  const needle = query.toLocaleLowerCase();
  if (needle) for (const row of rows) {
    const haystack = row.text.toLocaleLowerCase();
    for (let offset = 0, at; (at = haystack.indexOf(needle, offset)) !== -1; offset = at + needle.length) {
      matches.push({ anchor: row.part.anchor, start: at, end: at + needle.length });
    }
  }
  const matchingAnchors = new Set(matches.map(match => match.anchor));

  function center(element) {
    const pane = viewport.current;
    if (!pane || !element) return;
    // Fit the editor and its search controls into the visible browser window.
    // Centring only its inner scroll container can otherwise leave the hit
    // below the fold, especially after the correction context on a phone.
    const shell = pane.parentElement;
    const height = window.visualViewport?.height ?? window.innerHeight;
    const controls = pane.getBoundingClientRect().top - shell.getBoundingClientRect().top;
    pane.style.maxHeight = `${Math.max(160, Math.min(height * .72, height - controls - 40))}px`;
    shell.scrollIntoView?.({ block: "center", inline: "nearest", behavior: "instant" });
    const rect = element.getBoundingClientRect();
    const frame = pane.getBoundingClientRect();
    pane.scrollTo?.({ top: pane.scrollTop + rect.top - frame.top - pane.clientHeight / 2 + Math.min(rect.height, 24) / 2, behavior: "instant" });
  }
  function reveal(anchor, start = 0, end = start, focus = true) {
    const field = fields.current.get(anchor);
    if (field) {
      if (focus) field.focus({ preventScroll: true });
      field.setSelectionRange(start, end);
      setSelection({ anchor, start, end, text: field.value });
    } else setSelection(null);
    const hit = view === "preview" ? passages.current.get(anchor)?.querySelectorAll("mark")[
      matches.slice(0, Math.max(0, matchIndex)).filter(match => match.anchor === anchor).length] : fields.current.has(anchor) ? activeHit.current : passages.current.get(anchor)?.querySelector("mark");
    center(hit || field || passages.current.get(anchor));
  }
  useEffect(() => {
    setMatchIndex(query && matches.length ? 0 : -1);
    focusSearchHit.current = false;
    if (!query) setSelection(null);
  }, [query, view, parts.length]);
  useEffect(() => {
    if (matchIndex < 0 || !matches[matchIndex]) return;
    const frame = requestAnimationFrame(() => {
      const match = matches[matchIndex]; reveal(match.anchor, match.start, match.end, focusSearchHit.current);
      focusSearchHit.current = false;
    });
    return () => cancelAnimationFrame(frame);
  }, [matchIndex, query, view]);
  useEffect(() => {
    if (focusAnchor && fields.current.has(focusAnchor)) reveal(focusAnchor);
  }, [focusAnchor, parts.length]);
  useEffect(() => {
    if (pendingSelection.current) {
      const next = pendingSelection.current; pendingSelection.current = null;
      reveal(next.anchor, next.start, next.end);
    }
  }, [values]);
  function find(direction) {
    if (!matches.length) return;
    const next = matchIndex < 0 ? 0 : (Math.min(matchIndex, matches.length - 1) + direction + matches.length) % matches.length;
    focusSearchHit.current = true;
    if (next === matchIndex) { const match = matches[next]; reveal(match.anchor, match.start, match.end); }
    else setMatchIndex(next);
  }
  function highlighted(children) {
    if (!needle) return children;
    if (Array.isArray(children)) return children.map((child, index) => h(Fragment, { key: index }, highlighted(child)));
    if (typeof children === "string") {
      const result = []; let offset = 0, at;
      while ((at = children.toLocaleLowerCase().indexOf(needle, offset)) !== -1) {
        result.push(children.slice(offset, at), h("mark", { key: at }, children.slice(at, at + needle.length)));
        offset = at + needle.length;
      }
      result.push(children.slice(offset)); return result;
    }
    if (isValidElement(children) && children.type !== "mark") return cloneElement(children, {}, highlighted(children.props.children));
    return children;
  }
  const previewComponents = Object.fromEntries(["p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "td", "th", "code"].map(tag =>
    [tag, ({ children, node, ...props }) => h(tag, props, highlighted(children))]));
  function previewText(part, text) {
    if (!(part.anchor in values)) return part.lines.join("\n");
    if (["rule", "overflow-rule"].includes(part.kind)) {
      const avoid = part.lines.join("\n").startsWith("* Avoid:") && !/^(do\s*not|don['’]?t|never|avoid)\b/i.test(text.trim());
      return `* ${avoid ? "Avoid: " : ""}${text}`;
    }
    if (["heading", "title", "group"].includes(part.kind)) return `${part.lines[0]?.match(/^#+/)?.[0] || "##"} ${text}`;
    if (part.kind === "description" && /^description:/.test(part.lines[0] || "")) return `description: ${text}`;
    return text;
  }
  const selectedRow = selection && rows.find(row => row.part.anchor === selection.anchor);
  const canReplace = !disabled && replacementText?.trim() && selection && selection.end > selection.start && selectedRow?.text === selection.text;
  function replaceSelection() {
    if (!canReplace) return;
    const { anchor, start, end, text } = selection;
    pendingSelection.current = { anchor, start, end: start + replacementText.length };
    onChange(anchor, text.slice(0, start) + replacementText + text.slice(end));
    setSelection(null);
  }
  return h("div", { className: "oms-text-editor" },
    h("div", { className: "oms-text-editor__viewbar" },
      h("span", { className: "oms-manual-help" }, view === "markdown" ? "Edit Markdown text" : "Preview of your current draft"),
      h("div", { role: "group", "aria-label": "Skill view", className: "oms-segmented" },
        ...["markdown", "preview"].map(mode => h("button", { key: mode, type: "button", "aria-pressed": view === mode,
          onClick: () => setView(mode) }, mode === "markdown" ? "Markdown" : "Preview")))),
    h("div", { className: "oms-text-editor__toolbar" },
      h("label", null, "Find in skill", h("input", { type: "search", value: query, placeholder: "Find a phrase, e.g. field types",
        onChange: event => setQuery(event.target.value),
        onKeyDown: event => { if (event.key === "Enter") { event.preventDefault(); find(event.shiftKey ? -1 : 1); } } })),
      h("span", { role: "status", className: "oms-manual-help" }, query ? `${matches.length ? Math.min(matchIndex + 1, matches.length) : 0} of ${matches.length} matches` : "Search keeps the whole skill in view"),
      h(Button, { variant: "secondary", disabled: !matches.length, onClick: () => find(-1) }, "Previous match"),
      h(Button, { variant: "secondary", disabled: !matches.length, onClick: () => find(1) }, "Next match"),
      replacementText && view === "markdown" ? h(Button, { variant: "secondary", disabled: !canReplace, onMouseDown: event => event.preventDefault(), onClick: replaceSelection }, "Replace selection with correction") : null),
    h("div", { ref: viewport, className: `oms-text-editor__document${query ? " has-search" : ""}`, role: "region", "aria-label": "Skill text editor" },
      ...rows.map(({ part, editable, label, text, kind }) => {
        if (!part.lines.join("").trim() && !editable) return null;
        const changed = part.anchor in values;
        const removing = changed && allowRuleRemoval && kind === "Rule" && !text.trim();
        return h("section", { key: part.anchor, ref: element => { if (element) passages.current.set(part.anchor, element); else passages.current.delete(part.anchor); },
          className: `oms-text-editor__passage${changed ? " is-changed" : ""}${matchingAnchors.has(part.anchor) ? " is-match" : ""}`,
          "data-anchor": part.anchor },
          view === "preview" ? (removing ? h("p", { className: "oms-manual-help" }, "Rule marked for removal") : h(Markdown, { body: previewText(part, text), inert: true, components: previewComponents })) :
          editable ? h("div", { className: kind === "Rule" ? "oms-text-editor__rule" : undefined },
            kind === "Rule" ? h("span", { "aria-hidden": true, className: "oms-text-editor__bullet" }, "•") : null,
            h("div", { className: "oms-text-editor__field" },
              query ? h("div", { className: "oms-text-editor__highlight", "aria-hidden": true }, (() => {
                const segments = []; let offset = 0;
                matches.forEach((match, index) => {
                  if (match.anchor !== part.anchor) return;
                  segments.push(text.slice(offset, match.start), h("mark", { key: match.start, className: index === matchIndex ? "is-active" : undefined,
                    ...(index === matchIndex ? { ref: element => { activeHit.current = element; } } : {}) }, text.slice(match.start, match.end)));
                  offset = match.end;
                });
                segments.push(text.slice(offset), "\n"); return segments;
              })()) : null,
            h(AutoTextarea, { "aria-label": label, rows: 1, value: text, disabled, spellCheck: false,
              inputRef: element => { if (element) fields.current.set(part.anchor, element); else fields.current.delete(part.anchor); },
              onSelect: event => setSelection({ anchor: part.anchor, start: event.target.selectionStart, end: event.target.selectionEnd, text: event.target.value }),
              onChange: event => { setSelection(null); onChange(part.anchor, event.target.value); } })))
            : h("pre", { className: "oms-text-editor__raw" }, highlighted(part.lines.join("\n"))),
          changed ? h("div", { className: "oms-text-editor__change" }, h("span", null, removing ? "Marked for removal" : "Edited"),
            h(Button, { variant: "secondary", disabled, "aria-label": `Undo edit: ${label}`, onClick: () => onChange(part.anchor, part.edit_text ?? "") }, "Undo edit")) : null,
          changed && !text.trim() ? h("p", { className: "oms-manual-help" }, removing ? "This rule will be removed when you apply the changes. Undo edit to keep it." : "Enter replacement text or undo this edit before saving.") : null,
          actionsForPart?.(part));
      })));
}

/** The publisher supplies ordering and editability, never a second renderer. */
export function DocumentPartsEditor({ parts, values, onChange, disabled = false, actionsForPart }) {
  return h("div", { className: "oms-document-parts" }, parts.map((part, index) => {
    if (!part.lines.join("").trim() && !part.editable) return null;
    const label = `${part.kind.replaceAll("-", " ")} ${index + 1}`;
    return h("section", { key: part.anchor, className: "oms-document-part", "data-anchor": part.anchor },
      part.editable ? h("label", null, label,
        h(AutoTextarea, { rows: part.kind === "rule" ? 2 : 3, value: values[part.anchor] ?? part.edit_text ?? "", disabled,
          onChange: (event) => onChange(part.anchor, event.target.value) }))
        : h(Markdown, { body: part.lines.join("\n"), inert: true }),
      part.note ? h("p", { className: "muted" }, part.note) : null,
      actionsForPart?.(part));
  }));
}

function overviewLines(before, after) {
  const saved = [{ parts: [], changed: false }], current = [{ parts: [], changed: false }];
  function append(lines, run) {
    for (const text of run.text.split(/(\r\n|\r|\n)/)) {
      if (!text) continue;
      const line = lines.at(-1);
      line.parts.push({ kind: run.kind, text });
      if (run.kind !== "same") line.changed = true;
      if (/^(\r\n|\r|\n)$/.test(text)) lines.push({ parts: [], changed: false });
    }
  }
  for (const run of inlineDiff(before ?? "", after ?? "")) {
    // Mark the corresponding line on both sides, including the side where an
    // inserted/deleted comma has no character of its own to highlight.
    if (run.kind !== "same") { saved.at(-1).changed = true; current.at(-1).changed = true; }
    if (run.kind !== "add") append(saved, run);
    if (run.kind !== "del") append(current, run);
  }
  return [saved, current];
}

function OverviewDiff({ row }) {
  const sides = useMemo(() => overviewLines(row.old_text, row.new_text), [row.old_text, row.new_text]);
  return h("div", { className: "oms-version-columns" },
    ...[["Saved version", row.old_text, "saved"], ["Current document", row.new_text, "current"]].map(([label, text, side], index) =>
      h("div", { key: side }, h("h3", null, label),
        h("pre", { className: `oms-version-text oms-version-text--${side}`, "aria-label": `${label} text` },
          text === null ? "Not present" : sides[index].map((line, lineIndex) => h("span", { key: lineIndex,
            className: `oms-version-text__line${line.changed ? " is-changed" : ""}` },
            ...line.parts.map((part, partIndex) => part.kind === "same" ? part.text : h(part.kind === "del" ? "del" : "ins", {
              key: partIndex, title: part.kind === "del" ? "Removed since this saved version" : "Added since this saved version",
            }, part.text))))))));
}

/** Comparison data and restore decisions stay with the caller's API. */
export function VersionComparison({ rows, selected, onSelect, disabled = false, view = "overview" }) {
  const detailed = view === "detailed";
  return h("div", { className: `oms-version-comparison oms-version-comparison--${view}` }, rows.filter(row => detailed || row.state !== "same").map(row =>
    row.state === "same"
      ? h("div", { key: row.anchor, className: "oms-version-context", "data-anchor": row.anchor }, h(DiffLines, { lines: historyDiffLines(row) }))
      : h("article", { key: row.anchor, className: "oms-version-change", "data-anchor": row.anchor },
      h("header", null,
        row.restorable ? h("label", { className: "check-label" },
          h("input", { type: "checkbox", disabled, checked: selected.includes(row.anchor), onChange: (event) => onSelect(row.anchor, event.target.checked) }),
          `Restore ${row.kind.replaceAll("-", " ")}`) : h("strong", null, row.kind.replaceAll("-", " ")),
        h("span", { className: "muted" }, row.state === "only_old" ? "Removed since this version" : row.state === "only_new" ? "Added since this version" : "Wording changed")),
      detailed ? h(DiffLines, { lines: historyDiffLines(row) }) : h(OverviewDiff, { row }),
      row.note ? h("p", { className: "muted" }, row.note) : null)));
}
