import { createElement as h } from "react";
import { Badge } from "./primitives.js";

const MINUTE = 60_000, HOUR = 3_600_000, DAY = 86_400_000;

export function sourceUpdatedAgo(iso, now = new Date()) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const elapsed = Math.max(0, now.getTime() - then);
  const ago = (count, unit) => `updated ${count} ${unit}${count === 1 ? "" : "s"} ago`;
  if (elapsed < MINUTE) return "updated just now";
  if (elapsed < HOUR) return ago(Math.floor(elapsed / MINUTE), "minute");
  if (elapsed < DAY) return ago(Math.floor(elapsed / HOUR), "hour");
  const days = Math.floor(elapsed / DAY);
  if (days < 30) return ago(days, "day");
  if (days < 365) return ago(Math.floor(days / 30), "month");
  return ago(Math.floor(days / 365), "year");
}

export function sourceRepositoryFacts(facts, now = new Date()) {
  if (!facts) return [];
  const stars = `${facts.stars.toLocaleString("en-GB")} ${facts.stars === 1 ? "star" : "stars"}`;
  // A missing licence is a concern chip, so only a named one is listed here (ruling R10).
  const licence = facts.licence === "NOASSERTION" ? "Licence not recognised" : facts.licence;
  return [stars, licence, facts.pushed_at ? sourceUpdatedAgo(facts.pushed_at, now) : null].filter(Boolean);
}

export function SourceRepositoryLine({ repository, now = new Date() }) {
  if (!repository) return null;
  const facts = sourceRepositoryFacts(repository.facts, now);
  return h("div", { className: "oms-source-repository" },
    h("a", { href: repository.url, target: "_blank", rel: "noreferrer" }, repository.name),
    facts.map(fact => h("span", { key: fact, className: "oms-source-fact" }, fact)),
    repository.facts ? null : h("span", { className: "oms-source-fact" }, "Repository facts unavailable"),
    repository.concerns?.length ? h("div", { className: "oms-source-concerns", "aria-label": "Repository concerns" },
      repository.concerns.map(concern => h(Badge, { key: concern.code }, concern.label))) : null);
}
