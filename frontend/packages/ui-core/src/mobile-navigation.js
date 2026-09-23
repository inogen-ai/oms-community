"use client";

import { createElement as h, useEffect, useId, useRef, useState } from "react";
import { Menu, X } from "lucide-react";

/** Labelled destination navigation; each application supplies its own routes. */
export function MobileNavigation({
  items, current, onSelect, linkComponent = "a", label = "Sections",
  navigationLabel = "All destinations", wideQuery = "(min-width: 761px)",
  className = "",
}) {
  const [openedFor, setOpenedFor] = useState(null);
  const activeId = current ?? "";
  const open = openedFor === activeId;
  const root = useRef(null);
  const trigger = useRef(null);
  const panel = useRef(null);
  const panelId = useId();
  const currentLabel = items.find((item) => item.id === current)?.label ?? label;
  const waiting = items.reduce((total, item) => total + (item.id === current ? 0 : (item.count ?? 0)), 0);

  useEffect(() => {
    if (!open) return;
    const first = panel.current?.querySelector('[aria-current="page"]')
      ?? panel.current?.querySelector("a, button");
    first?.focus();
    const escape = (event) => {
      if (event.key !== "Escape") return;
      setOpenedFor(null);
      trigger.current?.focus();
    };
    const outside = (event) => {
      if (event.target && !root.current?.contains(event.target)) setOpenedFor(null);
    };
    document.addEventListener("keydown", escape);
    document.addEventListener("pointerdown", outside);
    const wide = typeof window.matchMedia === "function" ? window.matchMedia(wideQuery) : null;
    const resized = () => { if (wide?.matches) setOpenedFor(null); };
    resized();
    wide?.addEventListener("change", resized);
    return () => {
      document.removeEventListener("keydown", escape);
      document.removeEventListener("pointerdown", outside);
      wide?.removeEventListener("change", resized);
    };
  }, [open, wideQuery]);

  if (!items.length) return null;
  const choose = (id) => {
    onSelect?.(id);
    setOpenedFor(null);
    trigger.current?.focus();
  };
  return h("div", {
    className: `oms-mobile-nav ${className}`, ref: root,
    onBlur(event) {
      // Safari can blur a trigger to nothing before a link's click arrives.
      // Only a real focus destination outside the menu dismisses it here.
      if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) setOpenedFor(null);
    },
  },
  h("button", {
    type: "button", className: "oms-mobile-nav__trigger", ref: trigger,
    "aria-expanded": open, "aria-controls": panelId,
    "aria-label": `${label}, currently ${currentLabel}${waiting ? `, ${waiting} waiting in other sections` : ""}`,
    onClick: () => setOpenedFor(open ? null : activeId),
  },
  h(open ? X : Menu, { size: 16, "aria-hidden": true }),
  !open && waiting > 0 ? h("span", { className: "oms-mobile-nav__badge" }, waiting) : null,
  h("span", { className: "oms-mobile-nav__label" }, currentLabel)),
  open ? h("nav", {
    id: panelId, className: "oms-mobile-nav__panel", "aria-label": navigationLabel, ref: panel,
    onKeyDown(event) {
      const destinations = Array.from(panel.current?.querySelectorAll("a, button") ?? []);
      const index = destinations.indexOf(document.activeElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? destinations.length - 1
        : event.key === "ArrowDown" ? (index + 1) % destinations.length
          : event.key === "ArrowUp" ? (index - 1 + destinations.length) % destinations.length : null;
      if (next === null) return;
      event.preventDefault();
      destinations[next]?.focus();
    },
  }, items.map((item) => h(item.href ? linkComponent : "button", {
    key: item.id, ...(item.href ? { href: item.href } : { type: "button" }),
    className: "oms-mobile-nav__item", "aria-current": item.id === current ? "page" : undefined,
    onClick: () => choose(item.id),
  }, h("span", { className: "oms-mobile-nav__item-label" }, item.label),
  item.count ? h("span", { className: "oms-mobile-nav__badge" }, item.count) : null))) : null);
}
