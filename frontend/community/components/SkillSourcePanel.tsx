"use client";
import { useState } from "react";
import Link from "next/link";
import type { Skill, SourceDiscovery, SourceBindingView, ResolvedSourceRef } from "@inogen/oms-client";
import { Button, EmptyState, Notice, Panel, SourceStatus } from "@inogen/oms-ui-core";
import { ResourceStatus, useWorkspace, message } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";
import SourceDiscoveryForm from "./SourceDiscoveryForm";
import { SourceActionFeedback } from "./SourceOperations";

export default function SkillSourcePanel({ skill, startEditing = false }: { skill: Pick<Skill, "id" | "name">; startEditing?: boolean }) {
  const { sources } = useWorkspace();
  const resource = useSourceResource(() => sources!.client.binding(skill.id), [sources, skill.id]);
  const action = useSourceAction();
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(startEditing), [confirmUnlink, setConfirmUnlink] = useState(false);
  const view = resource.data, binding = view?.binding;
  async function check() {
    if (!binding) return;
    const source = await sources!.client.source(binding.source_id);
    const body = { expected_source_generation: source.source.generation, skill_ids: [skill.id] };
    await action.run(key => sources!.client.check(binding.source_id, body, key), JSON.stringify(["check", binding.source_id, body]));
  }
  return <Panel title={`Source tracking · ${skill.name}`} className="oms-source-details"><ResourceStatus resource={resource} /><SourceActionFeedback action={action} />{error && <Notice kind="error">{error}</Notice>}
    {view && <><SourceStatus status={binding && !binding.active ? "unlinked" : binding?.status ?? "unchecked"} ref={binding?.ref} firstReconciliation={!!binding?.active && binding.first_reconciliation} retryNeeded={view.blocked_attempts.some(attempt => attempt.retry_needed)}
      message={view.blocked_attempts.filter(attempt => attempt.retry_needed).map(attempt => attempt.reason.replaceAll("_", " ")).join("; ")} />
      {binding?.active ? <><p><Link href={`/import/?github=sources&source=${encodeURIComponent(binding.source_id)}`}>Open tracked source</Link> · Folder: {binding.package_path || "repository root"}</p>
        <div className="oms-source-actions"><Button disabled={action.busy || resource.loading} onClick={() => void check().catch(failure => setError(message(failure)))}>Check this skill now</Button><Button variant="secondary" disabled={action.busy} aria-expanded={editing} onClick={() => setEditing(value => !value)}>Change tracked ref or folder</Button></div>
        <details><summary>Unlink GitHub source</summary><p>Keep the skill, local package baseline and history. The GitHub binding and its open card close.</p><label className="oms-source-check"><input type="checkbox" checked={confirmUnlink} disabled={action.busy} onChange={event => setConfirmUnlink(event.target.checked)} />Confirm unlinking this skill</label>
          <Button variant="danger" disabled={!confirmUnlink || action.busy || resource.loading} onClick={() => { const body = { expected_content_generation: view.generations.content, expected_binding_generation: view.generations.binding }; void action.run(key => sources!.client.unlink(skill.id, body, key), JSON.stringify(["unlink", skill.id, body])); }}>Unlink source</Button></details>
      </> : <><EmptyState title="No active GitHub binding">An earlier import can be linked after its origin is verified. Locally authored skills cannot acquire invented source history.</EmptyState><Button variant="secondary" aria-expanded={editing} onClick={() => setEditing(value => !value)}>{binding ? "Relink source" : "Link a GitHub source"}</Button></>}
      {editing && <BindingTarget key={`${skill.id}:${binding?.active ? "retarget" : "link"}`} skillId={skill.id} view={view} />}
    </>}
  </Panel>;
}

function BindingTarget({ skillId, view }: { skillId: string; view: SourceBindingView }) {
  const { sources } = useWorkspace();
  const [discovery, setDiscovery] = useState<SourceDiscovery | null>(null), [selectedPath, setSelectedPath] = useState<string | null>(null);
  const action = useSourceAction();
  const binding = view.binding;
  const folderChanged = !!binding && selectedPath !== null && selectedPath !== binding.package_path;
  const differentRepository = !!discovery && !!binding && discovery.resolved_ref.canonical_url !== binding.ref.canonical_url;
  const [error, setError] = useState("");
  async function submit() {
    if (!discovery || selectedPath === null || differentRepository) return;
    const chosen = discovery.packages.items.find(pkg => pkg.path === selectedPath && pkg.valid);
    if (!chosen) return;
    const ref: ResolvedSourceRef = { ...discovery.resolved_ref, package_path: chosen.path };
    const guard = { expected_content_generation: view.generations.content, expected_binding_generation: view.generations.binding };
    if (binding && (!binding.active || folderChanged)) {
      const body = { ...guard, source_id: binding.source_id, package_path: chosen.path, ref };
      await action.run(key => sources!.client.relink(skillId, body, key), JSON.stringify(["relink", skillId, body]));
    } else if (binding?.active) {
      const body = { ...guard, ref };
      await action.run(key => sources!.client.retarget(skillId, body, key), JSON.stringify(["retarget", skillId, body]));
    } else {
      const result = await action.run(key => sources!.client.createSource({ discovery_id: discovery.discovery_id }, key), JSON.stringify(["create-source", discovery.discovery_id]));
      const sourceId = result?.source_id;
      if (!sourceId) return;
      const body = { ...guard, ref, source_id: sourceId, package_path: chosen.path };
      await action.run(key => sources!.client.link(skillId, body, key), JSON.stringify(["link", skillId, body]));
    }
  }
  return <><p>Choose a repository, ref and folder. The current binding stays in place until the change commits. Ref and folder changes require first reconciliation.</p>
    <SourceDiscoveryForm initialUrl={binding?.ref.canonical_url} onDiscovered={value => { setDiscovery(value); setSelectedPath(value?.preselected_path ?? null); }} />
    {discovery && <fieldset disabled={action.busy}><legend>Choose the tracked skill folder</legend>{discovery.packages.items.map(pkg => <label className="oms-source-check" key={pkg.path}><input type="radio" name={`binding-${skillId}`} value={pkg.path} disabled={!pkg.valid} checked={selectedPath === pkg.path} onChange={() => setSelectedPath(pkg.path)} />{pkg.path || "repository root"}{!pkg.valid ? ` — ${pkg.reasons.join("; ")}` : ""}</label>)}</fieldset>}
    <SourceActionFeedback action={action} />{error && <Notice kind="error">{error}</Notice>}
    {differentRepository && <Notice>Ref and folder changes must stay within the currently tracked repository.</Notice>}
    <div className="oms-source-actions"><Button disabled={action.busy || selectedPath === null || !discovery || (differentRepository)} onClick={() => void submit().catch(failure => setError(message(failure)))}>{binding?.active ? folderChanged ? "Confirm folder relink" : "Retarget source" : binding ? "Confirm relink" : "Confirm link"}</Button></div>
  </>;
}
