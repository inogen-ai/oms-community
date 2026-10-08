import type { ByteResponse, SkillVersion } from "./index.js";
export type JSONValue = null | boolean | number | string | JSONValue[] | { [key: string]: JSONValue };
export interface SourceRecord { readonly format_version: 1 }
export interface SourcePage<T> extends SourceRecord { readonly items: readonly T[]; readonly next_cursor: string | null }
export interface SourcePageQuery { cursor?: string | null; limit?: number }
export interface SkillRef { readonly tenant_id: string; readonly skill_id: string }
export interface SourceGenerations extends SourceRecord { readonly skill: SkillRef; readonly content: number; readonly binding: number }
export interface SourceOrigin extends SourceRecord { readonly skill: SkillRef; readonly origin_id: string; readonly kind: "github" | "local"; readonly generation: number }
export interface SourceSnapshotRef extends SourceRecord { readonly snapshot_id: string; readonly origin: SourceOrigin }
export interface SourceEvidence extends SourceRecord { readonly kind: "known" | "absent" | "unknown"; readonly value: JSONValue; readonly source_digest: string | null; readonly policy_version: string }
export interface PlanFingerprint {
  readonly format_version?: 1;
  readonly content_generation: number; readonly binding_generation: number; readonly update_generation: number;
  readonly policy_version: string; readonly local_digest: string; readonly candidate_digest: string;
  readonly policy_digest?: string | null;
}
export interface ResolvedSourceRef {
  readonly format_version?: 1; readonly canonical_url: string; readonly kind: "branch" | "tag" | "commit";
  readonly name: string; readonly package_path: string; readonly commit: string;
}
export type SourcePartKind = "field" | "section" | "rule" | "example" | "file" | "placement";
export type SourcePlanFlag = "conflict" | "unknown_identity" | "deletion_consent" | "script_changes"
  | "declared_licence_changed" | "unproven_history" | "rewritten_history" | "first_reconciliation"
  | "safety_hold" | "required_check_unavailable";
export interface SourceChange extends SourceRecord {
  readonly part_id: string; readonly kind: SourcePartKind; readonly action: "keep" | "add" | "replace" | "remove" | "conflict";
  readonly base: SourceEvidence; readonly local: SourceEvidence; readonly incoming: SourceEvidence; readonly linked_removals: readonly string[];
}
export type SourceDraftChoice = {
  readonly format_version?: 1; readonly part_id: string; readonly part_fingerprint: string;
} & ({ readonly choice: "keep_oms" | "use_upstream"; readonly merged_text?: null } | { readonly choice: "merged_text"; readonly merged_text: string });
export interface SourceConflict extends SourceRecord {
  readonly part_id: string; readonly reason: string; readonly allowed_choices: readonly SourceDraftChoice["choice"][];
}
export interface SourceMergePlan extends SourceRecord {
  readonly skill: SkillRef; readonly origin: SourceOrigin; readonly base: SourceSnapshotRef | null; readonly incoming: SourceSnapshotRef;
  readonly fingerprint: PlanFingerprint; readonly changes: readonly SourceChange[]; readonly conflicts: readonly SourceConflict[]; readonly flags: readonly SourcePlanFlag[];
}
export interface SourceUpdate extends SourceRecord {
  readonly update_id: string; readonly plan: SourceMergePlan; readonly generation: number;
  readonly status: "open" | "applied" | "skipped" | "adopted" | "superseded" | "closed";
  readonly review_item_id: string; readonly drafts: readonly SourceDraftChoice[]; readonly operation_id: string | null;
}
export interface SourceStatus extends SourceRecord {
  readonly state: "unchecked" | "up_to_date" | "updates_available" | "failed" | "missing" | "redirect";
  readonly checked_at: string | null; readonly code: string | null;
}
/** What a skill list row says about its GitHub source; `status` is the skill's own binding status. */
export interface SkillSourceSummary {
  readonly source_id: string; readonly canonical_url: string; readonly status: SourceStatus | null;
  readonly ref_kind: "branch" | "tag" | "commit"; readonly ref_name: string;
}
export interface SkillSource extends SourceRecord {
  readonly source_id: string; readonly tenant_id: string; readonly canonical_url: string; readonly confirmed_aliases: readonly string[];
  readonly discovery_root: string; readonly credential_profile_id: string | null; readonly generation: number;
  readonly scheduled: boolean; readonly status: SourceStatus | null;
}
export interface SourceBinding extends SourceRecord {
  readonly origin: SourceOrigin; readonly source_id: string; readonly package_path: string; readonly ref: ResolvedSourceRef;
  readonly baseline: SourceSnapshotRef | null; readonly automatic_apply: boolean; readonly automation_actor_id: string | null;
  readonly first_reconciliation: boolean; readonly status: SourceStatus | null; readonly active: boolean;
}
export interface SourceLocalStream extends SourceRecord {
  readonly origin: SourceOrigin; readonly baseline: SourceSnapshotRef | null; readonly transport: "zip" | "folder" | "cli" | null;
  readonly uploader_id: string | null; readonly filename: string | null;
}
export interface SourceBlockedAttempt extends SourceRecord {
  readonly attempt_id: string; readonly origin: SourceOrigin; readonly actor_id: string; readonly reason: string;
  readonly existing_update_id: string; readonly retry_needed: boolean;
}
export interface SourceBindingView {
  readonly binding: SourceBinding | null; readonly local_stream: SourceLocalStream | null;
  readonly generations: SourceGenerations; readonly blocked_attempts: readonly SourceBlockedAttempt[];
}
export interface SkillSourceView { readonly source: SkillSource; readonly bindings: SourcePage<SourceBinding> }
export interface SourceDiscoveredPackage extends SourceRecord {
  readonly path: string; readonly valid: boolean; readonly upstream_name: string | null; readonly description: string | null;
  readonly file_count: number; readonly total_bytes: number; readonly reasons: readonly string[]; readonly unsupported_metadata: readonly string[];
}
export interface SourceDiscovery {
  readonly discovery_id: string; readonly resolved_ref: ResolvedSourceRef; readonly expires_at: string;
  readonly preselected_path: string | null; readonly packages: SourcePage<SourceDiscoveredPackage>;
}
export type SourceOperationState = "fetching" | "planning" | "awaiting_review" | "applying" | "complete" | "blocked" | "failed";
export interface SourceOperation {
  readonly operation_id: string; readonly state: SourceOperationState; readonly committed: boolean;
  readonly outcomes: readonly { readonly skill_id: string; readonly state: "applied" | "awaiting_review" | "blocked" | "failed" | "unchanged"; readonly update_id: string | null; readonly code: string | null }[];
  readonly source_id?: string | null;
  readonly discovery_id?: string | null;
}
export interface SourceHistoryEntry extends SkillVersion {
  skill_id: string; tenant_id: string; at: string; revision: string; group_id: string | null;
  parts_json: string; metadata_json: string; rules_json: string; files_json: string | null;
  source_operation_id: string | null; source_origin_id: string | null; source_revision: string | null;
  undo?: SourceUndoSummary | null;
}
export interface SourceFilePreview {
  readonly path: string; readonly side: "base" | "local" | "upstream" | "discovery"; readonly size: number;
  readonly binary: boolean; readonly text: string | null; readonly truncated: boolean;
  readonly byte_limit: number; readonly line_limit: number; readonly media_type: "text/plain";
}
export interface DiscoverSourceBody { url: string; credential_profile_id?: string | null; ref_kind?: "branch" | "tag" | "commit" | null; ref_name?: string | null; package_path?: string | null; discovery_root?: string }
export interface InstallSourceBody { discovery_id: string; selections: readonly { package_path: string; local_name: string; domain: string }[] }
export interface CreateSourceBody { discovery_id: string }
export type LocalSourceSelection = { package_path: string; removal_consents?: readonly string[] } & (
  { target_skill_id: string; local_name?: never; domain?: never } |
  { target_skill_id?: never; local_name: string; domain: string }
);
export interface LocalSourceBody { upload_id: string; selections: readonly LocalSourceSelection[] }
export interface CheckSourceBody { expected_source_generation: number; skill_ids: readonly string[] }
export interface SourceBindingBody { expected_content_generation: number; expected_binding_generation: number }
export interface LinkSourceBody extends SourceBindingBody { source_id: string; package_path: string; ref: ResolvedSourceRef }
export interface RetargetSourceBody extends SourceBindingBody { ref: ResolvedSourceRef }
export interface SourceAutomationBody extends SourceBindingBody { enabled: boolean }
export interface SourceResolvedCheck extends SourceRecord {
  readonly state: "passed" | "held" | "unavailable"; readonly code: string; readonly reasons: readonly string[];
}
export interface SourceUpdateView extends SourceUpdate {
  readonly part_fingerprints: Readonly<Record<string, string>>;
  readonly ownership: Readonly<Record<string, "source" | "shared" | "local" | "unknown">>;
  readonly stale: boolean;
  readonly resolved_check: SourceResolvedCheck;
}
export interface SourceUpdateBody { skill_id: string; fingerprint: PlanFingerprint }
export interface SourceDraftBody extends SourceUpdateBody { choices: readonly SourceDraftChoice[] }
export interface SourceApplyBody extends SourceUpdateBody { removal_consents?: readonly string[] }
export interface SourceBulkApplyBody { updates: readonly (SourceApplyBody & { update_id: string })[] }
export interface SourceSkillGeneration { skill_id: string; content: number; binding: number }
export interface SourceUndoBody { undo_id: string; skill_id: string; expected_generations: readonly SourceSkillGeneration[]; policy_version: string }
export interface SourceUndoSummary extends SourceUndoBody { update_id: string; available: boolean; reasons: readonly string[] }
export interface RemoveSourceBody { expected_source_generation: number; expected_generations: readonly SourceSkillGeneration[] }
export interface RelocateSourceBody extends RemoveSourceBody { destination_url: string }
export interface ScheduleSourceBody extends RemoveSourceBody { enabled: boolean }
export type SourceRequest = <T = unknown>(path: string, init?: RequestInit) => Promise<T>;
export type SourceByteRequest = (path: string, init?: RequestInit) => Promise<ByteResponse>;
export type SourceRequestContext = (path: string, init?: RequestInit) => { path: string; init?: RequestInit } | Promise<{ path: string; init?: RequestInit }>;
export interface SourcesClient {
  workspace(): Promise<{ workspace_id: string }>;
  /** Call when service, scope or relevant settings change to fence delayed responses. */
  invalidate(): void;
  discover(body: DiscoverSourceBody, key: string): Promise<SourceDiscovery>;
  discovery(id: string, page?: SourcePageQuery): Promise<SourceDiscovery>;
  discardDiscovery(id: string, key: string): Promise<void>;
  install(body: InstallSourceBody, key: string): Promise<SourceOperation>;
  createSource(body: CreateSourceBody, key: string): Promise<SourceOperation>;
  installLocal(body: LocalSourceBody, key: string): Promise<SourceOperation>;
  sources(page?: SourcePageQuery): Promise<SourcePage<SkillSource>>;
  source(id: string, page?: SourcePageQuery): Promise<SkillSourceView>;
  check(id: string, body: CheckSourceBody, key: string): Promise<SourceOperation>;
  schedule(id: string, body: ScheduleSourceBody, key: string): Promise<SourceOperation>;
  removeSource(id: string, body: RemoveSourceBody, key: string): Promise<SourceOperation>;
  relocateSource(id: string, body: RelocateSourceBody, key: string): Promise<SourceOperation>;
  binding(skillId: string): Promise<SourceBindingView>;
  link(skillId: string, body: LinkSourceBody, key: string): Promise<SourceOperation>;
  relink(skillId: string, body: LinkSourceBody, key: string): Promise<SourceOperation>;
  retarget(skillId: string, body: RetargetSourceBody, key: string): Promise<SourceOperation>;
  unlink(skillId: string, body: SourceBindingBody, key: string): Promise<SourceOperation>;
  automation(skillId: string, body: SourceAutomationBody, key: string): Promise<SourceOperation>;
  updates(page?: SourcePageQuery): Promise<SourcePage<SourceUpdate>>;
  update(id: string): Promise<SourceUpdateView>;
  saveDraft(id: string, body: SourceDraftBody, key: string): Promise<SourceOperation>;
  recheck(id: string, body: SourceUpdateBody, key: string): Promise<SourceOperation>;
  apply(id: string, body: SourceApplyBody, key: string): Promise<SourceOperation>;
  skip(id: string, body: SourceUpdateBody, key: string): Promise<SourceOperation>;
  adopt(id: string, body: SourceUpdateBody, key: string): Promise<SourceOperation>;
  undo(id: string, body: SourceUndoBody, key: string): Promise<SourceOperation>;
  bulkApply(body: SourceBulkApplyBody, key: string): Promise<SourceOperation>;
  operation(id: string): Promise<SourceOperation>;
  operationForKey(key: string): Promise<SourceOperation>;
  history(skillId: string, page?: SourcePageQuery): Promise<SourcePage<SourceHistoryEntry>>;
  discoveryFile(id: string, file: { package_path: string; path: string; preview: true }): Promise<SourceFilePreview>;
  discoveryFile(id: string, file: { package_path: string; path: string; preview?: false }): Promise<ByteResponse>;
  discoveryFile(id: string, file: { package_path: string; path: string; preview: boolean }): Promise<SourceFilePreview | ByteResponse>;
  updateFile(id: string, file: { side: "base" | "local" | "upstream"; path: string; preview: true }): Promise<SourceFilePreview>;
  updateFile(id: string, file: { side: "base" | "local" | "upstream"; path: string; preview?: false }): Promise<ByteResponse>;
  updateFile(id: string, file: { side: "base" | "local" | "upstream"; path: string; preview: boolean }): Promise<SourceFilePreview | ByteResponse>;
}
export function createSourcesClient(options: { request: SourceRequest; requestBytes: SourceByteRequest; context?: SourceRequestContext }): SourcesClient;
