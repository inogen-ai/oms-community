import type { ReactNode } from "react";
import type { SourceCardUpdate } from "./source-card.js";
export interface SourceRefDisplay { kind: "branch" | "tag" | "commit"; name: string; commit?: string }
export interface SourceOutcomeDisplay { skill_id: string; state: string; code?: string | null; update_id?: string | null; detail?: string | null }
export interface SourceOperationDisplay {
  operation_id: string; state: string; committed: boolean;
  outcomes?: readonly SourceOutcomeDisplay[];
}
export const sourceFlagLabels: Readonly<Record<string, string>>;
export function SourceStatus(props: {
  status?: string | { state: string; code?: string | null }; message?: string; flags?: readonly string[]; retryNeeded?: boolean;
  firstReconciliation?: boolean; ref?: SourceRefDisplay; operation?: SourceOperationDisplay | null;
  exclusions?: readonly { skill_id: string; reasons: readonly string[] }[];
  /** Where an edition links one outcome (for example its held skill review card); return null for no link. */
  outcomeLink?: (outcome: SourceOutcomeDisplay) => { href: string; label: string } | null;
  /** "install" shows the install counts ("2 installed, 1 held for review, 0 failed"); only install screens pass it. */
  summary?: "install";
}): ReactNode;
export function sourceBulkExclusions(update: SourceCardUpdate): string[];
export interface SourceUndoGuard {
  undo_id: string; policy_version: string;
  expected_generations: readonly { skill_id: string; content: number; binding: number }[];
}
export function SourceUndo(props: {
  guard?: SourceUndoGuard | null; eligible?: boolean; reason?: string; confirmed?: boolean; busy?: boolean;
  onConfirmedChange?: (confirmed: boolean) => void; onUndo?: (guard: SourceUndoGuard) => void;
}): ReactNode;
