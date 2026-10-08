"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import type { SourceDiscovery as Discovery, InstallSourceBody } from "@inogen/oms-client";
import { Button, EmptyState, Notice, Panel, SourceDiscovery, SourceStatus, type SourceSelection } from "@inogen/oms-ui-core";
import { useWorkspace, ResourceStatus, message } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";
import { sourcePages } from "@/lib/sources";
import { PageHeading } from "./Shell";
import SourceDiscoveryForm from "./SourceDiscoveryForm";
import SourceDetail from "./SourceDetail";
import SourceUpdates from "./SourceUpdates";
import { SourceActionFeedback, SourceRecovery } from "./SourceOperations";

export default function Sources() {
  const { capabilities } = useWorkspace();
  const search = useSearchParams();
  if (!capabilities.github_skill_sources) return <><PageHeading title="Sources" /><Notice>Source tracking is unavailable on this server.</Notice></>;
  return <SourceWorkspace sourceId={search.get("source")} updateId={search.get("update")} discoveryId={search.get("discovery")} />;
}
function SourceWorkspace({ sourceId, updateId, discoveryId }: { sourceId: string | null; updateId: string | null; discoveryId: string | null }) {
  const { sources, api, sourceRevision } = useWorkspace();
  const list = useSourceResource(() => sourcePages(cursor => sources!.client.sources({ cursor })), [sources]);
  const skills = useSourceResource(() => api.skills(), [api]);
  const [discovering, setDiscovering] = useState(false), [discovery, setDiscovery] = useState<Discovery | null>(null);
  const [selections, setSelections] = useState<SourceSelection[] | undefined>(undefined);
  const action = useSourceAction();
  const [error, setError] = useState("");
  const domains = [...new Set(["general", ...(skills.data ?? []).map(skill => skill.domain || "general")])];
  useEffect(() => {
    let active = true;
    if (discoveryId) void sources!.client.discovery(discoveryId).then(value => { if (active) { setDiscovery(value); setSelections(undefined); } }).catch(failure => { if (active) setError(message(failure)); });
    return () => { active = false; };
  }, [sources, discoveryId]);
  function discovered(value: Discovery | null) { setDiscovery(value); setSelections(undefined); }
  async function install(body: InstallSourceBody) {
    const result = await action.run(key => sources!.client.install(body, key), JSON.stringify(["install", body]));
    if (result?.committed) setSelections([]);
  }
  return <div className="oms-source-workspace"><PageHeading title="Sources" description="Discover skill repositories and review updates before applying them. Publishing remains separate."><Button variant="secondary" aria-expanded={discovering} onClick={() => setDiscovering(value => !value)}>Add a GitHub source</Button></PageHeading>
    <SourceRecovery /><SourceActionFeedback action={action} />{error && <Notice kind="error">{error}</Notice>}
    {discovering && <SourceDiscoveryForm onDiscovered={discovered} />}
    {discovery && <SourceDiscovery discovery={discovery} selections={selections} domains={domains} defaultDomain="general" existingNames={(skills.data ?? []).flatMap(skill => [skill.id, skill.name])}
      onSelectionsChange={setSelections} onInstall={body => void install(body)} busy={action.busy} onLoadMore={cursor => {
        void sources!.client.discovery(discovery.discovery_id, { cursor }).then(next => setDiscovery(current => current?.discovery_id === next.discovery_id ? { ...next, packages: { ...next.packages, items: [...current.packages.items, ...next.packages.items] } } : current)).catch(failure => setError(message(failure)));
      }} />}
    {sourceId ? <SourceDetail key={`${sources?.scope}:${sourceId}`} id={sourceId} /> : <Panel title="Tracked repositories"><ResourceStatus resource={list} />
      {list.data?.length === 0 && <EmptyState title="No tracked repositories">Discover a repository above, or link an earlier import from its skill page.</EmptyState>}
      {list.data?.map(source => <article className="record" key={source.source_id}><h3><Link href={`/sources?source=${encodeURIComponent(source.source_id)}`}>{source.canonical_url}</Link></h3><SourceStatus status={source.status ?? undefined} /></article>)}
    </Panel>}
    <SourceUpdates selectedId={updateId ?? undefined} />
  </div>;
}
