import { createElement as h } from "react";

function splitLines(text) {
  return text.replace(/\r\n/g, "\n").split("\n");
}

/** Line-level LCS diff. Compare individual document parts to keep the table small. */
export function lineDiff(before, after) {
  return sequenceDiff(splitLines(before), splitLines(after));
}

function sequenceDiff(a, b) {
  const n = a.length;
  const m = b.length;
  const lcs = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const out = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ kind: "del", text: a[i++] });
    } else {
      out.push({ kind: "add", text: b[j++] });
    }
  }
  while (i < n) out.push({ kind: "del", text: a[i++] });
  while (j < m) out.push({ kind: "add", text: b[j++] });
  return out;
}

/** Preserve every character, treating punctuation separately from words. */
export function inlineDiff(before, after) {
  const tokens = text => text.match(/[\p{L}\p{M}\p{N}_]+|\s+|[^\p{L}\p{M}\p{N}_\s]/gu) ?? [];
  const a = tokens(before), b = tokens(after);
  let start = 0, endA = a.length, endB = b.length;
  while (start < endA && start < endB && a[start] === b[start]) start++;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
  const oldMiddle = a.slice(start, endA), newMiddle = b.slice(start, endB);
  // Bound the table for large rewrites. Common prefixes and suffixes still stay
  // unmarked; a one-character edit in a long passage needs no large table.
  const middle = oldMiddle.length * newMiddle.length > 1_000_000
    ? [{ kind: "del", text: oldMiddle.join("") }, { kind: "add", text: newMiddle.join("") }]
    : sequenceDiff(oldMiddle, newMiddle);
  const runs = [];
  for (const run of [{ kind: "same", text: a.slice(0, start).join("") }, ...middle,
    { kind: "same", text: a.slice(endA).join("") }]) {
    if (!run.text) continue;
    if (runs.at(-1)?.kind === run.kind) runs.at(-1).text += run.text;
    else runs.push({ ...run });
  }
  return runs;
}

function sideLines(text, kind) {
  return text === null ? [] : splitLines(text).map(line => ({ kind, text: line }));
}

/** Current → saved: additions are what restoring brings back, deletions what it removes. */
export function historyDiffLines(row) {
  if (row.state === "same") return sideLines(row.old_text ?? row.new_text, "same");
  // A missing part is not an empty line: never invent a blank +/- for its absent side.
  if (row.new_text === null) return sideLines(row.old_text, "add");
  if (row.old_text === null) return sideLines(row.new_text, "del");
  return lineDiff(row.new_text, row.old_text);
}

/** Shared by history, document editing and proposed revisions in both editions. */
export function DiffLines({ lines, className = "", ...props }) {
  return h("div", { ...props, className: `oms-line-diff gx-review__diff ${className}`.trim() },
    lines.map((line, index) => h("div", { key: index, className: `oms-line-diff__line oms-line-diff__line--${line.kind} gx-review__line gx-review__line--${line.kind}` },
      h("span", { className: "oms-line-diff__gutter gx-review__gutter" }, line.kind === "add" ? "+" : line.kind === "del" ? "-" : ""),
      h("span", { className: "oms-line-diff__text gx-review__text" }, line.text || " "))));
}
