"use client";
import { useState } from "react";
import { Badge, Button } from "@inogen/oms-ui-core";
import type { LocalConstraint } from "@/lib/local-review";
import { Feedback, useAction, useWorkspace } from "@/lib/workspace";

export default function ConstraintRecord({ constraint, onChanged }: { constraint: LocalConstraint; onChanged: () => void }) {
  const { api } = useWorkspace();
  const action = useAction();
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(constraint.body);
  const retired = constraint.status === "retired";
  function update(changes: { body?: string; status?: "active" | "retired" }, message: string) {
    return action.run(() => api.request(`/api/constraints/${encodeURIComponent(constraint.id)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(changes),
    }), message, () => { setEditing(false); onChanged(); });
  }
  return <article className="record" aria-label={`Constraint ${constraint.id}`}>
    {editing ? <form onSubmit={(event) => { event.preventDefault(); void update({ body: body.trim() }, "Constraint updated."); }}><label>Constraint wording<textarea value={body} rows={4} disabled={action.busy} onChange={(event) => setBody(event.target.value)} /></label><div className="actions"><Button type="submit" disabled={action.busy || !body.trim()}>Save constraint</Button><Button variant="secondary" disabled={action.busy} onClick={() => setEditing(false)}>Cancel</Button></div></form> : <p>{constraint.body}</p>}
    <div className="record-footer"><Badge>{retired ? "retired" : "active"}</Badge><span className="muted">{constraint.source === "console" ? "Added here" : "Imported"}</span></div>
    {constraint.source === "console" ? <div className="actions">{!editing && <Button variant="secondary" disabled={action.busy} onClick={() => { setBody(constraint.body); setEditing(true); }}>Edit constraint</Button>}<Button variant="secondary" disabled={action.busy} onClick={() => void update({ status: retired ? "active" : "retired" }, retired ? "Constraint restored." : "Constraint retired.")}>{retired ? "Restore constraint" : "Retire constraint"}</Button></div> : <p className="muted compact">Edit the source file and import it again to change this constraint.</p>}
    <Feedback action={action} /><code className="provenance">{constraint.id}</code>
  </article>;
}
