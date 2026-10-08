import { createElement as h } from "react";
import { Button, EmptyState, Notice, Panel } from "./primitives.js";
import { SourceStatus } from "./source-status.js";

// Match oms.domain.ids.slug so display-name differences cannot hide an ID collision.
const skillId = name => name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

function selectionFor(pkg, selected, existingNames, domain) {
  const used = new Set([...existingNames, ...selected.map(row => row.local_name)].map(skillId));
  const stem = (pkg.upstream_name || pkg.path.split("/").at(-1) || "Skill").slice(0, 190);
  let name = stem, suffix = 2;
  while (used.has(skillId(name))) name = `${stem} ${suffix++}`;
  return { package_path: pkg.path, local_name: name, domain };
}
export function initialSourceSelections(discovery, { defaultDomain = "", existingNames = [] } = {}) {
  const pkg = discovery?.packages.items.find(row => row.valid && row.path === discovery.preselected_path);
  return pkg ? [selectionFor(pkg, [], existingNames, defaultDomain)] : [];
}

export function SourceDiscovery({ discovery, selections, onSelectionsChange, onInstall, domains = [], existingNames = [],
  defaultDomain = "", loading = false, busy = false, error, unavailable, onLoadMore }) {
  if (loading) return h(Notice, null, "Discovering skill packages…");
  if (unavailable) return h(Notice, null, unavailable);
  if (error && !discovery) return h(Notice, { kind: "error" }, error);
  if (!discovery) return h(EmptyState, { title: "No discovery yet" }, "Choose a repository or explicit skill folder to discover packages.");
  const packages = discovery.packages.items;
  if (!packages.length) return h(EmptyState, { title: "No skill packages found" }, "No packages were found in this discovery scope.");
  const selected = selections ?? initialSourceSelections(discovery, { defaultDomain, existingNames });
  const disabled = busy || !!error || !onSelectionsChange;
  const known = new Map(packages.map(pkg => [pkg.path, pkg]));
  const used = new Set(existingNames.map(skillId));
  const seenPaths = new Set();
  const invalid = selected.some(row => {
    const name = skillId(row.local_name);
    const duplicate = used.has(name) || seenPaths.has(row.package_path);
    used.add(name); seenPaths.add(row.package_path);
    return duplicate || !known.get(row.package_path)?.valid || !name || name.startsWith("repo-") || row.local_name.length > 200
      || !row.domain.trim() || row.domain.length > 200 || (domains.length > 0 && !domains.includes(row.domain));
  });
  const change = next => { if (!disabled) onSelectionsChange(next); };
  const choose = (pkg, checked) => { if (checked && selected.length >= 100) return; change(checked
    ? [...selected, selectionFor(pkg, selected, existingNames, defaultDomain)]
    : selected.filter(row => row.package_path !== pkg.path)); };
  const edit = (path, field, value) => change(selected.map(row => row.package_path === path ? { ...row, [field]: value } : row));
  const atLimit = selected.length >= 100;
  const overLimit = selected.length > 100;
  const cannotInstall = disabled || invalid || overLimit || !selected.length || !onInstall;
  return h(Panel, { title: "Discovered skill packages", className: "oms-source-discovery", "aria-busy": busy },
    h(SourceStatus, { status: "complete", ref: discovery.resolved_ref }),
    h("p", null, "Select packages to install. Local names must produce unique skill IDs; each selected package needs a domain."),
    h("p", { role: "status" }, `${selected.length} of 100 packages selected. Install at most 100 packages per request.`),
    overLimit ? h(Notice, { kind: "error" }, "Select at most 100 packages before installing. Remove packages or clear the selection.") : null,
    error ? h(Notice, { kind: "error" }, error) : null,
    h("div", { className: "oms-source-actions" }, h(Button, { variant: "secondary", disabled: disabled || atLimit, onClick: () => {
      const next = [...selected];
      for (const pkg of packages) if (next.length < 100 && pkg.valid && !next.some(row => row.package_path === pkg.path)) next.push(selectionFor(pkg, next, existingNames, defaultDomain));
      change(next);
    } }, "Select all shown"), h(Button, { variant: "secondary", disabled: disabled || !selected.length, onClick: () => change([]) }, "Clear selection")),
    h("ul", { className: "oms-source-packages" }, packages.map(pkg => {
      const row = selected.find(item => item.package_path === pkg.path);
      const display = pkg.path || "repository root";
      return h("li", { key: pkg.path },
        h("label", { className: "oms-source-check" }, h("input", { type: "checkbox", checked: !!row, disabled: disabled || !pkg.valid || (!row && atLimit),
          "aria-label": `Select ${display}`, onChange: event => { if (pkg.valid) choose(pkg, event.target.checked); } }), h("strong", null, display)),
        pkg.description ? h("p", null, pkg.description) : null,
        h("p", null, `${pkg.file_count} files · ${pkg.total_bytes} bytes`),
        !pkg.valid ? h(Notice, { kind: "error" }, `Cannot install: ${pkg.reasons.join("; ") || "Invalid package"}`) : null,
        pkg.unsupported_metadata?.length ? h("p", null, `Unsupported metadata retained as evidence: ${pkg.unsupported_metadata.join(", ")}`) : null,
        row ? h("div", { className: "oms-source-fields" },
          h("label", null, "Local name", h("input", { value: row.local_name, maxLength: 200, disabled, "aria-label": `Local name for ${display}`, onChange: event => edit(pkg.path, "local_name", event.target.value) }), h("small", null, `Skill ID: ${skillId(row.local_name) || "No usable ID"}`)),
          h("label", null, "Domain", domains.length ? h("select", { value: row.domain, disabled, "aria-label": `Domain for ${display}`, onChange: event => edit(pkg.path, "domain", event.target.value) },
            h("option", { value: "" }, "Choose a domain"), domains.map(domain => h("option", { key: domain, value: domain }, domain)))
            : h("input", { value: row.domain, maxLength: 200, disabled, "aria-label": `Domain for ${display}`, onChange: event => edit(pkg.path, "domain", event.target.value) }))) : null);
    })),
    invalid ? h(Notice, { kind: "error" }, "Give every selected package a unique local name and an available domain. Names must produce a non-empty skill ID and cannot use the reserved repo- prefix.") : null,
    h("div", { className: "oms-source-actions" },
      discovery.packages.next_cursor ? h(Button, { variant: "secondary", disabled: busy || !onLoadMore, onClick: () => { if (!busy) onLoadMore?.(discovery.packages.next_cursor); } }, "Load more packages") : null,
      h(Button, { disabled: cannotInstall, onClick: () => { if (!cannotInstall) onInstall({ discovery_id: discovery.discovery_id, selections: selected }); } }, "Install selected skills")));
}
