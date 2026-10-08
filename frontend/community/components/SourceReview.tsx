"use client";
import { useEffect, useState } from "react";
import type { SourceChange, SourceFilePreview } from "@inogen/oms-client";
import { Button, Notice, SourceFileComparison, SourceUpdateCard, type SourceCardDraft, type SourceFileSide, type SourceFileSidePreview } from "@inogen/oms-ui-core";
import { useWorkspace, ResourceStatus, message } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";
import { saveSourceDownload } from "@/lib/sources";
import { SourceActionFeedback } from "./SourceOperations";

export default function SourceReview({ id, title, draft, onDraftChange }: { id: string; title?: string; draft?: SourceCardDraft; onDraftChange?: (draft?: SourceCardDraft) => void }) {
  const { sources, sourceRevision } = useWorkspace();
  const resource = useSourceResource(() => sources!.client.update(id), [sources, id]);
  const [localDraft, setLocalDraft] = useState<SourceCardDraft | undefined>(undefined);
  const currentDraft = onDraftChange ? draft : localDraft;
  const changeDraft = (value?: SourceCardDraft) => onDraftChange ? onDraftChange(value) : setLocalDraft(value);
  const action = useSourceAction();
  useEffect(() => {
    const update = resource.data;
    if (!currentDraft || !update || update.stale || JSON.stringify(currentDraft.fingerprint) === JSON.stringify(update.plan.fingerprint)) return;
    const valid = currentDraft.choices.every(choice => update.part_fingerprints[choice.part_id] === choice.part_fingerprint
      && (!update.plan.conflicts.some(conflict => conflict.part_id === choice.part_id)
        || update.plan.conflicts.some(conflict => conflict.part_id === choice.part_id && conflict.allowed_choices.includes(choice.choice))));
    if (valid) changeDraft({ ...currentDraft, fingerprint: update.plan.fingerprint, removal_consents: [] });
  // Retain local prose only when the server proves the same part and permitted choice.
  }, [resource.data, currentDraft]);
  return <><ResourceStatus resource={resource} /><SourceActionFeedback action={action} />
    {resource.data && <><SourceUpdateCard update={resource.data} title={title} draft={currentDraft} partFingerprints={resource.data.part_fingerprints} ownership={resource.data.ownership}
      busy={action.busy || resource.loading} onDraftChange={changeDraft}
      onSaveDraft={body => void action.run(key => sources!.client.saveDraft(id, body, key), JSON.stringify(["draft", id, body]))}
      onApply={body => void action.run(key => sources!.client.apply(id, body, key), JSON.stringify(["apply", id, body]))}
      onSkip={body => void action.run(key => sources!.client.skip(id, body, key), JSON.stringify(["skip", id, body]))}
      onAdopt={body => void action.run(key => sources!.client.adopt(id, body, key), JSON.stringify(["adopt", id, body]))}
      onRecheck={body => void action.run(key => sources!.client.recheck(id, body, key), JSON.stringify(["recheck", id, body]))}
      renderFile={change => <SourceFiles key={change.part_id} id={id} change={resource.data!.plan.changes.find(row => row.part_id === change.part_id)!} revision={sourceRevision} />} />
      {currentDraft && <Button variant="secondary" disabled={action.busy} onClick={() => changeDraft(undefined)}>Discard local choices</Button>}
    </>}
  </>;
}

function SourceFiles({ id, change, revision }: { id: string; change: SourceChange; revision: number }) {
  const { sources } = useWorkspace();
  const [error, setError] = useState("");
  const path = change.part_id.startsWith("file:") ? change.part_id.slice(5) : change.part_id;
  const sides: SourceFileSide[] = ["base", "local", "upstream"];
  const resource = useSourceResource(async () => {
    const results = await Promise.allSettled(sides.map(side => sources!.client.updateFile(id, { side, path, preview: true })));
    return Object.fromEntries(results.map((result, index) => [sides[index], result.status === "fulfilled" ? result.value : { unavailable: message(result.reason) }])) as Record<SourceFileSide, SourceFilePreview | SourceFileSidePreview>;
  }, [sources, id, path]);
  const value = (side: SourceFileSide): SourceFileSidePreview => {
    const evidence = side === "upstream" ? change.incoming : change[side];
    const metadata = evidence.value && typeof evidence.value === "object" && !Array.isArray(evidence.value) ? evidence.value : null;
    return { ...(resource.data?.[side] ?? { loading: resource.loading }), ...(metadata && typeof metadata.digest === "string" ? { digest: metadata.digest } : {}) };
  };
  const incoming = change.incoming.value;
  const publicationWarning = incoming && typeof incoming === "object" && !Array.isArray(incoming) && incoming.published === false
    ? `Retained but excluded from publication${typeof incoming.exclusion_reason === "string" ? `: ${incoming.exclusion_reason}` : "."}` : undefined;
  return <>{error && <Notice kind="error">{error}</Notice>}<SourceFileComparison path={path} base={value("base")} local={value("local")} upstream={value("upstream")} publicationWarning={publicationWarning}
    onDownload={(side, file) => { setError(""); void sources!.client.updateFile(id, { side, path: file }).then(result => saveSourceDownload(result.bytes, file)).catch(failure => setError(message(failure))); }} /></>;
}
