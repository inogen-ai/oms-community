"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { Button, EmptyState, Notice, Panel, SourceStatus, sourceBulkExclusions, sourceUpdateScope, type SourceCardDraft } from "@inogen/oms-ui-core";
import { useWorkspace, ResourceStatus } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";
import { type SourceOperation } from "@inogen/oms-client";
import { sourcePages, updateTarget } from "@/lib/sources";
import SourceReview from "./SourceReview";
import { SourceActionFeedback } from "./SourceOperations";

/** Open source updates, optionally only those from GitHub or only those from a re-imported local package. */
export default function SourceUpdates({ selectedId, skillId, origin, headingless }: { selectedId?: string; skillId?: string; origin?: "github" | "local"; headingless?: boolean }) {
  const { sources, api, sourceRevision } = useWorkspace();
  const resource = useSourceResource(async () => ({ updates: await sourcePages(cursor => sources!.client.updates({ cursor })), skills: await api.skills() }), [sources, api]);
  const [open, setOpen] = useState<string | null>(selectedId ?? null);
  useEffect(() => { if (selectedId) setOpen(selectedId); }, [selectedId]);
  const [drafts, setDrafts] = useState<Record<string, SourceCardDraft>>({});
  const action = useSourceAction();
  const [batchResults, setBatchResults] = useState<{ scope: string; operation: SourceOperation }[]>([]);
  useEffect(() => {
    if (action.operation && sources) setBatchResults(current => [
      ...current.filter(row => row.scope !== sources.scope || row.operation.operation_id !== action.operation!.operation_id),
      { scope: sources.scope, operation: action.operation! },
    ]);
  }, [action.operation, sources]);
  const openUpdates = (resource.data?.updates ?? []).filter(update => !skillId || update.plan.skill.skill_id === skillId);
  const { shown: updates, elsewhere, intro, empty } = sourceUpdateScope(openUpdates, { origin, selectedId });
  const clean = updates.filter(update => !sourceBulkExclusions(update).length);
  const excluded = updates.filter(update => sourceBulkExclusions(update).length).map(update => ({ skill_id: update.plan.skill.skill_id, reasons: sourceBulkExclusions(update) }));
  const names = new Map(resource.data?.skills.map(skill => [skill.id, skill.name]));
  async function bulk() {
    for (let offset = 0; offset < clean.length; offset += 100) {
      const body = { updates: clean.slice(offset, offset + 100).map(update => ({ update_id: update.update_id, skill_id: update.plan.skill.skill_id, fingerprint: update.plan.fingerprint, removal_consents: [] })) };
      const result = await action.run(key => sources!.client.bulkApply(body, key), JSON.stringify(["bulk", body]));
      if (result) setBatchResults(current => [...current.filter(row => row.scope !== sources!.scope || row.operation.operation_id !== result.operation_id), { scope: sources!.scope, operation: result }]);
      if (!result || ["fetching", "planning", "applying"].includes(result.state)) break;
    }
  }
  if (!sources) return null;
  return <section className="compact oms-source-details" aria-label="Source update review">{!headingless && <h2>Source updates</h2>}{intro && <p className="muted">{intro}</p>}<ResourceStatus resource={resource} />
    {!resource.loading && !resource.error && !updates.length && <EmptyState title="No source updates waiting">{empty}</EmptyState>}
    {updates.length > 0 && <><Button disabled={action.busy || resource.loading || !clean.length} onClick={() => void bulk()}>Apply all clean updates</Button><p className="muted">{clean.length} eligible. Each skill commits separately; failed peers do not undo successful updates.</p><SourceStatus exclusions={excluded} /></>}
    <SourceActionFeedback action={{ ...action, operation: null }} />
    {batchResults.filter(row => row.scope === sources.scope).map((row, index) => <SourceStatus key={`${row.operation.operation_id}:${index}`} operation={row.operation} status={row.operation.state} />)}
    <div className="stack">{updates.map(update => <article key={update.update_id} className="record">
      <div className="record-heading"><h3>{names.get(update.plan.skill.skill_id) || update.plan.skill.skill_id}</h3><Button variant="secondary" aria-expanded={open === update.update_id} onClick={() => setOpen(current => current === update.update_id ? null : update.update_id)}>Review source update</Button></div>
      <p>{update.plan.origin.kind === "github" ? "GitHub source" : "Local package"}{drafts[update.update_id] ? " · Draft retained" : ""}</p>
      <Link href={updateTarget(update.plan.origin.kind, update.update_id)}>Copyable update link</Link>
      {open === update.update_id && <SourceReview id={update.update_id} title={names.get(update.plan.skill.skill_id)} draft={drafts[update.update_id]} onDraftChange={draft => setDrafts(current => {
        const next = { ...current }; if (draft) next[update.update_id] = draft; else delete next[update.update_id]; return next;
      })} />}
    </article>)}</div>
    {elsewhere && <Notice>This update is for the other kind of source. <Link href={updateTarget(elsewhere.plan.origin.kind, elsewhere.update_id)}>Open it where it belongs</Link>.</Notice>}
    {selectedId && !resource.loading && !elsewhere && !updates.some(update => update.update_id === selectedId) && <><Notice>This update is no longer in the open queue. Its recorded state is shown below.</Notice><SourceReview id={selectedId} /></>}
  </section>;
}
