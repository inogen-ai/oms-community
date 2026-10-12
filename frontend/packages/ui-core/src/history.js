import { createElement as h } from "react";

export const IMPORT_CAUSE = "upload_import";
const CAUSES = {
  console_edit: "edited in the console", self_serve_edit: "edited in the portal",
  rule_edit: "reworded a rule", upload_import: "uploaded a package",
  import: "uploaded a package", compile: "compiled in a correction",
  review_decision: "made a review decision", restore: "restored an earlier version",
  source_install: "installed a source package", source_update: "applied a source update",
  source_undo: "undid a source update",
  created: "created this skill", unattributed: "changed outside the console",
};
const RULE_DETAILS = {
  "manual create": "created a rule", "manual reinforce": "reinforced a rule",
  retract: "retracted a rule", restore: "restored a rule",
  "local import review: block_revision accept": "applied a source text change",
  "local import review: block_revision reject": "kept the current source text",
  "local import review: removal accept": "retired a missing source rule",
  "local import review: removal reject": "kept a missing source rule",
  "local import review: injection accept": "approved a screened rule",
  "local import review: injection reject": "retired a screened rule",
  "local import review: polarity_conflict accept": "kept earlier imported guidance",
  "local import review: polarity_conflict reject": "retired earlier imported guidance",
};
const KINDS = { heading: ["heading", "headings"], rule: ["rule", "rules"],
  prose: ["text block", "text blocks"], examples: ["examples block", "examples blocks"],
  group: ["rule group", "rule groups"] };
const SINGLETONS = new Set(["name", "description", "title"]);

export function historySentence(entry, people = new Map(), baseline = false) {
  if (baseline && entry.cause === "unattributed") return "The version this history starts from";
  const cause = (entry.cause === "rule_edit" && Object.hasOwn(RULE_DETAILS, entry.detail) && RULE_DETAILS[entry.detail])
    || (Object.hasOwn(CAUSES, entry.cause) && CAUSES[entry.cause])
    || (entry.cause || "recorded a change").replaceAll("_", " ");
  const actor = entry.actor_person_id
    ? (people.get(entry.actor_person_id) || entry.actor_person_id) : null;
  const head = actor ? `${actor} ${cause}` : cause.charAt(0).toUpperCase() + cause.slice(1);
  const segments = Object.entries(entry.changed_kinds || {})
    .filter(([, count]) => Number.isFinite(count) && count > 0)
    .sort(([, a], [, b]) => b - a)
    .map(([kind, count]) => SINGLETONS.has(kind) ? `the ${kind}`
      : `${count} ${(KINDS[kind] || [kind, `${kind}s`])[count === 1 ? 0 : 1]}`);
  return segments.length ? `${head}, changing ${new Intl.ListFormat("en-GB", {
    style: "long", type: "conjunction",
  }).format(segments)}` : head;
}

export function formatHistoryWhen(iso) {
  if (!iso || !Number.isFinite(new Date(iso).getTime())) return "Time not recorded";
  const then = new Date(iso), now = new Date();
  const time = then.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
  const sameDay = then.getFullYear() === now.getFullYear() && then.getMonth() === now.getMonth()
    && then.getDate() === now.getDate();
  if (sameDay) return `${time} today`;
  const day = then.toLocaleDateString("en-GB", { day: "numeric", month: "short",
    ...(then.getFullYear() !== now.getFullYear() ? { year: "numeric" } : {}) });
  return `${day} ${time}`;
}

/** The same sentence, timestamp and known counts inside either edition's row. */
export function HistorySummary({ entry, people, baseline = false }) {
  const validTime = entry.at && Number.isFinite(new Date(entry.at).getTime());
  const counts = Number.isFinite(entry.added) && Number.isFinite(entry.removed);
  return h("span", { className: "oms-history__summary" },
    h("span", { className: "oms-history__sentence" }, historySentence(entry, people, baseline)),
    h("time", { className: "oms-history__when", dateTime: validTime ? entry.at : undefined,
      title: validTime ? new Date(entry.at).toLocaleString() : undefined }, formatHistoryWhen(entry.at)),
    !baseline && counts ? h("span", { className: "oms-history__counts" }, `+${entry.added} −${entry.removed}`) : null);
}

/** A readable local history. Supply proven fields only; snapshots and counts are optional. */
export function HistoryTimeline({ entries, people, emptyText = "No recorded changes yet." }) {
  if (!entries.length) return h("p", { className: "oms-history__empty" }, emptyText);
  return h("ol", { className: "oms-history__timeline" }, entries.map((entry) =>
    h("li", { key: entry.id }, h("details", null,
      h("summary", null, h(HistorySummary, { entry, people })),
      entry.detail ? h("p", { className: "oms-history__detail" }, entry.detail) : null,
      h("dl", { className: "oms-history__metadata" },
        h("div", null, h("dt", null, "Recorded by"), h("dd", null,
          entry.actor_person_id ? (people?.get(entry.actor_person_id) || entry.actor_person_id) : "Not recorded")),
        h("div", null, h("dt", null, "Version"), h("dd", null, entry.id)),
        entry.revision ? h("div", null, h("dt", null, "Revision"), h("dd", null, entry.revision)) : null,
        entry.source_operation_id ? h("div", null, h("dt", null, "Source operation"), h("dd", null, entry.source_operation_id)) : null,
        entry.source_origin_id ? h("div", null, h("dt", null, "Source origin"), h("dd", null, entry.source_origin_id)) : null,
        entry.source_revision ? h("div", null, h("dt", null, "Source revision"), h("dd", null, entry.source_revision)) : null)))));
}
