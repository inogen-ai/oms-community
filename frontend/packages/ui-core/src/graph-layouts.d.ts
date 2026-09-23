export interface LayoutOption { id: string; label: string; hint: string }
export const LAYOUTS: LayoutOption[];
export const DEFAULT_LAYOUT: string;
export function buildLayoutOptions(id: string): Record<string, unknown>;
