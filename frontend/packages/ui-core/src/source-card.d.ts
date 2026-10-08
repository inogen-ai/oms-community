import type { ReactNode } from "react";
import type { SourceRefDisplay } from "./source-status.js";
export interface SourceCardFingerprint {
  content_generation: number; binding_generation: number; update_generation: number;
  policy_version: string; policy_digest?: string | null; local_digest: string; candidate_digest: string;
}
export interface SourceCardEvidence { kind: "known" | "absent" | "unknown"; value: unknown }
export interface SourceCardChange {
  part_id: string; kind: string; action: "keep" | "add" | "replace" | "remove" | "conflict";
  base: SourceCardEvidence; local: SourceCardEvidence; incoming: SourceCardEvidence; linked_removals: readonly string[];
}
export type SourceCardChoice = { part_id: string; part_fingerprint: string } & (
  { choice: "keep_oms" | "use_upstream"; merged_text?: null } | { choice: "merged_text"; merged_text: string }
);
export interface SourceCardUpdate {
  update_id: string; generation: number; status: string; drafts: readonly SourceCardChoice[]; stale?: boolean;
  resolved_check?: { readonly state: "passed" | "held" | "unavailable" };
  plan: {
    skill: { skill_id: string }; origin: { kind: string }; base: { snapshot_id: string } | null; incoming: { snapshot_id: string };
    fingerprint: SourceCardFingerprint; changes: readonly SourceCardChange[];
    conflicts: readonly { part_id: string; reason: string; allowed_choices: readonly string[] }[]; flags: readonly string[];
  };
}
export interface SourceCardDraft { fingerprint: SourceCardFingerprint; choices: readonly SourceCardChoice[]; removal_consents: readonly string[] }
export interface SourceCardAction { skill_id: string; fingerprint: SourceCardFingerprint }
export function initialSourceDraft(update: SourceCardUpdate): SourceCardDraft;
export function sourceCardState(update: SourceCardUpdate, draft?: SourceCardDraft, partFingerprints?: Readonly<Record<string, string>>): {
  stale: boolean; validChoices: boolean; resolved: boolean; groups: string[][]; consented: boolean;
  saved: boolean; held: boolean; canSave: boolean; canApply: boolean;
};
export function SourceUpdateCard(props: {
  update?: SourceCardUpdate | null; title?: string; draft?: SourceCardDraft;
  partFingerprints?: Readonly<Record<string, string>>; ownership?: Readonly<Record<string, "source" | "shared" | "local" | "unknown">>;
  ref?: SourceRefDisplay; busy?: boolean; loading?: boolean; error?: string; unavailable?: string; freshness?: "fresh" | "stale" | "unavailable";
  onDraftChange?: (draft: SourceCardDraft) => void;
  onSaveDraft?: (body: SourceCardAction & { choices: readonly SourceCardChoice[] }) => void;
  onApply?: (body: SourceCardAction & { removal_consents: readonly string[] }) => void;
  onSkip?: (body: SourceCardAction) => void; onAdopt?: (body: SourceCardAction) => void; onRecheck?: (body: SourceCardAction) => void;
  renderFile?: (change: SourceCardChange) => ReactNode;
}): ReactNode;
