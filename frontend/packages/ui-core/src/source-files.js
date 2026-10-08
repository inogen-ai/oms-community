import { createElement as h } from "react";
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

export function SourceFileComparison({ path, base, local, upstream, onDownload, publicationWarning, busy = false }) {
  const sides = [["base", "Base (B)", "base", base], ["local", "Current OMS (L)", "current", local], ["upstream", "Incoming (U)", "incoming", upstream]];
  const bounded = sides.map(([side, title, download, value]) => ({ side, title, download, value,
    preview: boundedSourceText(value?.text),
  }));
  const diffable = bounded.every(({ value, preview }) => value && !value.loading && !value.error && !value.unavailable
    && !value.binary && typeof value.text === "string" && !value.truncated && !preview.truncated && (value.size === undefined || value.size <= BYTE_LIMIT));
  return h("section", { className: "oms-source-files", "aria-label": `File comparison: ${path}` },
    h("h4", null, path), publicationWarning ? h(Notice, null, publicationWarning) : null,
    h("div", { className: "oms-source-columns" }, bounded.map(({ side, title, download, value, preview }) => {
      const excerpt = value?.truncated || preview.truncated || value?.size > BYTE_LIMIT;
      return h("section", { key: side, className: "oms-source-side", "aria-label": `${title}: ${path}` }, h("h5", null, title),
        value?.loading ? h("p", { role: "status" }, "Loading preview…") : value?.error ? h(Notice, { kind: "error" }, value.error)
          : value?.unavailable ? h("p", null, value.unavailable) : !value ? h("p", null, "Preview not loaded")
            : value.binary ? h("p", null, "Binary file. Text preview unavailable.")
              : typeof value.text === "string" ? h("div", null, excerpt ? h("p", null, "Bounded excerpt — download to inspect the complete file.") : null,
                h("pre", { tabIndex: 0, "aria-label": `${title} text` }, preview.text)) : h("p", null, "Text preview unavailable"),
        value?.size !== undefined ? h("p", null, `${value.size} bytes`) : null,
        value?.digest ? h("p", null, "Digest: ", h("code", null, value.digest)) : null,
        h(Button, { variant: "secondary", disabled: busy || !value || !!value.loading || !!value.error || !!value.unavailable || !onDownload,
          onClick: () => { if (!busy && value && !value.loading && !value.error && !value.unavailable) onDownload?.(side, path); } }, `Download ${download}`));
    })),
    diffable ? h("details", null, h("summary", null, "Show line differences"),
      h("h5", null, "Base → current OMS"), h(DiffLines, { lines: lineDiff(bounded[0].preview.text, bounded[1].preview.text) }),
      h("h5", null, "Base → incoming"), h(DiffLines, { lines: lineDiff(bounded[0].preview.text, bounded[2].preview.text) })) : null);
}
