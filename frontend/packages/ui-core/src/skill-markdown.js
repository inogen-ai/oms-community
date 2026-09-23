import { createElement as h, Fragment } from "react";
import { splitFrontmatter } from "./frontmatter.js";
import { Markdown } from "./markdown.js";

/** The same readable skill metadata in both editions. */
export function Frontmatter({ fields }) {
  if (!fields.length) return null;
  return h("dl", { className: "oms-frontmatter gx-frontmatter", "data-testid": "frontmatter" },
    fields.map((field, index) => h("div", {
      key: `${field.key}-${index}`, className: "oms-frontmatter__row gx-frontmatter__row",
    },
    h("dt", { className: "oms-frontmatter__key gx-frontmatter__key" }, field.key),
    h("dd", { className: "oms-frontmatter__value gx-frontmatter__value" }, field.value))));
}

/** Render a complete SKILL.md; callers retain the original bytes for source views. */
export function SkillMarkdown({ body, ...props }) {
  const document = splitFrontmatter(body);
  return h(Fragment, null,
    h(Frontmatter, { fields: document.fields }),
    h(Markdown, { ...props, body: document.body }));
}
