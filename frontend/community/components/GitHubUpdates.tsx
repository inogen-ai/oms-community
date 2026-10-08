"use client";
import { useRouter } from "next/navigation";
import { Button, GitHubMark } from "@inogen/oms-ui-core";
import GitHubSources from "./GitHubSources";
import SourceUpdates from "./SourceUpdates";
import { SourceRecovery } from "./SourceOperations";

// Update decisions and repository actions are made here; the add flow and local imports show their own notices.
const made = (row: { path: string }) => /^\/api\/skill-(updates|sources)\//.test(row.path.split("?")[0]);

/** "GitHub Source updates": the GitHub update list, with the tracked repositories card expanded above it on request. */
export default function GitHubUpdates({ expanded, sourceId, updateId }: { expanded: boolean; sourceId: string | null; updateId: string | null }) {
  const router = useRouter();
  return <section className="compact" aria-label="GitHub source updates">
    <div className="record-heading"><h2>GitHub Source updates</h2>
      <Button variant="secondary" aria-expanded={expanded} onClick={() => router.replace(expanded ? "/import/" : "/import/?github=sources", { scroll: false })}><GitHubMark size={15} />View GitHub sources</Button></div>
    <SourceRecovery match={made} />
    {expanded && <GitHubSources sourceId={sourceId} onClose={() => router.replace("/import/", { scroll: false })} />}
    <SourceUpdates origin="github" headingless selectedId={expanded ? updateId ?? undefined : undefined} />
  </section>;
}
