"use client";
import Link from "next/link";
import { Button, Notice, Panel, SourceStatus } from "@inogen/oms-ui-core";
import { type SourceOperation } from "@inogen/oms-client";
import { useWorkspace } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";

export function SourceActionFeedback({ action }: { action: ReturnType<typeof useSourceAction> }) {
  return <>{action.error && <Notice kind="error">{action.error}</Notice>}{action.busy && <p role="status">Processing source request…</p>}{action.operation && <SourceStatus status={action.operation.state} operation={action.operation} />}
    {action.recoveryKey && <div><Button variant="secondary" disabled={action.busy} onClick={() => void action.recover()}>Recover previous request</Button>
      {action.canRetryOriginalInput && <label className="oms-source-check"><input type="checkbox" disabled={action.busy} checked={action.retryOriginalInput} onChange={event => action.selectOriginalInputRetry(event.target.checked)} />Retry with the exact original input on the next submission</label>}
    </div>}</>;
}
export function SourceRecovery() {
  const { sources } = useWorkspace();
  const pending = useSourceResource(() => sources!.requests.pending(), [sources]);
  const action = useSourceAction();
  if (!sources) return null;
  return <>{!sources.requests.persistent && <Notice>Session recovery storage is unavailable. Keep this page open while a source request is pending.</Notice>}
    {pending.error && <Notice kind="error">{pending.error}</Notice>}
    {Boolean(pending.data?.length) && <Panel title="Pending source requests"><p>Recover a previous request with its original identity. Checking its status does not start a new source check.</p>
      {pending.data?.map((row, index) => <article className="record" key={row.key}><p>Request {index + 1}{row.operation_id ? ` · ${row.operation_id}` : " · response not received"}</p>
        {row.requires_body && !row.operation_id && <p>Private input was not stored. Recovery checks the server receipt using the original request identity.</p>}
          <Button variant="secondary" disabled={action.busy} onClick={() => void action.run(async () => {
            const result = await sources.requests.recover(row.key);
            if (!result || !("state" in result)) throw new Error("The recovered request is not an operation.");
            return result as SourceOperation;
          }, `recover:${row.key}`)}>Recover request {index + 1}</Button>
      </article>)}
    </Panel>}
    <SourceActionFeedback action={action} />
    {action.operation && <p className="muted">This is the recorded result of the original request. Current source state is refreshed separately.</p>}
      {action.operation && "discovery_id" in action.operation && typeof action.operation.discovery_id === "string" && <Link href={`/sources?discovery=${encodeURIComponent(action.operation.discovery_id)}`}>Open recovered discovery</Link>}
  </>;
}
