import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode, Ref } from "react";
import type { Components } from "react-markdown";
import type { FrontmatterField } from "./frontmatter.js";
import type { TextareaHTMLAttributes } from "react";
export function AutoTextarea(props: TextareaHTMLAttributes<HTMLTextAreaElement> & { inputRef?: (element: HTMLTextAreaElement | null) => void }): ReactNode;
export interface EditableDocumentPart { anchor: string; kind: string; lines: string[]; editable: boolean; source_id?: string | null; note?: string | null; edit_text?: string | null; affected_skills?: { id: string; name: string }[] }
export function DocumentPartsEditor(props: { parts: EditableDocumentPart[]; values: Record<string, string>; onChange: (anchor: string, text: string) => void; disabled?: boolean; actionsForPart?: (part: EditableDocumentPart) => ReactNode }): ReactNode;
export function SkillTextEditor(props: Parameters<typeof DocumentPartsEditor>[0] & { guidanceOnly?: boolean; allowRuleRemoval?: boolean; focusAnchor?: string; replacementText?: string }): ReactNode;
export type VersionComparisonView = "overview" | "detailed";
export function SkillDeleteConfirmation(props: { name: string; confirmation: string; onConfirmationChange: (value: string) => void; onConfirm: () => void; onCancel: () => void; busy?: boolean; hasUnsavedChanges?: boolean }): ReactNode;
export function VersionComparison(props: { rows: { anchor: string; kind: string; state: string; old_text: string | null; new_text: string | null; restorable: boolean; note?: string | null }[]; selected: string[]; onSelect: (anchor: string, checked: boolean) => void; disabled?: boolean; view?: VersionComparisonView }): ReactNode;
export function Panel(props: HTMLAttributes<HTMLElement> & { title?: string; children?: ReactNode }): ReactNode;
export function Button(props: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "danger"; children?: ReactNode }): ReactNode;
export function Notice(props: { kind?: "info" | "error" | "success"; children?: ReactNode }): ReactNode;
export function EmptyState(props: { title: string; children?: ReactNode }): ReactNode;
export function Badge(props: { children?: ReactNode }): ReactNode;
export function ProductMark(props: { title?: string; logoSrc?: string; className?: string }): ReactNode;
export function BrandMark(props: { logoSrc?: string; className?: string }): ReactNode;
export function Markdown(props: HTMLAttributes<HTMLDivElement> & {
  body: string; className?: string; variant?: "document" | "review" | "unstyled";
  inert?: boolean; renderImages?: boolean; components?: Components; ref?: Ref<HTMLDivElement>;
}): ReactNode;
export function Frontmatter(props: { fields: FrontmatterField[] }): ReactNode;
export function SkillMarkdown(props: Parameters<typeof Markdown>[0]): ReactNode;
export function SkillPackagePicker(props: {
  directory?: boolean;
  onChooseDirectory?: (files: File[]) => void;
  onChoose: (file: File) => void; disabled?: boolean; label?: string;
  inputLabel?: string; className?: string; children?: ReactNode;
}): ReactNode;
export interface ManualRulePlacement { skill_id: string; revision: string; section_id: string; position: "start" | "end" | "after"; after_rule_id?: string }
export interface ManualDocument { skill_id: string; revision: string; parts: EditableDocumentPart[]; path: string; rule_sections?: { id: string; heading: string; rules: { id: string; body: string; group?: string | null }[] }[] }
export interface ManualAmendmentDraft { skill_id: string; document?: ManualDocument; values: Record<string, string>; focus_anchor?: string }
export interface ManualAmendmentDecision { skill_id: string; revision: string; parts: { anchor: string; text: string; expected_text: string }[]; affected_skill_ids: string[] }
export interface ManualReviewDraft { body: string; skill_ids: string[]; rule_id?: string; confirm_reinforcement?: boolean; expected_rule_body?: string; mode?: "rule" | "amend"; amendment?: ManualAmendmentDraft; placements?: ManualRulePlacement[]; separate_rule?: boolean; replacement?: { skill_id: string; document: ManualDocument; anchor: string }; confirm_shared_replacement?: boolean }
export interface ManualDecision { action: "create" | "reinforce" | "amend" | "reject" | "release_safety"; body: string; skill_ids: string[]; rule_id?: string; confirm_reinforcement?: boolean; expected_rule_body?: string; amendment?: ManualAmendmentDecision; placements?: ManualRulePlacement[] }
export interface ManualItem {
  posted_at?: string | null; repo?: string | null; skill_hint?: string | null; source_agent_id?: string | null; source_runtime?: string | null; context?: { user_input: string; agent_output: string } | null; context_truncated?: boolean;
  txn_id: string; text: string; signal_type: string; state: string;
  source_ref?: string | null; warning?: string | null; warnings?: string[]; held_reason?: string | null;
  candidate_skill_ids: string[]; exact_matches: ({ id: string; body: string } | string)[];
  skill_suggestions?: { id: string; name: string; score: number; reason: string }[];
  similar_matches?: { id: string; body: string; score: number; reason: string; exact: boolean; skill_ids?: string[] }[];
}
export function ManualReviewCard(props: { item: ManualItem; skills: { id: string; name: string }[]; onDecision: (decision: ManualDecision) => Promise<unknown>; createSkillHref?: string; draft?: ManualReviewDraft; onDraftChange?: (draft: ManualReviewDraft) => void; loadDocument?: (skillId: string) => Promise<ManualDocument> }): ReactNode;

export { collectionPage, CollectionPagination, CollectionViewToggle, CollectionList, CollectionMultiSelect } from "./collection.js";
