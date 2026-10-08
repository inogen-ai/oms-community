"use client";
import { useState } from "react";
import Link from "next/link";
import { Button, EmptyState, Notice, Panel, SourceStatus } from "@inogen/oms-ui-core";
import { ResourceStatus, useWorkspace } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";
import { sourcePages } from "@/lib/sources";
import { SourceActionFeedback } from "./SourceOperations";
import SkillSourcePanel from "./SkillSourcePanel";

export default function SourceDetail({ id }: { id: string }) {
  const { sources, api, sourceRevision } = useWorkspace();
  const resource = useSourceResource(async () => {
    const first = await sources!.client.source(id);
    const bindings = await sourcePages(async cursor => cursor ? (await sources!.client.source(id, { cursor })).bindings : first.bindings);
    const skills = await api.skills();
    return { source: first.source, bindings, skills };
  }, [sources, api, id]);
  const action = useSourceAction();
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [destination, setDestination] = useState(""), [confirmRelocation, setConfirmRelocation] = useState(false);
  let validDestination = false;
  try { const url = new URL(destination); validDestination = url.protocol === "https:" && url.hostname === "github.com" && !url.port && !url.username && !url.password && !url.search && !url.hash && /^\/[^/]+\/[^/]+\/?$/.test(url.pathname); } catch { /* Incomplete input. */ }
  const data = resource.data;
  async function check() {
    if (!data) return;
    const body = { expected_source_generation: data.source.generation, skill_ids: data.bindings.map(binding => binding.origin.skill.skill_id) };
    await action.run(key => sources!.client.check(id, body, key), JSON.stringify(["check", id, body]));
  }
  async function remove() {
    if (!data || !confirmRemove) return;
    await action.run(async key => {
      const views = await Promise.all(data.bindings.map(binding => sources!.client.binding(binding.origin.skill.skill_id)));
      const body = { expected_source_generation: data.source.generation, expected_generations: views.map(view => ({ skill_id: view.generations.skill.skill_id, content: view.generations.content, binding: view.generations.binding })) };
      return sources!.client.removeSource(id, body, key);
    }, JSON.stringify(["remove", id, data.source.generation]));
  }
  return <Panel title="Source details" className="oms-source-details"><Link href="/sources">All sources</Link><ResourceStatus resource={resource} /><SourceActionFeedback action={action} />
    {data && <><h3>{data.source.canonical_url}</h3><SourceStatus status={data.source.status ?? undefined} />
      <div className="oms-source-actions"><Button disabled={action.busy || resource.loading || !data.bindings.length} onClick={() => void check()}>Check source now</Button></div>
      {!data.bindings.length && <EmptyState title="No linked skills" />}
      {data.bindings.map(binding => <SkillSourcePanel key={binding.origin.skill.skill_id} skill={data.skills.find(skill => skill.id === binding.origin.skill.skill_id) ?? { id: binding.origin.skill.skill_id, name: binding.origin.skill.skill_id }} />)}
      <details><summary>Change repository location</summary><p>Confirm a repository relocation. This changes the source endpoint and retains current content, baselines, pinned commits, cards and drafts. The next Check fetches from the new location.</p>
        <label>New GitHub repository URL<input aria-label="New GitHub repository URL" type="url" value={destination} onChange={event => { setDestination(event.target.value); setConfirmRelocation(false); }} /></label>
        <label className="oms-source-check"><input type="checkbox" checked={confirmRelocation} disabled={!validDestination || action.busy} onChange={event => setConfirmRelocation(event.target.checked)} />Confirm this repository moved to the new location</label>
        <Button disabled={!validDestination || !confirmRelocation || action.busy || resource.loading} onClick={() => void action.run(async key => {
          const views = await Promise.all(data.bindings.map(binding => sources!.client.binding(binding.origin.skill.skill_id)));
          return sources!.client.relocateSource(id, { destination_url: destination, expected_source_generation: data.source.generation,
            expected_generations: views.map(view => ({ skill_id: view.generations.skill.skill_id, content: view.generations.content, binding: view.generations.binding })) }, key);
        }, JSON.stringify(["relocate", id, destination, data.source.generation]))}>Confirm repository relocation</Button>
      </details>
      <details><summary>Remove source tracking</summary><Notice>Removing this source preserves skills, local package baselines and history. Its bindings and open source cards close.</Notice><label className="oms-source-check"><input type="checkbox" checked={confirmRemove} onChange={event => setConfirmRemove(event.target.checked)} disabled={action.busy} />Confirm removing this source</label><Button variant="danger" disabled={!confirmRemove || action.busy || resource.loading} onClick={() => void remove()}>Remove source</Button></details>
    </>}
  </Panel>;
}
