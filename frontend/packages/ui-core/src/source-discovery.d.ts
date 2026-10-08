import type { ReactNode } from "react";
import type { SourceRefDisplay } from "./source-status.js";
export interface SourcePackageDisplay {
  path: string; valid: boolean; upstream_name?: string | null; description?: string | null;
  file_count: number; total_bytes: number; reasons: readonly string[]; unsupported_metadata?: readonly string[];
}
export interface SourceDiscoveryDisplay {
  discovery_id: string; resolved_ref: SourceRefDisplay; preselected_path: string | null;
  packages: { items: readonly SourcePackageDisplay[]; next_cursor: string | null };
}
export interface SourceSelection { package_path: string; local_name: string; domain: string }
export function initialSourceSelections(discovery: SourceDiscoveryDisplay | null | undefined, options?: {
  defaultDomain?: string; existingNames?: readonly string[];
}): SourceSelection[];
export function SourceDiscovery(props: {
  discovery?: SourceDiscoveryDisplay | null; selections?: readonly SourceSelection[]; domains?: readonly string[];
  defaultDomain?: string; existingNames?: readonly string[]; loading?: boolean; busy?: boolean; error?: string; unavailable?: string;
  onSelectionsChange?: (selections: SourceSelection[]) => void;
  onInstall?: (body: { discovery_id: string; selections: readonly SourceSelection[] }) => void;
  onLoadMore?: (cursor: string) => void;
}): ReactNode;
