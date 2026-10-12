import type { ReactNode } from "react";
import type { SourceRefDisplay } from "./source-status.js";
import type { SourceRepositoryDisplay } from "./source-repository.js";
import type { SourceFileSide, SourceFlaggedLine } from "./source-files.js";
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
  | { choice: "keep_oms"; merged_text?: null }
  | { choice: "use_upstream"; merged_text?: null; acknowledged?: readonly ("phone" | "email")[] }
  | { choice: "merged_text"; merged_text: string }
);
export type SourceCardRisk = "personal_data" | "injection" | "check_failed" | "check_unavailable" | "your_edits"
  | "removed_upstream" | "unclear_identity" | "script";
/** Where a check held and why: never the matched text. */
export interface SourceCardFinding { path: string | null; part_id: string | null; line: number | null; category: string; summary: string }
/** The server's triage of one change: what the card asks about, and why. */
export interface SourceCardPart {
  part_id: string; kind: string; label: string; attention: "decide" | "safe" | "automatic";
  risks: readonly SourceCardRisk[]; default: "use_upstream" | null; overridable: readonly ("phone" | "email")[];
  findings: readonly SourceCardFinding[];
}
export interface SourceCardSummary {
  decide: number; safe: number; automatic: number; risks: Readonly<Partial<Record<SourceCardRisk, number>>>;
  /** Findings that name no part: they hold the whole skill. */
  whole_skill: readonly SourceCardFinding[];
}
export interface SourceCardUpdate {
  update_id: string; generation: number; status: string; drafts: readonly SourceCardChoice[]; stale?: boolean;
  held_install?: boolean; check_findings?: readonly SourceCardFinding[];
  resolved_check?: { readonly state: "passed" | "held" | "unavailable" };
  /** Absent on a server without triage: the card then asks about every conflict and removal. */
  parts?: Readonly<Record<string, SourceCardPart>>;
  summary?: SourceCardSummary;
  repository?: SourceRepositoryDisplay | null;
  plan: {
    skill: { skill_id: string }; origin: { kind: string }; base: { snapshot_id: string } | null; incoming: { snapshot_id: string };
    fingerprint: SourceCardFingerprint; changes: readonly SourceCardChange[];
    conflicts: readonly { part_id: string; reason: string; allowed_choices: readonly string[] }[]; flags: readonly string[];
  };
}
export interface SourceCardDraft { fingerprint: SourceCardFingerprint; choices: readonly SourceCardChoice[]; removal_consents: readonly string[] }
export interface SourceCardAction { skill_id: string; fingerprint: SourceCardFingerprint }
/** The primary action's body: `save` is true when the effective choices differ from the saved draft. */
export type SourceCardApply = SourceCardAction & { choices: readonly SourceCardChoice[]; removal_consents: readonly string[]; save: boolean };
/** One radio: `acknowledged` and `note` are set on "Take it anyway". */
export interface SourceCardOption {
  choice: SourceCardChoice["choice"]; label: string; acknowledged?: readonly ("phone" | "email")[]; note?: string;
}
export interface SourceCardGroup { risk: SourceCardRisk | "other"; label: string; parts: readonly SourceCardPart[] }
export interface SourceCardGroupOption {
  choice: "keep_oms" | "use_upstream"; label: string; note?: string;
  /** "This also removes 2 parts in OMS." when taking the group removes the parts it replaces. */
  removalNote?: string;
  /** The matching option of the group's part at `index`. */
  optionFor(index: number): SourceCardOption;
}
export const SOURCE_RISK_ORDER: readonly SourceCardRisk[];
export const SOURCE_RISK_LABELS: Readonly<Record<SourceCardRisk | "other", string>>;
export function initialSourceDraft(update: SourceCardUpdate): SourceCardDraft;
/**
 * A local draft made on an older revision of the card: keeps each choice the card still offers for the
 * same part revision, drops the rest and takes the card's fingerprint. A matching draft, or a card the
 * server calls stale, is returned as it is.
 */
export function carrySourceDraft<T extends SourceCardDraft | undefined>(draft: T, update: SourceCardUpdate | null | undefined,
  partFingerprints?: Readonly<Record<string, string>>): T;
/** The server's triage, or a fallback for a server without it. */
export function sourceReviewParts(update: SourceCardUpdate): Readonly<Record<string, SourceCardPart>>;
/** Plain reason lines for a part's findings: places and counts, never the matched text. */
export function sourceFindingReasons(findings?: readonly SourceCardFinding[]): string[];
/** The part's own flagged lines (a file's by its path, a prose part's by its id), each with its plain tag. */
export function sourceFlaggedLines(part: Pick<SourceCardPart, "part_id" | "kind" | "findings">): SourceFlaggedLine[];
/**
 * What the card hands the host's file preview: the part's flagged lines, the sides OMS has no copy of, and where
 * the update comes from ("local" for a re-imported package), which names the columns.
 */
export interface SourceCardFileView { flagged: readonly SourceFlaggedLine[]; absent: readonly SourceFileSide[]; origin: "github" | "local" }
export function sourceChoiceOptions(update: SourceCardUpdate, change: SourceCardChange, part: SourceCardPart,
  conflict?: { allowed_choices: readonly string[] } | null): SourceCardOption[];
/** The draft's choices plus each safe part's default. */
export function sourceEffectiveChoices(update: SourceCardUpdate, draft?: SourceCardDraft, partFingerprints?: Readonly<Record<string, string>>): SourceCardChoice[];
/** The parts that need a decision, grouped by their first risk in SOURCE_RISK_ORDER. */
export function sourceNeedsYou(update: SourceCardUpdate): SourceCardGroup[];
export function sourceGroupOptions(update: SourceCardUpdate, group: SourceCardGroup): SourceCardGroupOption[];
/** "16 rules, 5 sections and 6 files passed the checks. They will be installed." */
export function sourceSafeSummary(update: SourceCardUpdate, choices: readonly SourceCardChoice[]): string;
export interface SourceCardState {
  stale: boolean; validChoices: boolean; resolved: boolean; groups: string[][]; saved: boolean; held: boolean; taken: boolean;
  /** Parts still waiting for a decision. */
  waiting: number;
  /** Decisions still open as the card asks them: a group answered with one choice counts once. */
  waitingItems: number;
  choices: SourceCardChoice[]; canApply: boolean;
}
/**
 * The card's status sentence: "Ready to install." or "Ready to apply." only when `state.canApply`;
 * otherwise the out-of-date wording, "N items need you." (counting decisions), the hold, or the held-install "Choose at least one part" rule.
 */
export function sourceStatusSentence(update: SourceCardUpdate, state: Pick<SourceCardState, "stale" | "waitingItems" | "held" | "taken" | "canApply">): string;
export function sourceCardState(update: SourceCardUpdate, draft?: SourceCardDraft, partFingerprints?: Readonly<Record<string, string>>): SourceCardState;
export function SourceUpdateCard(props: {
  update?: SourceCardUpdate | null; title?: string; draft?: SourceCardDraft;
  partFingerprints?: Readonly<Record<string, string>>; ownership?: Readonly<Record<string, "source" | "shared" | "local" | "unknown">>;
  ref?: SourceRefDisplay; now?: Date; busy?: boolean; loading?: boolean; error?: string; unavailable?: string; freshness?: "fresh" | "stale" | "unavailable";
  onDraftChange?: (draft: SourceCardDraft) => void;
  onApply?: (body: SourceCardApply) => void;
  onSkip?: (body: SourceCardAction) => void; onAdopt?: (body: SourceCardAction) => void; onRecheck?: (body: SourceCardAction) => void;
  /** Offered on a held install only; the host asks for confirmation before it deletes. */
  onDeleteSkill?: () => void;
  renderFile?: (change: SourceCardChange, view: SourceCardFileView) => ReactNode;
}): ReactNode;
