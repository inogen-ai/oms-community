import type { ReactNode } from "react";
/** GitHub's repository facts as the server sends them: dates are ISO strings. */
export interface SourceRepositoryFactsDisplay {
  full_name: string; description: string | null; stars: number; forks: number;
  archived: boolean; disabled: boolean; is_fork: boolean; licence: string | null;
  created_at: string; pushed_at: string | null; default_branch: string; fetched_at: string;
}
export interface SourceRepositoryDisplay {
  name: string; url: string; facts: SourceRepositoryFactsDisplay | null;
  concerns: readonly { code: string; label: string }[];
}
/** "updated 2 days ago", "updated just now"; empty for an unreadable date. */
export function sourceUpdatedAgo(iso: string, now?: Date): string;
/** The facts the repository line lists, for example ["120 stars", "MIT", "updated 2 days ago"]. */
export function sourceRepositoryFacts(facts: SourceRepositoryFactsDisplay | null | undefined, now?: Date): string[];
export function SourceRepositoryLine(props: { repository?: SourceRepositoryDisplay | null; now?: Date }): ReactNode;
