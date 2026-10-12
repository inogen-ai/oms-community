"use client";
import type { ComponentProps } from "react";
import Link from "next/link";
import { Notice, SourcePendingNotice, SourceStatus, sourcePendingSubject, type SourcePendingSubject } from "@inogen/oms-ui-core";
import { type PendingSourceRequest, type SourceOperation } from "@inogen/oms-client";
import { useWorkspace } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";

/** `summary="install"` adds the install counts; only the GitHub install screen passes it. */
export function SourceActionFeedback({ action, subject = "request", outcomeLink, summary }: { action: ReturnType<typeof useSourceAction>; subject?: SourcePendingSubject; outcomeLink?: ComponentProps<typeof SourceStatus>["outcomeLink"]; summary?: ComponentProps<typeof SourceStatus>["summary"] }) {
  // While a flow runs (the one-step install) the card's own line is the only progress text: not the busy line, not
  // the answer to its first request and not a "Check result" for a request still in flight (follow-up C).
  const flowing = action.flowing;
  return <>{action.error && <Notice kind="error">{action.error}</Notice>}{action.busy && !flowing && <p role="status">Processing source request…</p>}{action.operation && !flowing && <SourceStatus status={action.operation.state} operation={action.operation} outcomeLink={outcomeLink} summary={summary} />}
    {action.recoveryKey && !flowing && <SourcePendingNotice subject={subject} busy={action.busy} onCheck={() => void action.recover()}>
      {action.canRetryOriginalInput && <label className="oms-source-check"><input type="checkbox" disabled={action.busy} checked={action.retryOriginalInput} onChange={event => action.selectOriginalInputRetry(event.target.checked)} />Retry with the exact original input on the next submission</label>}
    </SourcePendingNotice>}</>;
}
/** Shows a "Check result" notice for each pending request this place can start; `match` narrows them (default: all).
 * Pass `summary="install"` only when `match` admits install requests alone. */
export function SourceRecovery({ match = () => true, summary }: { match?: (row: PendingSourceRequest) => boolean; summary?: ComponentProps<typeof SourceStatus>["summary"] }) {
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
    <SourceActionFeedback action={{ ...action, recoveryKey: null }} summary={summary} />
    {action.operation && <p className="muted">This is the recorded result of the original request. Current source state is refreshed separately.</p>}
    {action.operation && "discovery_id" in action.operation && typeof action.operation.discovery_id === "string" && <Link href={`/skills/?github=add&discovery=${encodeURIComponent(action.operation.discovery_id)}`}>Open recovered discovery</Link>}
  </>;
}
