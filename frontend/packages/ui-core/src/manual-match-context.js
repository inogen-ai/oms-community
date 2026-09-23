"use client";
import { createElement as h, useEffect, useState } from "react";
import { Button } from "./primitives.js";
import { Markdown } from "./markdown.js";

const textOf = part => (part.lines ?? []).join("\n").trim();
const headingOf = part => (part.edit_text || textOf(part)).trim().replace(/^#{1,6}\s+/, "");

// Use the outline and saved placements, not word matching or inferred headings.
// Rule-only skills publish flat, but retain their imported section structure.
function contextParts(document, ruleId) {
  if (document.path !== "flat" || !document.rule_sections?.some(section => section.rules.some(rule => rule.id === ruleId))) return document.parts;
  const partsByRule = new Map(document.parts.filter(part => part.kind === "rule").map(part => [part.source_id || part.anchor.slice(5), part]));
  return document.rule_sections.flatMap(section => {
    let group = null;
    return [{ kind: "heading", lines: [`## ${section.heading}`], edit_text: section.heading }, ...section.rules.flatMap(rule => {
      const part = partsByRule.get(rule.id);
      if (!part) return [];
      const nextGroup = rule.group || null;
      const heading = nextGroup !== group ? [{ kind: "group", lines: nextGroup ? [`### ${nextGroup}`] : [] }] : [];
      group = nextGroup;
      return [...heading, part];
    })];
  });
}

// A shared rule can occur in several sections; overflow rules live in another file.
function contextsFor(document, ruleId) {
  const parts = contextParts(document, ruleId);
  return parts.flatMap((part, index) => {
    if (part.anchor !== `rule:${ruleId}`) return [];
    if (part.kind === "overflow-rule") return [{ part, location: "references/edge-cases.md", note: part.note }];
    let start = index;
    while (start > 0 && !["heading", "title"].includes(parts[start].kind)) start--;
    let end = index + 1;
    while (end < parts.length && !["heading", "title", "overflow-rule"].includes(parts[end].kind)) end++;
    let group = index;
    while (group > start && parts[group].kind !== "group") group--;
    const section = parts[start].kind === "heading" ? headingOf(parts[start]) : "";
    const subheading = parts[group].kind === "group" ? headingOf(parts[group]) : "";
    return [{ part, location: [section, subheading].filter(Boolean).join(" › ") || "SKILL.md (no section heading)",
      note: parts !== document.parts ? "Section layout from the saved skill; the published file uses a single list." : null,
      section: parts.slice(start, end), sectionName: section || "skill text" }];
  });
}

/** Fetch only while the comparison pane is open, once per distinct skill. */
export function useMatchDocuments({ active, matches, selected, loadDocument }) {
  const ids = JSON.stringify([...new Set(matches.flatMap(match => match.skill_ids ?? selected))].sort());
  const [result, setResult] = useState(null);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    if (!active || !loadDocument) return;
    let cancelled = false;
    setResult({ ids, loadDocument, rows: {} });
    for (const id of JSON.parse(ids)) {
      Promise.resolve().then(() => loadDocument(id)).then(document => ({ document }), error => ({ error }))
        .then(row => { if (!cancelled) setResult(current => ({ ...current, rows: { ...current.rows, [id]: row } })); });
    }
    return () => { cancelled = true; };
  }, [active, ids, loadDocument, reload]);
  return { rows: active && result?.ids === ids && result.loadDocument === loadDocument ? result.rows : {},
    retry: () => setReload(value => value + 1) };
}

export function RuleMatchContext({ match, skills, selected, documents }) {
  const id = typeof match === "string" ? match : match.id;
  const body = typeof match === "string" ? match : match.body;
  const skillIds = [...new Set(match.skill_ids ?? selected)];
  return h("div", null,
    !skillIds.length ? h("div", null, h("blockquote", null, body),
      h("p", { className: "oms-manual-help" }, "Choose a target skill to see this rule in context.")) : null,
    ...skillIds.map(skillId => {
      const name = skills.find(skill => skill.id === skillId)?.name || skillId;
      const row = documents.rows[skillId];
      const contexts = row?.document ? contextsFor(row.document, id) : [];
      return h("section", { key: skillId, className: "oms-match-context", "aria-label": `Rule context in ${name}` },
        h("p", { className: "oms-match-context__skill" }, name),
        h("p", { className: "oms-manual-help" }, "Current saved skill text"),
        !contexts.length ? h("blockquote", null, body) : null,
        !row ? h("p", { role: "status", className: "oms-manual-help" }, "Loading rule context…") : null,
        row?.error ? h("div", { role: "status" }, h("p", null, "Could not load this rule’s context."),
          h(Button, { variant: "secondary", onClick: documents.retry }, "Retry rule context")) : null,
        row?.document && !contexts.length ? h("p", { role: "status", className: "oms-manual-help" },
          "This rule is no longer in the current skill text. Refresh the inbox before deciding.") : null,
        ...contexts.map((context, index) => h("div", { key: index },
          h("p", { className: "oms-match-context__location" }, context.location),
          context.part.edit_text !== body && typeof match !== "string" ? h("div", { role: "status" },
            h("p", null, "The rule has changed since this match was suggested. The current wording is shown below; refresh the inbox before deciding."),
            h("blockquote", null, body)) : null,
          context.note ? h("p", { className: "oms-manual-help" }, context.note) : null,
          h("div", { className: "oms-match-context__rule" }, h("span", { className: "oms-match-context__label" }, "Matched rule"),
            h(Markdown, { body: textOf(context.part) || context.part.edit_text, inert: true })),
          context.section ? h("details", { className: "oms-match-context__section" },
            h("summary", null, "Show full section"),
            h("div", { role: "region", "aria-label": `Full section: ${context.sectionName}`, tabIndex: 0, className: "oms-match-context__full" },
              ...context.section.map((part, partIndex) => h("div", { key: partIndex, className: part.anchor === `rule:${id}` ? "oms-match-context__rule" : undefined },
                part.anchor === `rule:${id}` ? h("span", { className: "oms-match-context__label" }, "Matched rule") : null,
                h(Markdown, { body: textOf(part), inert: true }))))) : null)));
    }));
}
