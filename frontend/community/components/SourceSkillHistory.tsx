"use client";
import { useState } from "react";
import type { SourceHistoryEntry } from "@inogen/oms-client";
import { EmptyState, Notice, Panel, SourceUndo } from "@inogen/oms-ui-core";
import { HistoryTimeline } from "@inogen/oms-ui-core/history";
import { ResourceStatus, useWorkspace } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";
import { sourcePages } from "@/lib/sources";
import { SourceActionFeedback } from "./SourceOperations";

type HistoryRow = SourceHistoryEntry;
export default function SourceSkillHistory({ skillId, dirty = false }: { skillId: string; dirty?: boolean }) {
  const { sources } = useWorkspace();
  const resource = useSourceResource<HistoryRow[]>(() => sourcePages(cursor => sources!.client.history(skillId, { cursor })), [sources, skillId]);
  const action = useSourceAction();
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({});
  return <Panel title="Source and attachment history"><p>Source history includes attachment changes. Restoring an ordinary document is a local edit; it does not rewind source baselines or restore supporting files.</p>
    <ResourceStatus resource={resource} /><SourceActionFeedback action={action} />{dirty && <Notice>Save or discard your document edits before undoing a source update.</Notice>}
    {resource.data?.length === 0 && <EmptyState title="No source history recorded" />}
    {resource.data && <HistoryTimeline entries={resource.data} />}
    {resource.data?.filter(row => row.undo).map(row => {
      const undo = row.undo!;
      return <SourceUndo key={undo.undo_id} eligible={undo.available && !resource.loading && !dirty} reason={undo.reasons.map(reason => reason.replaceAll("_", " ")).join("; ")}
        guard={{ undo_id: undo.undo_id, expected_generations: undo.expected_generations, policy_version: undo.policy_version }} busy={action.busy}
        confirmed={confirmed[undo.undo_id] ?? false} onConfirmedChange={value => setConfirmed(current => ({ ...current, [undo.undo_id]: value }))}
        onUndo={guard => { const body = { ...guard, skill_id: undo.skill_id }; void action.run(key => sources!.client.undo(undo.update_id, body, key), JSON.stringify(["undo", undo.update_id, body])); }} />;
    })}
  </Panel>;
}
