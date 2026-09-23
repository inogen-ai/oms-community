export interface Capabilities {
  edition: "community" | "pro" | "enterprise";
  api_contract_version: "1.0" | "1.1";
  schema_version: 1;
  manual_learning: boolean;
  semantic_compilation: boolean;
  multi_user_identity: boolean;
  team_scoping: boolean;
  personal_mutes: boolean;
  contributor_portal: boolean;
  redaction_vault: boolean;
  model_settings: boolean;
  scheduled_publish: boolean;
  managed_publish: boolean;
  graph_query_console: boolean;
  advanced_review: boolean;
  usage_analytics: boolean;
}
export interface NavigationItem { href: string; label: string; key: string }
export class HttpError extends Error { status: number; code: string; constructor(message: string, status: number, code?: string) }
export function parseCapabilities(value: unknown): Readonly<Capabilities>;
export function communityNavigation(capabilities: Capabilities): NavigationItem[];
export interface Rule { id: string; body: string; status?: string; corroboration_count?: number; skill_ids?: string[]; transaction_ids?: string[] }
export interface Section { id: string; heading?: string; name?: string; body?: string; text?: string; editable?: boolean; kind?: string }
export interface Artefact { path: string; bytes?: number; size?: number; digest?: string }
export interface SkillVersion { id: string; at?: string; cause: string; actor_person_id?: string | null; detail?: string | null; revision?: string }
export interface SkillChange extends SkillVersion { skill_id: string; skill_name: string }
export interface Skill { id: string; name: string; description?: string; domain?: string; publish_enabled?: boolean; body?: string; rules?: Rule[]; sections?: Section[]; artefacts?: (Artefact | string)[]; versions?: SkillVersion[] }
export interface ReviewItem {
  posted_at?: string | null; repo?: string | null; skill_hint?: string | null; source_agent_id?: string | null; source_runtime?: string | null; context?: { user_input: string; agent_output: string } | null; context_truncated?: boolean; txn_id: string; text: string; signal_type: string; source_ref?: string | null; state: string; exact_matches: (Rule | string)[]; candidate_skill_ids: string[]; warning?: string | null; warnings?: string[]; held_reason?: string | null; skill_suggestions?: { id: string; name: string; score: number; reason: string }[]; similar_matches?: { id: string; body: string; score: number; reason: string; exact: boolean }[] }
export interface RuleInsertion { skill_id: string; revision: string; section_id: string; position: "start" | "end" | "after"; after_rule_id?: string }
export interface Decision { action: "create" | "reinforce" | "amend" | "reject" | "release_safety"; body: string; skill_ids: string[]; rule_id?: string; confirm_reinforcement?: boolean; expected_rule_body?: string; amendment?: { skill_id: string; revision: string; parts: { anchor: string; text: string; expected_text: string }[]; affected_skill_ids: string[] }; placements?: RuleInsertion[] }
export interface DocumentPart { anchor: string; kind: string; lines: string[]; editable: boolean; source_id?: string | null; note?: string | null; edit_text?: string | null; affected_skills?: { id: string; name: string }[] }
export interface SkillDocument { skill_id: string; revision: string; parts: DocumentPart[]; path: string; rule_sections?: { id: string; heading: string; rules: { id: string; body: string; group?: string | null }[] }[] }
export interface CompareRow { anchor: string; kind: string; state: string; old_text: string | null; new_text: string | null; rule_id: string | null; restorable: boolean; note?: string | null }
export interface VersionComparison { version: SkillVersion; revision: string; markdown: string; rows: CompareRow[] }
export interface FilePreview { path: string; size: number; text: string | null; reason?: string }
export interface Constraint { id: string; body: string; status?: string }
export interface GraphNode { id: string; label?: string; name?: string; body?: string; kind?: string; labels?: string[]; properties?: Record<string, unknown> }
export interface Graph { nodes: GraphNode[]; edges?: { source: string; target: string; type?: string }[]; relationships?: unknown[] }
export interface GraphNeighbours extends Graph { next_offset: number | null; total: number }
export interface Preview { body?: string; markdown?: string; text?: string; skill_id?: string }
export type Health = Record<string, unknown>;
export interface SkillDeletion { deleted: string; name: string; rules_detached: number }
export type CoreSettings = Record<string, string | number | boolean | null | string[]>;
export interface CommunityClient {
  request<T = unknown>(path: string, init?: RequestInit): Promise<T>;
  capabilities(): Promise<Readonly<Capabilities>>;
  health(): Promise<Health>;
  contribute(body: { correction: string; skill_hint?: string; transaction_id?: string; source_ref?: string; signal_type?: string }): Promise<{ transaction_id: string; state: string }>;
  skills(): Promise<Skill[]>;
  recentSkillChanges(): Promise<SkillChange[]>;
  createSkill(body: { name: string; description?: string; domain: string }): Promise<Skill>;
  skill(id: string): Promise<Skill>;
  updateSkill(id: string, body: { name?: string; description?: string; domain?: string; publish_enabled?: boolean }): Promise<Skill>;
  deleteSkill(id: string): Promise<SkillDeletion>;
  document(id: string): Promise<SkillDocument>;
  saveDocument(id: string, body: { revision: string; parts: { anchor: string; text: string }[] }): Promise<SkillDocument>;
  versions(id: string): Promise<SkillVersion[]>;
  compareVersion(id: string, versionId: string): Promise<VersionComparison>;
  restoreVersion(id: string, versionId: string, body: { revision: string; anchors: string[] }): Promise<SkillDocument>;
  file(id: string, path: string): Promise<FilePreview>;
  updateSection(skillId: string, sectionId: string, text: string): Promise<unknown>;
  review(): Promise<ReviewItem[]>;
  decide(transactionId: string, body: Decision): Promise<unknown>;
  rules(): Promise<Rule[]>;
  updateRule(id: string, body: { action: "retract" | "restore"; body?: string }): Promise<unknown>;
  constraints(): Promise<Constraint[]>;
  createConstraint(body: string): Promise<Constraint>;
  importArchive(file: File): Promise<Record<string, unknown>>;
  importDirectory(files: File[]): Promise<Record<string, unknown>>;
  preview(skillId: string): Promise<Preview | string>;
  publish(): Promise<{ output: string; skills: number | string[]; target?: "local" | "git"; host_path?: string | null; pushed?: boolean }>;
  graph(query?: string, skillId?: string): Promise<Graph>;
  node(id: string): Promise<GraphNode>;
  neighbours(id: string, offset?: number): Promise<GraphNeighbours>;
  settings(): Promise<CoreSettings>;
  updateSettings(body: CoreSettings): Promise<CoreSettings>;
}
export function createClient(options?: { baseUrl?: string; fetch?: typeof globalThis.fetch; requestPolicy?: "local" | "browser" }): CommunityClient;
