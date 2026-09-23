// Layout algorithms offered in the canvas layout picker. The Cola force
// simulation is the default; the rest are Cytoscape built-ins (no extra deps).
/** @typedef {{ id: string, label: string, hint: string }} LayoutOption */

/** @type {LayoutOption[]} */
export const LAYOUTS = [
  { id: "cola", label: "Force simulation", hint: "Constraint physics (Cola)" },
  { id: "fcose", label: "Force-directed", hint: "Organic clusters (fCoSE)" },
  { id: "cose-bilkent", label: "Organic (Bilkent)", hint: "Balanced spring model" },
  { id: "cose", label: "Organic", hint: "Spring model (CoSE)" },
  { id: "dagre", label: "Layered (Dagre)", hint: "Directed, ranked layers" },
  { id: "concentric", label: "Concentric", hint: "Rings by connectivity" },
  { id: "circle", label: "Circle", hint: "Single ring" },
  { id: "breadthfirst", label: "Hierarchy", hint: "Layered, top-down" },
  { id: "grid", label: "Grid", hint: "Uniform grid" },
];

export const DEFAULT_LAYOUT = "cola";

// Returns a Cytoscape layout-options object for the given algorithm id.
export function buildLayoutOptions(id) {
  const common = { animate: true, animationDuration: 480, fit: true, padding: 48 };
  switch (id) {
    case "fcose":
      return { name: "fcose", quality: "default", randomize: true, nodeSeparation: 90, idealEdgeLength: 110, ...common };
    case "cose-bilkent":
      return { name: "cose-bilkent", ...common, nodeRepulsion: 8000, idealEdgeLength: 110, gravity: 0.25, randomize: true };
    case "dagre":
      return { name: "dagre", ...common, rankDir: "TB", nodeSep: 36, rankSep: 64, edgeSep: 12 };
    case "cose":
      return { name: "cose", ...common, nodeRepulsion: 9000, idealEdgeLength: 110, nodeOverlap: 12 };
    case "concentric":
      return {
        name: "concentric", ...common, minNodeSpacing: 36,
        concentric: (n) => n.degree(),
        levelWidth: () => 3,
      };
    case "circle":
      return { name: "circle", ...common, spacingFactor: 1.25 };
    case "breadthfirst":
      return { name: "breadthfirst", ...common, directed: true, spacingFactor: 1.1, grid: true };
    case "grid":
      return { name: "grid", ...common, avoidOverlap: true, spacingFactor: 1.05 };
    case "cola":
    default:
      return { name: "cola", ...common, maxSimulationTime: 2200, nodeSpacing: 18, edgeLength: 120, avoidOverlap: true, randomize: false };
  }
}
