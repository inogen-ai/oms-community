"use client";

import { createElement as h, useEffect, useId, useMemo, useRef, useState } from "react";
import { createGraph, fitToVisible } from "./graph-engine.js";
import { buildLayoutOptions, LAYOUTS } from "./graph-layouts.js";
import { labelColour, nodeIcon } from "./graph-node-types.js";
import { Button, EmptyState, Notice } from "./primitives.js";

const EMPTY_EDGES = [];
// These are Cytoscape's built-ins. Editions can register additional algorithms
// without adding their extensions to the basic explorer's download.
const EXPLORER_LAYOUTS = LAYOUTS.filter(({ id }) => ["cose", "breadthfirst", "circle", "grid"].includes(id));

/** Adapt endpoint-neutral graph data to the shared canvas vocabulary. */
export function graphElements(nodes, edges = EMPTY_EDGES) {
  const ids = new Set();
  const elements = [];
  for (const node of nodes) {
    if (!node.id || ids.has(node.id)) continue;
    ids.add(node.id);
    elements.push({ data: { id: node.id, label: node.label, caption: node.caption } });
  }
  const nodeIds = new Set(ids);
  for (const [index, edge] of edges.entries()) {
    // Search responses can contain only part of a graph. A dangling relation
    // must not make Cytoscape reject all the nodes that can be displayed.
    if (!nodeIds.has(edge.source) || !nodeIds.has(edge.target)) continue;
    let id = edge.id || `oms-edge:${edge.source}:${edge.type || "RELATED_TO"}:${edge.target}`;
    while (ids.has(id)) id = `edge:${id}`;
    ids.add(id);
    elements.push({ data: { id, source: edge.source, target: edge.target, label: edge.type || "RELATED_TO" } });
  }
  return elements;
}

export function GraphExplorer({ nodes, edges = EMPTY_EDGES, selectedId = null, onSelectNode, onExpandNode, className = "" }) {
  const container = useRef(null);
  const canvas = useRef(null);
  const select = useRef(onSelectNode);
  select.current = onSelectNode;
  const expand = useRef(onExpandNode);
  expand.current = onExpandNode;
  const previousLayout = useRef(null);
  const descriptionId = useId();
  const [layout, setLayout] = useState("cose");
  const [labelsVisible, setLabelsVisible] = useState(true);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const elements = useMemo(() => graphElements(nodes, edges), [nodes, edges]);
  const labels = useMemo(() => [...new Set(nodes.map((node) => node.label))].sort(), [nodes]);
  const nodeCount = elements.filter((element) => element.data.source === undefined).length;
  const edgeCount = elements.length - nodeCount;

  useEffect(() => {
    if (!container.current) return;
    let cy;
    let observer;
    const resize = () => cy?.resize();
    try {
      cy = createGraph(container.current, []);
      canvas.current = cy;
      cy.boxSelectionEnabled(false);
      let lastTap = { id: null, at: 0 };
      cy.on("tap", "node", (event) => {
        const id = event.target.id(), at = Date.now();
        select.current?.(id);
        if (lastTap.id === id && at - lastTap.at < 350) { expand.current?.(id); lastTap = { id: null, at: 0 }; }
        else lastTap = { id, at };
      });
      cy.on("tap", (event) => { if (event.target === cy) select.current?.(null); });
      if (typeof ResizeObserver !== "undefined") {
        observer = new ResizeObserver(resize);
        observer.observe(container.current);
      } else {
        window.addEventListener("resize", resize);
      }
    } catch {
      setError("The graph canvas could not be opened. You can still inspect nodes using the selector below.");
    }
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", resize);
      cy?.destroy();
      canvas.current = null;
    };
  }, []);

  useEffect(() => {
    const cy = canvas.current;
    if (!cy) return;
    try {
      const wanted = new Set(elements.map(element => element.data.id));
      const retained = cy.nodes().filter(node => wanted.has(node.id()));
      const arrange = !retained.length || previousLayout.current !== layout;
      const addedNodes = [];
      cy.batch(() => {
        cy.elements().filter(element => !wanted.has(element.id())).remove();
        for (const element of elements) {
          const existing = cy.getElementById(element.data.id);
          if (existing.nonempty()) existing.data(element.data);
          else {
            const added = cy.add(element);
            if (added.isNode()) addedNodes.push(added);
            if (added.isNode() && !arrange) {
              const seed = cy.getElementById(selectedId || retained[0]?.id());
              const centre = seed.nonempty() ? seed.position() : { x: 0, y: 0 };
              const angle = cy.nodes().length * 2.39996;
              added.position({ x: centre.x + 100 * Math.cos(angle), y: centre.y + 100 * Math.sin(angle) });
            }
          }
        }
        cy.edges().unselectify();
      });
      if (cy.nodes().nonempty() && arrange) {
        // Initial data and a new layout settle before framing the viewport.
        // This avoids partial animations leaving a small graph off the canvas.
        cy.layout({ ...buildLayoutOptions(layout), animate: false }).run();
        cy.fit(cy.elements(), 48);
      } else if (addedNodes.some(node => {
        const box = node.renderedBoundingBox();
        return box.x1 < 24 || box.y1 < 24 || box.x2 > cy.width() - 24 || box.y2 > cy.height() - 24;
      })) {
        // Expanding a tightly framed seed must reveal the new connections.
        // Change only the viewport, preserving the user's node arrangement.
        cy.fit(cy.elements(), 48);
      }
      previousLayout.current = layout;
      setError("");
      setReady(true);
    } catch {
      setReady(false);
      setError("The graph could not be drawn. You can still inspect nodes using the selector below.");
    }
  }, [elements, layout]);

  useEffect(() => {
    const cy = canvas.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().unselect();
      if (selectedId) {
        const node = cy.getElementById(selectedId);
        node.select();
        if (node.nonempty()) {
          const position = node.renderedPosition();
          if (position.x < 30 || position.x > cy.width() - 30 || position.y < 30 || position.y > cy.height() - 30) cy.center(node);
        }
      }
      cy.elements().style("text-opacity", labelsVisible ? 1 : 0);
    });
  }, [selectedId, labelsVisible, elements, layout]);

  function zoom(factor) {
    const cy = canvas.current;
    if (!cy) return;
    cy.zoom({
      level: Math.min(cy.maxZoom(), Math.max(cy.minZoom(), cy.zoom() * factor)),
      renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 },
    });
  }

  return h("div", { className: `oms-graph ${className}` },
    h("div", { className: "oms-graph__toolbar", role: "group", "aria-label": "Graph controls" },
      h("span", { className: "oms-graph__counts", "aria-live": "polite" }, `${nodeCount} ${nodeCount === 1 ? "node" : "nodes"} · ${edgeCount} ${edgeCount === 1 ? "connection" : "connections"}`),
      h("label", { className: "oms-graph__layout" }, "Layout",
        h("select", { value: layout, onChange: (event) => setLayout(event.target.value) },
          EXPLORER_LAYOUTS.map(({ id, label }) => h("option", { key: id, value: id }, label)))),
      h("label", { className: "oms-graph__labels" },
        h("input", { type: "checkbox", checked: labelsVisible, onChange: (event) => setLabelsVisible(event.target.checked) }), "Labels"),
      h("div", { className: "oms-graph__viewport" },
        h(Button, { variant: "secondary", onClick: () => zoom(1 / 1.25), "aria-label": "Zoom out", disabled: !nodeCount || !!error }, "−"),
        h(Button, { variant: "secondary", onClick: () => zoom(1.25), "aria-label": "Zoom in", disabled: !nodeCount || !!error }, "+"),
        h(Button, { variant: "secondary", onClick: () => { if (canvas.current) fitToVisible(canvas.current, 48, window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 180); }, disabled: !nodeCount || !!error }, "Fit graph"))),
    error ? h(Notice, { kind: "error" }, error) : null,
    h("div", { className: "oms-graph__stage" },
      h("div", {
        ref: container, className: "oms-graph__canvas", "data-testid": "graph-canvas",
        "data-node-count": nodeCount, "data-edge-count": edgeCount, "data-ready": ready,
        role: "img", "aria-label": `Knowledge graph with ${nodeCount} nodes and ${edgeCount} connections`,
        "aria-describedby": descriptionId,
      }),
      !nodeCount ? h("div", { className: "oms-graph__empty" }, h(EmptyState, { title: "No nodes to display" }, "Import skills or try a different search.")) : null),
    h("div", { className: "oms-graph__footer" },
      h("div", { className: "oms-graph__legend", "aria-label": "Node types" }, labels.map((label) =>
        h("span", { key: label, className: "oms-graph__legend-item" },
          h("i", { style: { backgroundColor: labelColour(label) }, "aria-hidden": "true" }, h("img", { src: nodeIcon(label), alt: "" })), label))),
      h("p", { id: descriptionId, className: "oms-graph__hint" }, `Drag nodes to arrange them. Scroll to zoom. ${onExpandNode ? "Double-click a node to load its connections. " : ""}Select a node on the graph or use the selector below.`)),
    h("label", { className: "oms-graph__node-picker" }, "Inspect a node",
      h("select", { value: selectedId || "", onChange: (event) => select.current?.(event.target.value || null) },
        h("option", { value: "" }, "Choose a node…"),
        nodes.map((node) => h("option", { key: node.id, value: node.id }, `${node.caption} (${node.label})`)))));
}
