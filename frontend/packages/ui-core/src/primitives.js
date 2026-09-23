import { createElement as h } from "react";

export function Panel({ title, children, className = "", ...props }) {
  return h("section", { ...props, className: `oms-panel ${className}` },
    title ? h("h2", null, title) : null, children);
}
export function Button({ variant = "primary", children, className = "", ...props }) {
  return h("button", { type: "button", ...props, className: `oms-button oms-button--${variant} ${className}` }, children);
}
export function Notice({ kind = "info", children }) {
  return h("div", { className: `oms-notice oms-notice--${kind}`, role: kind === "error" ? "alert" : "status" }, children);
}
export function EmptyState({ title, children }) {
  return h("div", { className: "oms-empty" }, h("h3", null, title), children ? h("p", null, children) : null);
}
export function Badge({ children }) {
  return h("span", { className: "oms-badge" }, children);
}
