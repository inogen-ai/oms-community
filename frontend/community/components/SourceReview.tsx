"use client";
import { useEffect, useState } from "react";
import { applyReviewedChoices, type SourceChange, type SourceFilePreview, type SourceReviewStop } from "@inogen/oms-client";
import { Button, Notice, SkillDeleteConfirmation, SourceFileComparison, SourceUpdateCard, carrySourceDraft, sourceInstalledMessage, type SourceCardApply, type SourceCardDraft, type SourceCardFileView, type SourceFileSide, type SourceFileSidePreview } from "@inogen/oms-ui-core";
import { useWorkspace, useAction, Feedback, ResourceStatus, message } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";
import { saveSourceDownload } from "@/lib/sources";
import { SourceActionFeedback } from "./SourceOperations";

// Why the one-step Apply stopped after it saved the choices (ruling T6-2).
const STOPPED: Record<SourceReviewStop, string> = {
  changed: "Someone else changed the choices. Check them and try again.",
  held: "A safety check still holds these choices.",
  stale: "This card is out of date. Check safety again.",
  unavailable: "The card could not be read back. Try again.",
};

/**
 * One update's review card. `onInstalled` takes the confirmation ("brainstorming installed.") once the one-step
 * install or apply succeeded: the card then leaves, and a host whose list drops it shows the line where the reader
 * is. Without it the confirmation shows here, in the card's place.
 */
export default function SourceReview({ id, title, draft, onDraftChange, onInstalled }: { id: string; title?: string; draft?: SourceCardDraft; onDraftChange?: (draft?: SourceCardDraft) => void; onInstalled?: (message: string) => void }) {
  const { api, sources, sourceRevision, refreshSources } = useWorkspace();
  const resource = useSourceResource(() => sources!.client.update(id), [sources, id]);
  const [localDraft, setLocalDraft] = useState<SourceCardDraft | undefined>(undefined);
  const currentDraft = onDraftChange ? draft : localDraft;
  const changeDraft = (value?: SourceCardDraft) => onDraftChange ? onDraftChange(value) : setLocalDraft(value);
  const action = useSourceAction();
  const deletion = useAction();
  const [deleting, setDeleting] = useState<string | null>(null);
  const [stopped, setStopped] = useState<SourceReviewStop | null>(null);
  const [installed, setInstalled] = useState("");
  const skillName = title ?? resource.data?.plan.skill.skill_id ?? "";
  const stop = (reason: SourceReviewStop) => {
    setStopped(reason);
    // Show the choices someone else saved, so "Check them" means those.
    if (reason === "changed") changeDraft(undefined);
  };
  useEffect(() => {
    // A newer card keeps the local choices it still offers for the same part revision and drops the rest.
    const carried = carrySourceDraft(currentDraft, resource.data, resource.data?.part_fingerprints);
    if (carried !== currentDraft) changeDraft(carried);
  }, [resource.data, currentDraft]);
  // Follow-up C: the save, the read-back and the apply are one flow. It refreshes once, when it settles, so the
  // card is not read again in between; on success the card is done and is not shown again on its way out.
  function install(body: SourceCardApply) {
    const card = resource.data!;
    setStopped(null);
    void action.flow(async send => {
      const result = await applyReviewedChoices(sources!.client, send, id, body, { onStopped: stop });
      if (result?.state === "complete") {
        const confirmation = sourceInstalledMessage(card, title);
        changeDraft(undefined); setInstalled(confirmation); onInstalled?.(confirmation);
      }
      return result;
    });
  }
  if (installed) return onInstalled ? null : <Notice kind="success">{installed}</Notice>;
  return <><ResourceStatus resource={resource} /><SourceActionFeedback action={action} />
    {stopped && <Notice kind="error">{STOPPED[stopped]}</Notice>}
    {resource.data && <><SourceUpdateCard update={resource.data} title={title} draft={currentDraft} partFingerprints={resource.data.part_fingerprints} ownership={resource.data.ownership}
      busy={action.busy || resource.loading || deletion.busy} installing={action.flowing} onDraftChange={value => { setStopped(null); changeDraft(value); }}
      onApply={install}
      onSkip={body => { setStopped(null); void action.run(key => sources!.client.skip(id, body, key), JSON.stringify(["skip", id, body])); }}
      onAdopt={body => { setStopped(null); void action.run(key => sources!.client.adopt(id, body, key), JSON.stringify(["adopt", id, body])); }}
      onRecheck={body => { setStopped(null); void action.run(key => sources!.client.recheck(id, body, key), JSON.stringify(["recheck", id, body])); }}
      onDeleteSkill={resource.data.held_install ? () => setDeleting("") : undefined}
      renderFile={(change, view) => <SourceFiles key={change.part_id} id={id} change={resource.data!.plan.changes.find(row => row.part_id === change.part_id)!} revision={sourceRevision} view={view} />} />
      {deleting !== null && <SkillDeleteConfirmation name={skillName} confirmation={deleting} onConfirmationChange={setDeleting} busy={deletion.busy}
        onCancel={() => setDeleting(null)} onConfirm={() => {
          if (deleting !== skillName || deletion.busy || !resource.data) return;
          void deletion.run(() => api.deleteSkill(resource.data!.plan.skill.skill_id), "Skill deleted.", () => { setDeleting(null); refreshSources(); });
        }} />}
      {currentDraft && <Button variant="secondary" disabled={action.busy} onClick={() => changeDraft(undefined)}>Discard local choices</Button>}
    </>}
    {/* Outside the card's block: the deletion result stays readable after the deleted skill's card goes. */}
    <Feedback action={deletion} />
  </>;
}

// The card says which lines are flagged and which sides OMS has no copy of; an absent side is never fetched.
function SourceFiles({ id, change, revision, view }: { id: string; change: SourceChange; revision: number; view: SourceCardFileView }) {
  const { sources } = useWorkspace();
  const [error, setError] = useState("");
  const path = change.part_id.startsWith("file:") ? change.part_id.slice(5) : change.part_id;
  const sides = (["base", "local", "upstream"] as SourceFileSide[]).filter(side => !view.absent.includes(side));
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
  return <>{error && <Notice kind="error">{error}</Notice>}<SourceFileComparison path={path} {...view} base={value("base")} local={value("local")} upstream={value("upstream")} publicationWarning={publicationWarning}
    onDownload={(side, file) => { setError(""); void sources!.client.updateFile(id, { side, path: file }).then(result => saveSourceDownload(result.bytes, file)).catch(failure => setError(message(failure))); }} /></>;
}
