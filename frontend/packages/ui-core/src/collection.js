"use client";

import { createElement as h, useState } from "react";
import { Button } from "./primitives.js";

/** Shared collection arithmetic: callers filter and order before paging. */
export function collectionPage(items, requestedPage, pageSize) {
  const pageCount = Math.max(1, Math.ceil(items.length / pageSize));
  const page = Math.min(pageCount, Math.max(1, requestedPage));
  const start = (page - 1) * pageSize;
  return { items: items.slice(start, start + pageSize), page, pageCount, total: items.length, start: items.length ? start + 1 : 0, end: Math.min(start + pageSize, items.length) };
}

export function CollectionPagination({ page, total, pageSize, onPageChange, label = "Items", singularLabel = label.replace(/s$/, ""), disabled = false }) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.min(pageCount, Math.max(1, page));
  const start = total ? (current - 1) * pageSize + 1 : 0;
  const end = Math.min(current * pageSize, total);
  const countLabel = (total === 1 ? singularLabel : label).toLowerCase();
  return h("div", { className: "oms-pagination", role: "group", "aria-label": `${label} pagination` },
    h("p", { className: "oms-pagination__count", "aria-live": "polite" }, total ? `${start}–${end} of ${total} ${countLabel}` : `0 ${countLabel}`),
    pageCount > 1 ? h("div", { className: "oms-pagination__controls" },
      h(Button, { variant: "secondary", disabled: disabled || current === 1, onClick: () => onPageChange(current - 1), "aria-label": `Previous ${label.toLowerCase()} page` }, "Previous"),
      h("label", null, h("span", { className: "oms-visually-hidden" }, `${label} page`),
        h("select", { value: current, disabled, onChange: (event) => onPageChange(Number(event.target.value)) },
          Array.from({ length: pageCount }, (_, index) => h("option", { key: index + 1, value: index + 1 }, `Page ${index + 1} of ${pageCount}`)))),
      h(Button, { variant: "secondary", disabled: disabled || current === pageCount, onClick: () => onPageChange(current + 1), "aria-label": `Next ${label.toLowerCase()} page` }, "Next")) : null);
}

export function CollectionViewToggle({ value, onChange, label = "Collection view" }) {
  return h("div", { className: "oms-collection-view", role: "group", "aria-label": label },
    h("button", { type: "button", "aria-pressed": value === "cards", onClick: () => onChange("cards") }, "Cards"),
    h("button", { type: "button", "aria-pressed": value === "list", onClick: () => onChange("list") }, "Compact list"));
}

/** A labelled, keyboard-operable list used for libraries and review queues. */
export function CollectionList({ items, selectedId, onSelect, label = "Items" }) {
  return h("ul", { className: "oms-collection-list", "aria-label": label }, items.map((item) =>
    h("li", { key: item.id }, h("button", { type: "button", className: "oms-collection-list__row", "aria-pressed": selectedId === undefined ? undefined : selectedId === item.id, onClick: () => onSelect(item.id) },
      h("span", { className: "oms-collection-list__content" },
        h("strong", { className: "oms-collection-list__title" }, item.title),
        item.description ? h("span", { className: "oms-collection-list__description" }, item.description) : null),
      item.meta ? h("span", { className: "oms-collection-list__meta" }, item.meta) : null),
      item.actions ? h("div", { className: "oms-collection-list__actions" }, item.actions) : null)));
}

/** Keep selection independent of search/page so hidden checked values survive. */
export function CollectionMultiSelect({ items, selectedIds, onChange, disabled = false, searchLabel = "Find options" }) {
  const [query, setQuery] = useState("");
  const [selectedOnly, setSelectedOnly] = useState(false);
  const [page, setPage] = useState(1);
  const filtered = [...items].sort((left, right) => left.name.localeCompare(right.name) || left.id.localeCompare(right.id)).filter((item) => (!selectedOnly || selectedIds.includes(item.id)) && item.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const result = collectionPage(filtered, page, 12);
  const sizeable = items.length > 12;
  return h("div", { className: "oms-multiselect" },
    sizeable ? h("div", { className: "oms-multiselect__filters" },
      h("label", null, searchLabel, h("input", { type: "search", value: query, disabled, onChange: (event) => { setQuery(event.target.value); setPage(1); } })),
      h("label", { className: "oms-multiselect__selected" }, h("input", { type: "checkbox", checked: selectedOnly, disabled, onChange: (event) => { setSelectedOnly(event.target.checked); setPage(1); } }), "Selected only")) : null,
    h("p", { className: "oms-manual-help", "aria-live": "polite" }, `${selectedIds.length} selected${sizeable ? " across all pages" : ""}`),
    h("div", { className: "oms-manual-targets" }, result.items.map((item) => h("label", { key: item.id },
      h("input", { type: "checkbox", checked: selectedIds.includes(item.id), disabled,
        onChange: (event) => onChange(event.target.checked ? [...selectedIds, item.id] : selectedIds.filter((id) => id !== item.id)) }), item.name))),
    !filtered.length && items.length ? h("p", { className: "oms-manual-help" }, "No matching target skills.") : null,
    sizeable ? h(CollectionPagination, { page: result.page, total: result.total, pageSize: 12, label: "Target skills", disabled, onPageChange: setPage }) : null,
    query || selectedOnly ? h(Button, { variant: "secondary", disabled, onClick: () => { setQuery(""); setSelectedOnly(false); setPage(1); } }, "Clear target filters") : null);
}
