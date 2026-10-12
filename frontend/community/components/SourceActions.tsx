"use client";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { Skill, SkillSource, SourceBinding } from "@inogen/oms-client";
import { Button, Notice, SourceChangeConfirmation, SourceStatus, sourceDisplayUrl } from "@inogen/oms-ui-core";
import { ResourceStatus, useWorkspace } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";
import { sourcePages } from "@/lib/sources";
import { SourceActionFeedback } from "./SourceOperations";

type Loaded = { source: SkillSource; bindings: SourceBinding[]; skills: Skill[] };

function githubRepository(value: string): boolean {
  try { const url = new URL(value); return url.protocol === "https:" && url.hostname === "github.com" && !url.port && !url.username && !url.password && !url.search && !url.hash && /^\/[^/]+\/[^/]+\/?$/.test(url.pathname); }
  catch { return false; /* Incomplete input. */ }
}

/** Repository-level actions: check, change repository URL and remove. Remove and move ask for confirmation first. */
export default function SourceActions({ sourceId: id, children }: { sourceId: string; children?: (data: Loaded) => ReactNode }) {
  const { sources, api } = useWorkspace();
  const resource = useSourceResource(async () => {
    const first = await sources!.client.source(id);
    const bindings = await sourcePages(async cursor => cursor ? (await sources!.client.source(id, { cursor })).bindings : first.bindings);
    const skills = await api.skills();
    return { source: first.source, bindings, skills };
  }, [sources, api, id]);
  const action = useSourceAction();
  const [destination, setDestination] = useState("");
  const [confirm, setConfirm] = useState<"relocate" | "remove" | null>(null);
  const triggers = { relocate: useRef<HTMLButtonElement>(null), remove: useRef<HTMLButtonElement>(null) };
  const returnFocus = useRef<"relocate" | "remove" | null>(null);
  // After a confirmed change settles, focus goes back to the button that asked for it.
  useEffect(() => {
    if (!action.busy && !resource.loading && returnFocus.current) { triggers[returnFocus.current].current?.focus(); returnFocus.current = null; }
  }, [action.busy, resource.loading]);
  const data = resource.data;
  const validDestination = githubRepository(destination);
  const guards = async (loaded: Loaded) => (await Promise.all(loaded.bindings.map(binding => sources!.client.binding(binding.origin.skill.skill_id))))
    .map(view => ({ skill_id: view.generations.skill.skill_id, content: view.generations.content, binding: view.generations.binding }));
  function ask(kind: "relocate" | "remove") { setConfirm(kind); }
  function close() { const kind = confirm; setConfirm(null); if (kind) triggers[kind].current?.focus(); }
  async function check(loaded: Loaded) {
    const body = { expected_source_generation: loaded.source.generation, skill_ids: loaded.bindings.map(binding => binding.origin.skill.skill_id) };
    await action.run(key => sources!.client.check(id, body, key), JSON.stringify(["check", id, body]));
  }
  async function remove(loaded: Loaded) {
    await action.run(async key => sources!.client.removeSource(id, { expected_source_generation: loaded.source.generation, expected_generations: await guards(loaded) }, key),
      JSON.stringify(["remove", id, loaded.source.generation]));
  }
  async function relocate(loaded: Loaded) {
    await action.run(async key => sources!.client.relocateSource(id, { destination_url: destination, expected_source_generation: loaded.source.generation,
      expected_generations: await guards(loaded) }, key), JSON.stringify(["relocate", id, destination, loaded.source.generation]));
  }
  const names = (loaded: Loaded) => loaded.bindings.map(binding => { const skill = binding.origin.skill.skill_id; return { id: skill, name: loaded.skills.find(row => row.id === skill)?.name ?? skill }; });
  return <><ResourceStatus resource={resource} /><SourceActionFeedback action={action} />
    {data && <><h3>{sourceDisplayUrl(data.source.canonical_url)}</h3><SourceStatus status={data.source.status ?? undefined} />
      <div className="oms-source-actions"><Button disabled={action.busy || resource.loading || !data.bindings.length} onClick={() => void check(data)}>Check source now</Button></div>
      {children?.(data)}
      <details><summary>Change repository location</summary><p>This changes the source endpoint and retains current content, baselines, pinned commits, cards and drafts. The next Check fetches from the new location.</p>
        <label>New GitHub repository URL<input aria-label="New GitHub repository URL" type="url" value={destination} onChange={event => { setDestination(event.target.value); setConfirm(null); }} /></label>
        <Button ref={triggers.relocate} variant="secondary" disabled={!validDestination || action.busy || resource.loading} onClick={() => ask("relocate")}>Change repository URL</Button>
        {confirm === "relocate" && <SourceChangeConfirmation action="relocate" destination={destination} skills={names(data)} busy={action.busy} onCancel={close} onConfirm={() => { returnFocus.current = "relocate"; setConfirm(null); void relocate(data); }} />}
      </details>
      <details><summary>Remove source tracking</summary><Notice>Removing this source preserves skills, local package baselines and history. Its bindings and open source cards close.</Notice>
        <Button ref={triggers.remove} variant="danger" disabled={action.busy || resource.loading} onClick={() => ask("remove")}>Remove source</Button>
        {confirm === "remove" && <SourceChangeConfirmation action="remove" skills={names(data)} busy={action.busy} onCancel={close} onConfirm={() => { returnFocus.current = "remove"; setConfirm(null); void remove(data); }} />}
      </details>
    </>}
  </>;
}
