import { createElement as h, Fragment } from "react";
import { Button, Notice } from "./primitives.js";
import { DiffLines, lineDiff } from "./diff.js";

const BYTE_LIMIT = 65536;
const LINE_LIMIT = 1000;
/** Bound work before the quadratic line diff, including unexpectedly oversized responses. */
export function boundedSourceText(value) {
  const original = typeof value === "string" ? value : "";
  const prefix = original.slice(0, BYTE_LIMIT);
  const bytes = new TextEncoder().encode(prefix);
  const decoded = new TextDecoder().decode(bytes.subarray(0, BYTE_LIMIT), { stream: bytes.length > BYTE_LIMIT });
  const lines = decoded.split(/\r\n|\n|\r/);
  const text = lines.slice(0, LINE_LIMIT).join("\n");
  return { text, truncated: prefix.length < original.length || bytes.length > BYTE_LIMIT || lines.length > LINE_LIMIT };
}

/**
 * The sides a comparison has no copy of: no version was taken from GitHub before, OMS does not have
 * the part (nothing of a new skill is in OMS yet), or GitHub removed it. An absent side has no
 * preview and no Download button.
 */
export function sourceAbsentSides(change, heldInstall = false) {
  return [change.base.kind !== "known" ? "base" : null, heldInstall || change.local.kind === "absent" ? "local" : null,
    change.incoming.kind === "absent" ? "upstream" : null].filter(Boolean);
}

/** The preview's new scrollTop: the line sits up to two lines (at most a quarter of the preview) below its top edge. */
export function sourceLineScrollTop(container, line, scrollTop) {
  const context = Math.min(line.height * 2, container.height / 4);
  return Math.max(0, Math.round(scrollTop + line.top - container.top - context));
}

/**
 * Scrolls the GitHub preview inside `root` (a "Show" disclosure) to the line a line-number button
 * asked for (`data-oms-line`), or else to the first flagged line. An asked-for line also takes focus,
 * so the reader lands on it. Plain DOM work: the card stays hook-free.
 */
export function revealSourceLine(root) {
  const preview = root.querySelector(".oms-source-numbered");
  const asked = /^\d+$/.test(root.dataset.omsLine ?? "") ? root.dataset.omsLine : null;
  const target = preview ? preview.querySelector(asked ? `[data-line="${asked}"]` : ".oms-source-flagged") : null;
  if (!target) return;
  preview.scrollTop = sourceLineScrollTop(preview.getBoundingClientRect(), target.getBoundingClientRect(), preview.scrollTop);
  if (asked) {
    delete root.dataset.omsLine;
    preview.focus?.({ preventScroll: true });
  }
  // Brings the line into the page's view too, when the preview sits below the fold.
  target.scrollIntoView?.({ block: "nearest" });
}
// A preview that arrives after its disclosure opened scrolls itself. Kept at module level so React
// calls it once per mounted preview, not again on every render.
function revealWhenShown(preview) {
  const details = preview?.closest("details");
  if (details?.open) revealSourceLine(details);
}

/**
 * The GitHub side's text with line numbers. Each flagged line is marked and tagged in plain words;
 * a flagged line the preview does not hold is named below it. It only ever sits inside a disclosure
 * the reader opens (ruling P4).
 */
export function numberedSourceText(text, label, flagged = []) {
  const lines = text.split("\n");
  const tags = new Map(flagged.map(row => [row.line, row.label]));
  return h(Fragment, null,
    h("pre", { tabIndex: 0, "aria-label": label, className: "oms-source-numbered", ref: revealWhenShown }, lines.map((line, index) => {
      const number = index + 1, tag = tags.get(number);
      return h("span", { key: number, className: tag ? "oms-source-line oms-source-flagged" : "oms-source-line", "data-line": number },
        line, tag ? " " : null, tag ? h("span", { className: "oms-source-flag" }, tag) : null);
    })),
    flagged.filter(row => row.line > lines.length).map(row => h("p", { key: row.line }, `Line ${row.line} is beyond this preview.`)));
}

const SIDES = [["base", "Last version taken from GitHub", "base"], ["local", "In OMS now", "current"], ["upstream", "New on GitHub", "incoming"]];

export function SourceFileComparison({ path, base, local, upstream, absent = [], flagged = [], onDownload, publicationWarning, busy = false }) {
  const values = { base, local, upstream };
  const missing = new Set(absent);
  // A new file, or any file of a new skill: only the GitHub side has something to show.
  const fresh = missing.has("base") && missing.has("local");
  const bounded = SIDES.filter(([side]) => !fresh || side === "upstream").map(([side, title, download]) => ({ side, title, download,
    value: missing.has(side) ? null : values[side], absent: missing.has(side), preview: boundedSourceText(missing.has(side) ? null : values[side]?.text),
  }));
  const diffable = !missing.size && bounded.every(({ value, preview }) => value && !value.loading && !value.error && !value.unavailable
    && !value.binary && typeof value.text === "string" && !value.truncated && !preview.truncated && (value.size === undefined || value.size <= BYTE_LIMIT));
  return h("section", { className: "oms-source-files", "aria-label": `File comparison: ${path}` },
    h("h4", null, path), publicationWarning ? h(Notice, null, publicationWarning) : null,
    fresh ? h("p", { className: "oms-source-new" }, "This file is new. OMS does not have it yet.") : null,
    h("div", { className: fresh ? "oms-source-columns oms-source-columns-single" : "oms-source-columns" }, bounded.map(({ side, title, download, value, absent: gone, preview }) => {
      const excerpt = value?.truncated || preview.truncated || value?.size > BYTE_LIMIT;
      return h("section", { key: side, className: "oms-source-side", "aria-label": `${title}: ${path}` }, h("h5", null, title),
        gone ? h("p", null, "Not present")
          : value?.loading ? h("p", { role: "status" }, "Loading preview…") : value?.error ? h(Notice, { kind: "error" }, value.error)
            : value?.unavailable ? h("p", null, value.unavailable) : !value ? h("p", null, "Preview not loaded")
              : value.binary ? h("p", null, "Binary file. Text preview unavailable.")
                : typeof value.text === "string" ? h("div", null, excerpt ? h("p", null, "Bounded excerpt: download to inspect the complete file.") : null,
                  side === "upstream" ? numberedSourceText(preview.text, `${title} text`, flagged)
                    : h("pre", { tabIndex: 0, "aria-label": `${title} text` }, preview.text)) : h("p", null, "Text preview unavailable"),
        value?.size !== undefined ? h("p", null, `${value.size} bytes`) : null,
        value?.digest ? h("p", null, "Digest: ", h("code", null, value.digest)) : null,
        // An absent side has nothing to download, so it has no button (not a greyed-out one).
        gone ? null : h(Button, { variant: "secondary", disabled: busy || !value || !!value.loading || !!value.error || !!value.unavailable || !onDownload,
          onClick: () => { if (!busy && value && !value.loading && !value.error && !value.unavailable) onDownload?.(side, path); } }, `Download ${download}`));
    })),
    diffable ? h("details", null, h("summary", null, "Show line differences"),
      h("h5", null, "Base → current OMS"), h(DiffLines, { lines: lineDiff(bounded[0].preview.text, bounded[1].preview.text) }),
      h("h5", null, "Base → incoming"), h(DiffLines, { lines: lineDiff(bounded[0].preview.text, bounded[2].preview.text) })) : null);
}
