import { allowedRequest } from "./route-contract.mjs";
import type { Page } from "@playwright/test";
import type { SourceBindingView, SourceUpdateView, SkillSource, SourceOperation, SourceDiscovery } from "@inogen/oms-client";

type Mutable<T> = { -readonly [K in keyof T]: T[K] };
const API = `http://127.0.0.1:${process.env.OMS_COMMUNITY_API_PORT || "4317"}`;
const UI = `http://127.0.0.1:${process.env.OMS_COMMUNITY_UI_PORT || "4318"}`;
const format_version = 1 as const;
const skill = { id: "expenses", name: "Expense review", domain: "operations", description: "Review expense documents", body: "# Expenses", artefacts: [], versions: [] };
const skillRef = { tenant_id: "local", skill_id: skill.id };
const origin = { format_version, skill: skillRef, origin_id: "origin-1", kind: "github" as const, generation: 1 };
const ref = { format_version, canonical_url: "https://github.com/fixture/skills", kind: "branch" as const, name: "main", package_path: "expenses", commit: "a".repeat(40) };
const evidence = (value: string) => ({ format_version, kind: "known" as const, value, source_digest: null, policy_version: "p" });
export async function sourceFixture(page: Page, options: { enabled?: boolean; blocked?: boolean; loseApply?: boolean; removal?: boolean; clean?: boolean; script?: boolean; loseLocal?: boolean; blockLocal?: boolean; blockRetarget?: boolean; undo?: boolean; unlinked?: boolean; file?: boolean; loseDiscovery?: boolean; loseInstall?: boolean; invalidOperation?: boolean; bindingCount?: number; localReview?: boolean } = {}) {
  const source: SkillSource = { format_version, source_id: "source-1", tenant_id: "local", canonical_url: ref.canonical_url, confirmed_aliases: [], discovery_root: "", credential_profile_id: null, generation: 1, scheduled: false, status: { format_version, state: "updates_available", checked_at: null, code: null } };
  const binding: Mutable<SourceBindingView> = { binding: { format_version, origin, source_id: source.source_id, package_path: "expenses", ref, baseline: { format_version, snapshot_id: "base", origin }, automatic_apply: false, automation_actor_id: null, first_reconciliation: true, status: source.status, active: true }, local_stream: null, generations: { format_version, skill: skillRef, content: 1, binding: 1 }, blocked_attempts: options.blocked ? [{ format_version, attempt_id: "blocked-1", origin, actor_id: "local-operator", reason: "Other-origin update remains open", existing_update_id: "update-1", retry_needed: true }] : [] };
  const originalBinding = structuredClone(binding.binding);
  if (options.unlinked) { binding.binding = null; binding.local_stream = { format_version, origin: { ...origin, origin_id: "local-origin", kind: "local" }, baseline: null, transport: "zip", uploader_id: "local-operator", filename: "earlier.zip" }; }
  const update: Mutable<SourceUpdateView> = { format_version, update_id: "update-1", generation: 1, status: "open", review_item_id: "review-1", drafts: [], operation_id: null, stale: false, part_fingerprints: { "section:intro": "part-1" }, ownership: { "section:intro": "shared" }, resolved_check: { format_version, state: "passed", code: "checked", reasons: [] }, plan: { format_version, skill: skillRef, origin, base: { format_version, snapshot_id: "base", origin }, incoming: { format_version, snapshot_id: "incoming", origin }, fingerprint: { format_version, content_generation: 1, binding_generation: 1, update_generation: 1, policy_version: "p", policy_digest: "policy", local_digest: "local", candidate_digest: "incoming" }, changes: [{ format_version, part_id: "section:intro", kind: "section", action: "conflict", base: evidence("Check receipts."), local: evidence("Keep receipt references."), incoming: evidence("Compare receipts and orders."), linked_removals: [] }], conflicts: [{ format_version, part_id: "section:intro", reason: "Both sides changed", allowed_choices: ["keep_oms", "use_upstream", "merged_text"] }], flags: ["conflict", "first_reconciliation"] } };
  if (options.file) update.plan = { ...update.plan, flags: [], conflicts: [], changes: [{ ...update.plan.changes[0], part_id: "file:references/guide.md", kind: "file", action: "replace", base: { ...evidence(""), value: { path: "references/guide.md", digest: "base-digest", size: 12 } }, local: { ...evidence(""), value: { path: "references/guide.md", digest: "local-digest", size: 12 } }, incoming: { ...evidence(""), value: { path: "references/guide.md", digest: "upstream-digest", size: 12, published: false, exclusion_reason: "reserved output" } } }] };
  if (options.removal) update.plan = { ...update.plan, flags: ["deletion_consent"], conflicts: [], changes: [{ ...update.plan.changes[0], action: "remove", incoming: { ...evidence(""), kind: "absent", value: null } }] };
  if (options.clean) update.plan = { ...update.plan, flags: [], conflicts: [], changes: [{ ...update.plan.changes[0], action: "replace" }] };
  const localOrigin = { ...origin, origin_id: "local-origin", kind: "local" as const };
  const localUpdate: Mutable<SourceUpdateView> = { ...structuredClone(update), update_id: "update-local", plan: { ...update.plan, origin: localOrigin, base: { format_version, snapshot_id: "local-base", origin: localOrigin }, incoming: { format_version, snapshot_id: "local-incoming", origin: localOrigin } } };
  const scriptOrigin = { ...origin, origin_id: "script-origin", skill: { tenant_id: "local", skill_id: "scripts" } };
  const script: Mutable<SourceUpdateView> = { ...structuredClone(update), update_id: "update-script", plan: { ...update.plan, skill: scriptOrigin.skill, origin: scriptOrigin, base: { format_version, snapshot_id: "script-base", origin: scriptOrigin }, incoming: { format_version, snapshot_id: "script-incoming", origin: scriptOrigin }, flags: ["script_changes"], conflicts: [], changes: [] } };
  const discovery: SourceDiscovery = { discovery_id: "discovery-1", resolved_ref: ref, expires_at: "2099-01-01T00:00:00Z", preselected_path: null, packages: { format_version, next_cursor: null, items: ["expenses", "orders"].map(path => ({ format_version, path, valid: true, upstream_name: path === "expenses" ? "Expenses incoming" : "Orders", description: null, file_count: 1, total_bytes: 10, reasons: [], unsupported_metadata: [] })) } };
  const additionalBindings = Array.from({ length: Math.max(0, (options.bindingCount ?? 1) - 1) }, (_, index) => {
    const id = `linked-${String(index + 1).padStart(3, "0")}`;
    const extraOrigin = { ...origin, origin_id: `origin-${id}`, skill: { ...skillRef, skill_id: id } };
    return { ...originalBinding!, origin: extraOrigin, baseline: { format_version, snapshot_id: `base-${id}`, origin: extraOrigin } };
  });
  const state = { source, binding, update, localUpdate, discovery, workspaceId: "11111111-1111-4111-8111-111111111111", workspaceAvailable: true, checkClaims: 0, additionalBindings,
    skills: [skill, ...additionalBindings.map(row => ({ ...skill, id: row.origin.skill.skill_id, name: row.origin.skill.skill_id }))], requests: [] as { method: string; path: string; key: string | undefined; body: unknown; query: string; workspace: string | undefined }[], operations: new Map<string, SourceOperation>(), decisions: [] as string[], lost: false, uploadCount: 0, localLost: false, undone: false, discoveryLost: false, installLost: false, extraUpdates: options.script ? [script] : [], violations: [] as string[] };
  await page.route("**/*", route => {
    const origin = new URL(route.request().url()).origin;
    if (origin === UI || origin === API) return route.continue();
    state.violations.push(`Unexpected origin: ${origin}`);
    return route.abort();
  });
  await page.route(`${API}/api/**`, async route => {
    const request = route.request(), url = new URL(request.url()), method = request.method(), path = url.pathname;
    const headers = { "Access-Control-Allow-Origin": UI, "Access-Control-Allow-Headers": "Content-Type, Idempotency-Key, X-Source-Workspace", "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS" };
    const reply = (body: unknown, status = 200) => route.fulfill({ status, headers, json: body });
    if (method === "OPTIONS") return reply({});
    if (url.searchParams.has("tenant_id") || request.headers()["x-tenant-id"]) state.violations.push("Tenant override");
    if (!allowedRequest(method, path)) { state.violations.push(`${method} ${path}`); return reply({ message: "Forbidden fixture request" }, 418); }
    const body = request.headers()["content-type"]?.includes("application/json") ? request.postDataJSON() as Record<string, unknown> : undefined;
    const key = request.headers()["idempotency-key"];
    state.requests.push({ method, path, key, body, query: url.search, workspace: request.headers()["x-source-workspace"] });
    if (path !== "/api/skill-source-workspace" && request.headers()["x-source-workspace"] && request.headers()["x-source-workspace"] !== state.workspaceId) return reply({ code: "workspace_changed", message: "The source workspace changed" }, 409);
    if (path === "/api/capabilities") return reply({ edition: "community", api_contract_version: "1.2", schema_version: 2, github_skill_sources: options.enabled ?? true, manual_learning: true, semantic_compilation: false, multi_user_identity: false, team_scoping: false, personal_mutes: false, contributor_portal: false, redaction_vault: false, model_settings: false, scheduled_publish: false, managed_publish: false, graph_query_console: false, advanced_review: false, usage_analytics: false });
    if (path === "/api/skill-source-workspace") return state.workspaceAvailable ? reply({ workspace_id: state.workspaceId }) : reply({ message: "Workspace identity is unavailable" }, 503);
    if (path === "/api/skills") return reply(state.skills.map(row => {
      const linked = row.id === skill.id ? binding.binding : additionalBindings.find(extra => extra.origin.skill.skill_id === row.id);
      return { ...row, source: linked?.active ? { source_id: source.source_id, canonical_url: source.canonical_url, status: linked.status, ref_kind: linked.ref.kind, ref_name: linked.ref.name } : null };
    }));
    if (path === "/api/skills/expenses") return reply(skill);
    if (["/api/review", "/api/import-review", "/api/skills/expenses/versions"].includes(path)) return reply([]);
    if (path === "/api/skill-sources" && method === "GET") return reply({ format_version, items: [source], next_cursor: null });
    if (path === "/api/skill-sources/source-1") {
      const rows = [...(binding.binding ? [binding.binding] : []), ...additionalBindings];
      const offset = Number(url.searchParams.get("cursor") ?? 0);
      return reply({ source, bindings: { format_version, items: rows.slice(offset, offset + 100), next_cursor: rows.length > offset + 100 ? String(offset + 100) : null } });
    }
    const extraBinding = additionalBindings.find(row => path === `/api/skills/${row.origin.skill.skill_id}/source-binding`);
    if (extraBinding) return reply({ binding: extraBinding, local_stream: null, generations: { format_version, skill: extraBinding.origin.skill, content: 1, binding: 1 }, blocked_attempts: [] });
    if (path === "/api/skills/expenses/source-binding" && method === "GET") return reply(binding);
    if (path === "/api/skills/expenses/source-history") return reply({ format_version, items: options.undo ? [{ id: "history-1", skill_id: "expenses", tenant_id: "local", at: "2026-10-06T12:00:00Z", revision: "revision-1", cause: "source_update", actor_person_id: "local-operator", detail: "Updated supporting files", group_id: null, parts_json: "[]", metadata_json: "{}", rules_json: "[]", files_json: "[]", source_operation_id: "prior-operation", source_origin_id: "origin-1", source_revision: "a".repeat(40), undo: { undo_id: "undo-1", update_id: "update-1", skill_id: "expenses", expected_generations: [{ skill_id: "expenses", content: 1, binding: 1 }], policy_version: "p", available: !state.undone, reasons: state.undone ? ["already_undone"] : [] } }] : [], next_cursor: null });
    if (path === "/api/skill-updates") return reply({ format_version, items: [update, ...(options.localReview ? [localUpdate] : []), ...state.extraUpdates].filter(row => row.status === "open"), next_cursor: null });
    if (path === "/api/skill-updates/update-script") return reply(script);
    if (path === "/api/skill-updates/update-local" && method === "GET") return reply(localUpdate);
    if (path === "/api/skill-updates/update-1" && method === "GET") {
      const resolved = update.plan.conflicts.every(conflict => update.drafts.some(choice => choice.part_id === conflict.part_id));
      update.resolved_check = { format_version, state: resolved ? "passed" : "held", code: resolved ? "resolved_writes_checked" : "review_decisions_required", reasons: [] };
      return reply(update);
    }
    if (path === "/api/skill-source-operations") return reply(state.operations.get(url.searchParams.get("request_key") ?? "") ?? { code: "missing" });
    if (path.startsWith("/api/skill-source-operations/")) return reply(state.operations.get(decodeURIComponent(path.split("/").at(-1)!)) ?? { code: "missing" });
    if (path.endsWith("/files")) {
      if (url.searchParams.get("preview") === "false") return route.fulfill({ status: 200, headers: { ...headers, "Content-Type": "application/octet-stream", "Content-Disposition": 'attachment; filename="guide.md"' }, body: "Fixture file" });
      return reply({ path: url.searchParams.get("path"), side: url.searchParams.get("side") ?? "discovery", size: 12, binary: false, text: "<script>window.bad = true</script>", truncated: false, byte_limit: 65536, line_limit: 1000, media_type: "text/plain" });
    }
    if (path === "/api/import" || path === "/api/import/directory") {
      state.uploadCount++;
      return reply({ upload_id: `upload-${state.uploadCount}`, filename: "fixture.zip", warnings: [], packages: [{ package_path: "bundle", revision: "local-revision" }], skills: [{ name: "Expense review", domain: "operations", source_ref: "bundle/SKILL.md", skill_id: "expense-review", is_new: false }] });
    }
    if (path === "/api/skill-source-discoveries" || path === "/api/skill-source-discoveries/discovery-1") {
      if (method === "DELETE") return route.fulfill({ status: 204, headers });
      if (method === "POST") state.discovery = { ...discovery, preselected_path: typeof body?.package_path === "string" ? body.package_path : null, resolved_ref: { ...ref, kind: (body?.ref_kind ?? "branch") as "branch" | "tag" | "commit", name: String(body?.ref_name ?? "main") } };
      if (method === "POST" && key) state.operations.set(key, { operation_id: key, discovery_id: discovery.discovery_id, state: "complete", committed: false, outcomes: [] });
      if (method === "POST" && options.loseDiscovery && !state.discoveryLost) { state.discoveryLost = true; return route.abort("failed"); }
      return reply(state.discovery);
    }
    if (method === "POST" && key) {
      const previous = state.operations.get(key);
      if (previous) return reply(previous);
      if (options.invalidOperation) return reply({ items: [], next_cursor: null });
      const action = path.split("/").at(-1)!;
      if (action === "check") { state.checkClaims++; if (state.checkClaims > 1) return reply({ message: "Source check was throttled", code: "source_busy" }, 429); }
      if (action === "draft") { update.drafts = body!.choices as SourceUpdateView["drafts"]; update.generation++; update.plan = { ...update.plan, fingerprint: { ...update.plan.fingerprint, update_generation: update.generation } }; }
      if (["apply", "skip", "adopt"].includes(action)) { const target = path.includes("/update-local/") ? localUpdate : update; target.status = action === "apply" ? "applied" : action === "skip" ? "skipped" : "adopted"; state.decisions.push(action); }
      if (action === "unlink" && binding.binding) binding.binding = { ...binding.binding, active: false };
      if (action === "retarget" && options.blockRetarget) return reply({ operation_id: key, state: "blocked", committed: false, outcomes: [{ skill_id: "expenses", state: "blocked", update_id: "update-1", code: "source_card_open" }] });
      if (action === "retarget" && binding.binding) {
        binding.generations = { ...binding.generations, binding: binding.generations.binding + 1 };
        binding.binding = { ...binding.binding, origin: { ...binding.binding.origin, generation: binding.generations.binding }, ref: body!.ref as typeof ref };
        binding.blocked_attempts = []; update.generation++;
        update.plan = { ...update.plan, origin: binding.binding.origin, incoming: { ...update.plan.incoming, origin: binding.binding.origin }, fingerprint: { ...update.plan.fingerprint, binding_generation: binding.generations.binding, update_generation: update.generation } };
      }
      if (["link", "relink"].includes(action) && originalBinding) { binding.generations = { ...binding.generations, binding: binding.generations.binding + 1 }; binding.binding = { ...originalBinding, baseline: null, source_id: String(body!.source_id), package_path: String(body!.package_path), origin: { ...originalBinding.origin, generation: binding.generations.binding }, ref: body!.ref as typeof ref, active: true }; }
      if (action === "bulk-apply") { update.status = "applied"; state.decisions.push("bulk-apply"); }
      if (action === "undo") state.undone = true;
      if (path === "/api/skill-local-imports") {
        if (options.blockLocal && update.status === "open") {
          binding.blocked_attempts = [...binding.blocked_attempts.filter(row => row.attempt_id !== "local-attempt"), { format_version, attempt_id: "local-attempt", origin: { ...origin, kind: "local" }, actor_id: "local-operator", reason: "Local upload refused", existing_update_id: "update-1", retry_needed: true }];
          const result: SourceOperation = { operation_id: key, state: "blocked", committed: false, outcomes: [{ skill_id: "expenses", state: "blocked", update_id: "update-1", code: "source_card_open" }] };
          state.operations.set(key, result); return reply(result);
        }
        binding.blocked_attempts = binding.blocked_attempts.filter(row => row.attempt_id !== "local-attempt");
        state.decisions.push("local-import");
        if (options.localReview) { const review: SourceOperation = { operation_id: key, state: "awaiting_review", committed: false, outcomes: [{ skill_id: skill.id, state: "awaiting_review", code: null, update_id: localUpdate.update_id }] }; state.operations.set(key, review); return reply(review); }
      }
      const result: SourceOperation = { operation_id: key, source_id: path === "/api/skill-sources" ? "source-1" : null, state: action === "check" ? "awaiting_review" : "complete", committed: action === "apply", outcomes: [{ skill_id: skill.id, state: action === "apply" ? "applied" : action === "check" ? "awaiting_review" : "unchanged", code: null, update_id: update.update_id }] };
      state.operations.set(key, result);
      if (path === "/api/skill-source-installations" && options.loseInstall && !state.installLost) { state.installLost = true; return route.abort("failed"); }
      if (path === "/api/skill-local-imports" && options.loseLocal && !state.localLost) { state.localLost = true; return route.abort("failed"); }
      if (action === "apply" && options.loseApply && !state.lost) { state.lost = true; return route.abort("failed"); }
      return reply(result);
    }
    state.violations.push(`${method} ${path}`);
    return reply({ code: "fixture_not_found", message: `${method} ${path}` }, 404);
  });
  return state;
}
