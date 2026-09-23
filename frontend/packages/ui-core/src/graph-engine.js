import cytoscape from "cytoscape";
import { labelColour, nodeIcon, shapeFor } from "./graph-node-types.js";

export const baseStyle = [
  {
    selector: "node",
    style: {
      "background-color": (n) => labelColour(n.data("label")),
      "background-opacity": 0.95,
      // The silhouette itself encodes the type; the glyph reinforces it.
      shape: (n) => shapeFor(n.data("label")),
      // Per-type glyph drawn on the node fill (dark stroke = inverted chip).
      "background-image": (n) => nodeIcon(n.data("label")),
      "background-width": "50%",
      "background-height": "50%",
      "background-position-x": "50%",
      "background-position-y": "50%",
      label: "data(caption)",
      color: "#c5cbd8",
      "font-family": "Inter Variable, Inter, sans-serif",
      "font-size": 9,
      "font-weight": 500,
      "text-wrap": "ellipsis",
      "text-max-width": "92px",
      "text-margin-y": 4,
      "text-valign": "bottom",
      "text-halign": "center",
      "text-outline-color": "#0a0b11",
      "text-outline-width": 2.4,
      width: 26,
      height: 26,
      "border-width": 1.5,
      "border-color": "rgba(255,255,255,0.18)",
      "border-opacity": 1,
      "transition-property": "border-width, border-color, width, height, background-opacity",
      "transition-duration": "120ms",
    },
  },
  {
    selector: "node:active",
    style: { "overlay-color": "#00dee8", "overlay-opacity": 0.18, "overlay-padding": 6 },
  },
  {
    selector: "edge",
    style: {
      width: 1.1,
      "line-color": "rgba(255,255,255,0.13)",
      "target-arrow-color": "rgba(255,255,255,0.28)",
      "target-arrow-shape": "triangle",
      "arrow-scale": 0.8,
      "curve-style": "bezier",
      label: "data(label)",
      "font-family": "ui-monospace, monospace",
      "font-size": 7,
      color: "rgba(255,255,255,0.32)",
      "text-rotation": "autorotate",
      "text-background-color": "#0a0b11",
      "text-background-opacity": 0.6,
      "text-background-padding": 1,
    },
  },
  {
    selector: "node:selected",
    style: {
      "border-width": 3,
      "border-color": "#00dee8",
      width: 30,
      height: 30,
      "overlay-color": "#00dee8",
      "overlay-opacity": 0.16,
      "overlay-padding": 8,
    },
  },
  {
    selector: "edge:selected",
    style: { "line-color": "#00dee8", "target-arrow-color": "#00dee8", width: 2 },
  },
  { selector: ".hidden", style: { display: "none" } },
];

export function createGraph(container, elements) {
  return cytoscape({
    container,
    elements,
    style: baseStyle,
    minZoom: 0.15,
    maxZoom: 3,
  });
}

/**
 * Animate the viewport to frame the elements not hidden by the client-side
 * filter (the `.hidden` class). Framing after a canvas replacement must not
 * rely on a layout plugin's own `fit` option: the offered layouts implement
 * it differently mid-animation, which left query results off-viewport on a
 * blank canvas. No-op when nothing is visible.
 */
export function fitToVisible(cy, padding = 48, duration = 320) {
  const eles = cy.elements().not(".hidden");
  if (eles.empty()) return;
  cy.animate({ fit: { eles, padding }, duration });
}

