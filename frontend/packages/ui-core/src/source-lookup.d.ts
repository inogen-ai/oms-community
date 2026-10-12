import type { ReactNode } from "react";
export type SourceRefChoice = "" | "branch" | "tag" | "commit";
export interface SourceLookupValue { url: string; kind: SourceRefChoice; name: string; path: string; profile: string }
export interface SourceLookupBody {
  url: string; ref_kind?: "branch" | "tag" | "commit"; ref_name?: string; package_path?: string; credential_profile_id?: string;
}
export function sourceLookupValue(initialUrl?: string): SourceLookupValue;
export function sourceLookupBody(value: SourceLookupValue): SourceLookupBody;
export type SourceProfileChoice = { mode: "text" } | { mode: "select"; profiles: readonly string[] } | { mode: "none" };
export function SourceLookupForm(props: {
  value: SourceLookupValue; onChange: (value: SourceLookupValue) => void; onSubmit: (body: SourceLookupBody) => void;
  profile?: SourceProfileChoice; busy?: boolean; error?: string; submitLabel?: string; children?: ReactNode;
}): ReactNode;
export type SourcePendingSubject = "import" | "request";
export function sourcePendingSubject(path: string): SourcePendingSubject;
export function SourcePendingNotice(props: { subject?: SourcePendingSubject; onCheck: () => void; busy?: boolean; children?: ReactNode }): ReactNode;
