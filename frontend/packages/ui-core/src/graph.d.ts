import type { Core, ElementDefinition, StylesheetStyle } from "cytoscape";
import type { ReactNode } from "react";

export interface ExplorerNode { id: string; label: string; caption: string }
export interface ExplorerEdge { id?: string; source: string; target: string; type?: string }

export const baseStyle: StylesheetStyle[];
export function createGraph(container: HTMLElement, elements: ElementDefinition[]): Core;
export function fitToVisible(cy: Core, padding?: number, duration?: number): void;
export function graphElements(nodes: ExplorerNode[], edges: ExplorerEdge[]): ElementDefinition[];
export function GraphExplorer(props: {
  nodes: ExplorerNode[];
  edges?: ExplorerEdge[];
  selectedId?: string | null;
  onSelectNode?: (id: string | null) => void;
  onExpandNode?: (id: string) => void;
  className?: string;
}): ReactNode;
export { NODE_LABELS, labelColour, nodeIcon, shapeFor } from "./graph-node-types.js";
