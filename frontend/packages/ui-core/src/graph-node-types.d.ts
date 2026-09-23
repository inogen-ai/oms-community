export interface NodeType { colour: string; shape: string; glyph: string }
export const NODE_TYPES: Record<string, NodeType>;
export const NODE_LABELS: string[];
export const DEFAULT_LABEL_COLOUR: string;
export const DEFAULT_SHAPE: string;
export function labelColour(label: string): string;
export function shapeFor(label: string): string;
export function nodeIcon(label: string, stroke?: string): string;
