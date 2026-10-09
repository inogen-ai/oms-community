"use client";
import { useEffect, useState } from "react";
import type { SourceDiscovery as Discovery, InstallSourceBody } from "@inogen/oms-client";
import { Notice, SourceDiscovery, sourceSkillId, type SourceSelection } from "@inogen/oms-ui-core";
import { useWorkspace, message } from "@/lib/workspace";
import { updateTarget } from "@/lib/sources";
import { useSourceAction, useSourceResource } from "@/lib/source-state";
import SourceDiscoveryForm from "./SourceDiscoveryForm";
import { SourceActionFeedback, SourceRecovery } from "./SourceOperations";

// Pending discoveries are shown by the lookup form itself, where "Check result" loads the discovery it found.
const installations = (row: { path: string; method: string }) => row.method === "POST" && row.path.split("?")[0] === "/api/skill-source-installations";

const outcomeLink = (outcome: { update_id?: string | null }) => outcome.update_id ? { href: updateTarget("github", outcome.update_id), label: "Open in review" } : null;

/** Add a GitHub source: URL first, then pick packages. Recovery shows only the requests made here. */
export default function GitHubImport({ discoveryId, onInstalled }: { discoveryId: string | null; onInstalled: () => void }) {
  const { sources, api } = useWorkspace();
  const skills = useSourceResource(() => api.skills(), [api]);
  const [discovery, setDiscovery] = useState<Discovery | null>(null);
  const [selections, setSelections] = useState<SourceSelection[] | undefined>(undefined);
  const action = useSourceAction();
  const [error, setError] = useState("");
  const domains = [...new Set(["general", ...(skills.data ?? []).map(skill => skill.domain || "general")])];
  useEffect(() => {
    let active = true;
    if (discoveryId) void sources!.client.discovery(discoveryId).then(value => { if (active) { setDiscovery(value); setSelections(undefined); } }).catch(failure => { if (active) setError(message(failure)); });
    return () => { active = false; };
  }, [sources, discoveryId]);
  function discovered(value: Discovery | null) { setDiscovery(value); setSelections(undefined); }
  async function install(body: InstallSourceBody) {
    const result = await action.run(key => sources!.client.install(body, key), JSON.stringify(["install", body]));
    if (!result) return;
    // Keep the packages that were not installed or held (blocked or failed) so they can be retried.
    const done = new Set(result.outcomes.filter(row => ["applied", "awaiting_review", "unchanged"].includes(row.state)).map(row => row.skill_id));
    if (result.outcomes.length) setSelections(current => (current ?? body.selections).filter(row => !done.has(sourceSkillId(row.local_name))));
    else if (result.committed) setSelections([]);
    if (result.committed || done.size) onInstalled();
  }
  return <>
    <SourceRecovery match={row => installations(row) && row.key !== action.recoveryKey} /><SourceActionFeedback action={action} subject="import" outcomeLink={outcomeLink} />{error && <Notice kind="error">{error}</Notice>}
    <SourceDiscoveryForm onDiscovered={discovered} subject="import" />
    {discovery && <SourceDiscovery discovery={discovery} selections={selections} domains={domains} defaultDomain="general" existingNames={(skills.data ?? []).flatMap(skill => [skill.id, skill.name])}
      onSelectionsChange={setSelections} onInstall={body => void install(body)} busy={action.busy} onLoadMore={cursor => {
        void sources!.client.discovery(discovery.discovery_id, { cursor }).then(next => setDiscovery(current => current?.discovery_id === next.discovery_id ? { ...next, packages: { ...next.packages, items: [...current.packages.items, ...next.packages.items] } } : current)).catch(failure => setError(message(failure)));
      }} />}
  </>;
}
