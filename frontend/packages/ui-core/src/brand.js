import { createElement as h } from "react";

/** The same product mark in the console, portal and Community edition. */
export function ProductMark({ title, logoSrc, className = "" } = {}) {
  if (logoSrc) return h("span", { className: `oms-product oms-product--image ${className}` },
    h("img", { className: "oms-product__logo", src: logoSrc, alt: "OMS", width: 1536, height: 1024 }),
    title ? h("b", null, title) : null,
    h("span", { className: "oms-product__sub" }, "Organisational Memory"));
  return h("span", { className: `oms-product ${className}` },
    h("span", { className: "oms-product__mark gx-brand__mark", "aria-hidden": true }),
    h("span", { className: "oms-product__name gx-brand__name" },
      "OMS", title ? h("b", null, ` ${title}`) : null,
      h("span", { className: "oms-product__sub gx-brand__sub" }, "Organisational Memory")));
}

/** Supply the packaged logo's built URL; the component has no bundler dependency. */
export function BrandMark({ logoSrc = "/inogen_logo_darkmode.png", className = "" } = {}) {
  return h("a", {
    className: `oms-brand ${className}`,
    href: "https://www.inogen.ai",
    target: "_blank",
    rel: "noreferrer noopener",
  },
  h("img", {
    className: "oms-brand__logo gx-brand__logo",
    src: logoSrc,
    alt: "InoGen AI",
    width: 96,
    height: 24,
  }),
  h("span", { className: "oms-brand__lines gx-brand__lines" },
    h("span", { className: "oms-brand__line gx-brand__line" }, "By InoGen AI"),
    h("span", { className: "oms-brand__line oms-brand__line--url gx-brand__line gx-brand__line--url" }, "www.inogen.ai")));
}
