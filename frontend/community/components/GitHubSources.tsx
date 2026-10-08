"use client";
import Link from "next/link";
import { Button, EmptyState, Panel, SourceStatus, sourceDisplayUrl } from "@inogen/oms-ui-core";
import { ResourceStatus, useWorkspace } from "@/lib/workspace";
import { useSourceResource } from "@/lib/source-state";
import { sourcePages } from "@/lib/sources";
import SourceDetail from "./SourceDetail";

/** The tracked repositories card: the list, or one repository's detail with its actions. Close sits inside the card. */
export default function GitHubSources({ sourceId, onClose }: { sourceId: string | null; onClose: () => void }) {
  const { sources } = useWorkspace();
  const list = useSourceResource(() => sourcePages(cursor => sources!.client.sources({ cursor })), [sources]);
  return <Panel title="Tracked repositories">
    {sourceId ? <SourceDetail key={`${sources?.scope}:${sourceId}`} id={sourceId} /> : <>
      <ResourceStatus resource={list} />
      {list.data?.length === 0 && <EmptyState title="No tracked repositories">Add a GitHub source, or link an earlier import from its skill page.</EmptyState>}
      {list.data?.map(source => <article className="record" key={source.source_id}><h3><Link href={`/import/?github=sources&source=${encodeURIComponent(source.source_id)}`}>{sourceDisplayUrl(source.canonical_url)}</Link></h3><SourceStatus status={source.status ?? undefined} /></article>)}
    </>}
    <div className="oms-panel-footer"><Button variant="secondary" onClick={onClose}>Close</Button></div>
  </Panel>;
}
