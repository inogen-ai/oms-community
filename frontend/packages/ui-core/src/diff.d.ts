import type { HTMLAttributes, ReactNode } from "react";

export type DiffKind = "same" | "add" | "del";
export interface DiffLine { kind: DiffKind; text: string }
export interface HistoryDiffRow { state: string; old_text: string | null; new_text: string | null }

export function lineDiff(before: string, after: string): DiffLine[];
/** Word and punctuation changes, preserving whitespace and source text. */
export function inlineDiff(before: string, after: string): DiffLine[];
/** Current → saved: additions are what restoring adds, deletions what it removes. */
export function historyDiffLines(row: HistoryDiffRow): DiffLine[];
export function DiffLines(props: HTMLAttributes<HTMLDivElement> & { lines: DiffLine[] }): ReactNode;
