"use client";
import Link from "next/link";
import { Notice, SourcePendingNotice, SourceStatus, sourcePendingSubject, type SourcePendingSubject } from "@inogen/oms-ui-core";
import { type PendingSourceRequest, type SourceOperation } from "@inogen/oms-client";
import { useWorkspace } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";

export function SourceActionFeedback({ action, subject = "request" }: { action: ReturnType<typeof useSourceAction>; subject?: SourcePendingSubject }) {
  return <>{action.error && <Notice kind="error">{action.error}</Notice>}{action.busy && <p role="status">Processing source request…</p>}{action.operation && <SourceStatus status={action.operation.state} operation={action.operation} />}
    {action.recoveryKey && <SourcePendingNotice subject={subject} busy={action.busy} onCheck={() => void action.recover()}>
      {action.canRetryOriginalInput && <label className="oms-source-check"><input type="checkbox" disabled={action.busy} checked={action.retryOriginalInput} onChange={event => action.selectOriginalInputRetry(event.target.checked)} />Retry with the exact original input on the next submission</label>}
    </SourcePendingNotice>}</>;
}
/** Shows a "Check result" notice for each pending request this place can start; `match` narrows them (default: all). */
export function SourceRecovery({ match = () => true }: { match?: (row: PendingSourceRequest) => boolean }) {
  const { sources } = useWorkspace();
  const pending = useSourceResource(() => sources!.requests.pending(), [sources]);
  const action = useSourceAction();
  if (!sources) return null;
  return <>{!sources.requests.persistent && <Notice>Session recovery storage is unavailable. Keep this page open while a source request is pending.</Notice>}
    {pending.error && <Notice kind="error">{pending.error}</Notice>}
    {pending.data?.filter(match).map(row => <SourcePendingNotice key={row.key} subject={sourcePendingSubject(row.path)} busy={action.busy} onCheck={() => void action.run(async () => {
      const result = await sources.requests.recover(row.key);
      if (!result || !("state" in result)) throw new Error("The recovered request is not an operation.");
      return result as SourceOperation;
    }, `recover:${row.key}`)}>
      {row.requires_body && !row.operation_id && <p>Private input was not stored. Recovery checks the server receipt using the original request identity.</p>}
    </SourcePendingNotice>)}
    <SourceActionFeedback action={{ ...action, recoveryKey: null }} />
    {action.operation && <p className="muted">This is the recorded result of the original request. Current source state is refreshed separately.</p>}
    {action.operation && "discovery_id" in action.operation && typeof action.operation.discovery_id === "string" && <Link href={`/skills/?github=add&discovery=${encodeURIComponent(action.operation.discovery_id)}`}>Open recovered discovery</Link>}
  </>;
}
