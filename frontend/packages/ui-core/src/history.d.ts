import type { ReactElement } from "react";
export interface HistoryEntry {
  id: string; at?: string; cause: string; actor_person_id?: string | null;
  detail?: string | null; revision?: string; added?: number; removed?: number;
  changed_kinds?: Record<string, number>;
}
export const IMPORT_CAUSE: string;
export function historySentence(entry: HistoryEntry, people?: Map<string, string>, baseline?: boolean): string;
export function formatHistoryWhen(iso?: string): string;
export function HistorySummary(props: { entry: HistoryEntry; people?: Map<string, string>; baseline?: boolean }): ReactElement;
export function HistoryTimeline(props: { entries: HistoryEntry[]; people?: Map<string, string>; emptyText?: string }): ReactElement;
