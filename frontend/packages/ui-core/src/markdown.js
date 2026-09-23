import { createElement as h } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Previewing an unapproved upload must not load a tracking image or hide a
// destination behind a friendly link label. These overrides always win over
// custom document components when inert mode is selected.
const INERT_COMPONENTS = {
  img: ({ src, alt }) => h("span", { className: "oms-prose__inert gx-uqf__inert" },
    `[image not loaded: ${alt ? `${alt}, ` : ""}${typeof src === "string" ? src : "no source"}]`),
  a: ({ href, children }) => h("span", { className: "oms-prose__inert gx-uqf__inert" },
    children, href ? h("span", { className: "oms-prose__href gx-uqf__href" }, ` (${href})`) : null),
};

/** GFM tables, lists and code share one renderer and typography in every edition.
 * Raw HTML stays escaped and react-markdown's default URL sanitiser stays on.
 * Use inert mode when reviewing files that have not yet been approved.
 */
export function Markdown({ body, className = "", variant = "document", inert = false, renderImages = true, components, ...props }) {
  const renderers = inert
    ? { ...components, ...INERT_COMPONENTS }
    : renderImages ? components : { ...components, img: INERT_COMPONENTS.img };
  return h("div", { ...props, className: `oms-prose oms-prose--${variant} ${className}` },
    h(ReactMarkdown, {
      remarkPlugins: [remarkGfm],
      components: renderers,
    }, body));
}
