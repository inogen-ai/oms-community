"use client";
import { useState } from "react";
import Link from "next/link";
import { Badge, Button, Notice, Panel } from "@inogen/oms-ui-core";
import { Feedback, useAction, useWorkspace } from "@/lib/workspace";
import { importDecisionLabels, type ImportReviewItem } from "@/lib/local-review";

export default function ImportReview({ item, onDecided }: { item: ImportReviewItem; onDecided: () => void }) {
  const { api } = useWorkspace();
  const action = useAction();
  const [body, setBody] = useState(item.proposed_body ?? "");
  const labels = importDecisionLabels[item.kind];
  if (!labels) return <Notice>A newer kind of import decision needs a matching application version.</Notice>;
  const editable = item.kind === "block_revision";
  function decide(decision: "accept" | "reject") {
    return action.run(() => api.request(`/api/import-review/${encodeURIComponent(item.id)}/decision`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: decision, ...(editable && decision === "accept" && body !== (item.proposed_body ?? "") ? { body } : {}) }),
    }), "Import decision recorded.", onDecided);
  }
  return <Panel className="import-review-card" aria-label={`Import decision ${item.id}`}>
    <div className="record-heading"><h3>{labels.title}</h3><Badge>Needs a decision</Badge></div>
    <p className="import-reason">{item.reason}</p>
    {item.skill_id && <Link href={`/skills?skill=${encodeURIComponent(item.skill_id)}`}>Open the affected skill →</Link>}
    {editable && <label className="compact">Source wording to apply<textarea rows={5} value={body} disabled={action.busy} onChange={(event) => setBody(event.target.value)} /></label>}
    {!editable && item.proposed_body && <blockquote className="correction-source">{item.proposed_body}</blockquote>}
    <Feedback action={action} />
    <div className="actions"><Button variant="secondary" disabled={action.busy || (editable && !body.trim())} onClick={() => void decide("accept")}>{labels.accept}</Button><Button variant="secondary" disabled={action.busy} onClick={() => void decide("reject")}>{labels.reject}</Button></div>
  </Panel>;
}
