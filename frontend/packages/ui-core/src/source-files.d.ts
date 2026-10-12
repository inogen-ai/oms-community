import type { ReactNode } from "react";
export type SourceFileSide = "base" | "local" | "upstream";
export interface SourceFileSidePreview {
  text?: string | null; size?: number; digest?: string; binary?: boolean; truncated?: boolean;
  loading?: boolean; error?: string; unavailable?: string;
}
/** A flagged line of the GitHub side and its plain tag ("looks like a phone number"), never the matched text. */
export interface SourceFlaggedLine { line: number; label: string }
type SourceEvidenceKind = { kind: "known" | "absent" | "unknown" };
export function boundedSourceText(value: unknown): { text: string; truncated: boolean };
/**
 * The sides a comparison has no copy of: "base" when no version was taken from GitHub before, "local" when
 * OMS does not have the part (always on a held install), "upstream" when GitHub removed it.
 */
export function sourceAbsentSides(change: { base: SourceEvidenceKind; local: SourceEvidenceKind; incoming: SourceEvidenceKind }, heldInstall?: boolean): SourceFileSide[];
/** The preview's new scrollTop that puts `line` up to two lines (at most a quarter of the preview) below its top edge. */
export function sourceLineScrollTop(container: { top: number; height: number }, line: { top: number; height: number }, scrollTop: number): number;
/**
 * `absent` sides show "Not present" and no Download button; with both "base" and "local" absent only the incoming
 * side shows, as a new file. The incoming side shows line numbers as the server counts them and marks each of
 * `flagged`. `origin` (default "github") names the columns: "local" is a re-imported package, never GitHub.
 */
export function SourceFileComparison(props: {
  path: string; base?: SourceFileSidePreview | null; local?: SourceFileSidePreview | null; upstream?: SourceFileSidePreview | null;
  absent?: readonly SourceFileSide[]; flagged?: readonly SourceFlaggedLine[]; origin?: "github" | "local";
  onDownload?: (side: SourceFileSide, path: string) => void; publicationWarning?: string; busy?: boolean;
}): ReactNode;
