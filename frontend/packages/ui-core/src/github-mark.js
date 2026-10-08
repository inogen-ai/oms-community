import { createElement as h } from "react";

// GitHub's official Invertocat, unmodified. Both editions are dark-only, so the white file is used;
// the black file ships beside it for a future light theme. A URL reference is emitted as a static
// file by the bundlers and resolves to a file: URL under Node.
const WHITE = new URL("./assets/GitHub_Invertocat_White.svg", import.meta.url).href;

/** Decorative without a title; with one it is the accessible name and tooltip. Padding is GitHub's clear space. */
export function GitHubMark({ title, size = 16 }) {
  const label = title ? { alt: title, title } : { alt: "", "aria-hidden": "true" };
  return h("img", { className: "oms-github-mark", src: WHITE, width: Math.round(size * 98 / 96), height: size,
    style: { padding: Math.round(size / 4) }, ...label });
}
