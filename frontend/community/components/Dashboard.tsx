"use client";
import Link from "next/link";
import { Badge, Button, EmptyState, Panel } from "@inogen/oms-ui-core";
import { HistorySummary } from "@inogen/oms-ui-core/history";
import { PageHeading } from "./Shell";
import type { ImportReviewItem } from "@/lib/local-review";
import { ResourceStatus, useResource, useWorkspace } from "@/lib/workspace";

const localPeople = new Map([["local-operator", "Local operator"]]);

export default function Dashboard() {
  const { api, capabilities } = useWorkspace();
  const resource = useResource(async () => {
    const [health, skills, inbox, imports] = await Promise.all([
      api.health(), api.skills(), capabilities.manual_learning ? api.review() : [],
      api.request<ImportReviewItem[]>("/api/import-review"),
    ]);
    return { health, skills, inbox, imports };
  }, [api, capabilities.manual_learning]);
  // A failed history read must not hide the work that still needs a decision.
  const changes = useResource(() => api.recentSkillChanges(), [api]);
  const data = resource.data;
  const established = !!data && Boolean(data.skills.length || data.inbox.length || data.imports.length);
  const held = data?.inbox.filter((item) => item.state === "held_safety").length ?? 0;
  return <>
    <PageHeading title="Workspace overview">
      {established && <Link className="oms-button oms-button--secondary" href="/publish">Preview & publish</Link>}
    </PageHeading>
    <ResourceStatus resource={resource} />
    {data && <>
      <div className="stats">
        <Panel><span className="stat-label">Skills in your library</span><strong>{data.skills.length}</strong><Link href="/skills">Open library →</Link></Panel>
        <Panel><span className="stat-label">Corrections to review</span><strong>{data.inbox.length}</strong>{capabilities.manual_learning && <Link href="/inbox">Review corrections →</Link>}</Panel>
        <Panel><span className="stat-label">Source changes to review</span><strong>{data.imports.length}</strong><Link href="/import">Review source changes →</Link></Panel>
      </div>
      {!established && <Panel title="Start with your skills" className="dashboard-onboarding">
        <ol className="journey">
          <li><span>01</span><div><h3>Bring your skills</h3><p>Upload a skill package with its references and scripts, or create a skill.</p><Link href="/skills">Open your skills library →</Link></div></li>
          <li><span>02</span><div><h3>Decide what belongs</h3><p>Turn a correction into a durable rule after reviewing its wording and evidence.</p>{capabilities.manual_learning && <Link href="/inbox">Open correction inbox →</Link>}</div></li>
          <li><span>03</span><div><h3>Publish when you’re ready</h3><p>Preview the guidance and write a portable skill tree.</p><Link href="/publish">Preview your guidance →</Link></div></li>
        </ol>
      </Panel>}
      <div className="two-column dashboard-work">
        <Panel title="Needs your attention">
          {data.inbox.length === 0 && data.imports.length === 0
            ? <EmptyState title="No pending decisions">Your correction inbox and source reviews are clear.</EmptyState>
            : <ul className="dashboard-actions">
              {capabilities.manual_learning && data.inbox.length > 0 && <li><Link href="/inbox"><div><strong>Review corrections</strong><p>{held ? `${held} ${held === 1 ? "correction needs" : "corrections need"} a safety decision. ` : ""}Choose which corrections become rules.</p></div><Badge>{data.inbox.length}</Badge></Link></li>}
              {data.imports.length > 0 && <li><Link href="/import"><div><strong>Review source changes</strong><p>Decide how imported source changes affect your skills.</p></div><Badge>{data.imports.length}</Badge></Link></li>}
            </ul>}
        </Panel>
        <Panel title="Recent skill changes">
          <p className="muted">The latest eight recorded changes across your skills.</p>
          <ResourceStatus resource={changes} />
          {changes.error && <Button variant="secondary" onClick={changes.refresh}>Try again</Button>}
          {changes.data && (changes.data.length ? <ol className="dashboard-changes">
            {changes.data.map((change) => <li key={change.id}><Link href={`/skills?skill=${encodeURIComponent(change.skill_id)}`}>
              <strong>{change.skill_name}</strong><HistorySummary entry={change} people={localPeople} />
            </Link></li>)}
          </ol> : <EmptyState title="No recorded changes yet">Saved skill changes will appear here.</EmptyState>)}
        </Panel>
      </div>
      <details className="dashboard-health"><summary>Workspace health <Badge>{String(data.health.status ?? data.health.state ?? "available")}</Badge></summary>
        <dl className="details">{Object.entries(data.health).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{typeof value === "object" ? JSON.stringify(value) : String(value)}</dd></div>)}</dl>
      </details>
    </>}
  </>;
}
