import type { ReactNode } from "react";
export type SourceFileSide = "base" | "local" | "upstream";
export interface SourceFileSidePreview {
  text?: string | null; size?: number; digest?: string; binary?: boolean; truncated?: boolean;
  loading?: boolean; error?: string; unavailable?: string;
}
export function boundedSourceText(value: unknown): { text: string; truncated: boolean };
export function SourceFileComparison(props: {
  path: string; base?: SourceFileSidePreview | null; local?: SourceFileSidePreview | null; upstream?: SourceFileSidePreview | null;
  onDownload?: (side: SourceFileSide, path: string) => void; publicationWarning?: string; busy?: boolean;
}): ReactNode;
