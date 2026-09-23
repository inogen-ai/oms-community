"use client";
import { useState } from "react";
import Link from "next/link";
import { Button, EmptyState, Panel } from "@inogen/oms-ui-core";
import { Feedback, ResourceStatus, useAction, useResource, useWorkspace } from "@/lib/workspace";
import { PageHeading } from "./Shell";
import type { LocalConstraint } from "@/lib/local-review";
import ConstraintRecord from "./ConstraintRecord";

export default function Rules() {
  const { api } = useWorkspace();
  const resource = useResource(() => api.request<LocalConstraint[]>("/api/constraints"), [api]);
  const action = useAction();
  const [body, setBody] = useState("");
  const active = resource.data?.filter(constraint => (constraint.status ?? "active") === "active") ?? [];
  const inactive = resource.data?.filter(constraint => (constraint.status ?? "active") !== "active") ?? [];
  return <><PageHeading title="Constraints for every task" description="Requirements that apply across your skills, regardless of which skill an agent selects." /><Feedback action={action} /><ResourceStatus resource={resource} />
    <div className="two-column"><Panel title="Required constraints"><p className="muted">Active constraints appear in the published root instruction files (AGENTS.md and/or CLAUDE.md, as selected in Settings). After publishing and syncing, the installer connects these instructions to each installed agent. Changes take effect when that agent next loads its instructions.</p><p className="muted">Guidance for an individual skill belongs in its <Link href="/skills">skill document</Link>.</p>
      <div className="stack compact">{resource.data && active.length === 0 && <EmptyState title={inactive.length ? "No active constraints" : "No constraints yet"}>{inactive.length ? "Add a requirement or restore one from the inactive constraints below." : "Add a requirement that should apply to every task."}</EmptyState>}{active.map(constraint => <ConstraintRecord key={constraint.id} constraint={constraint} onChanged={resource.refresh} />)}</div>
      {inactive.length > 0 && <details className="constraint-archive"><summary>Inactive constraints ({inactive.length})</summary><div className="stack">{inactive.map(constraint => <ConstraintRecord key={constraint.id} constraint={constraint} onChanged={resource.refresh} />)}</div></details>}
    </Panel>
    <Panel title="Add a constraint"><form onSubmit={(event) => { event.preventDefault(); void action.run(() => api.createConstraint(body.trim()), "Constraint added. Publish to update your agents.", () => { setBody(""); resource.refresh(); }); }}><label>Required guidance<textarea rows={6} value={body} onChange={(event) => setBody(event.target.value)} required /></label><Button type="submit" disabled={!body.trim() || action.busy}>Add constraint</Button></form></Panel></div>
  </>;
}
