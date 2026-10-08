"use client";
import { EmptyState, Panel } from "@inogen/oms-ui-core";
import { ResourceStatus, useResource, useWorkspace } from "@/lib/workspace";
import type { ImportReviewItem } from "@/lib/local-review";
import ImportReview from "./ImportReview";
import { PageHeading } from "./Shell";
import SkillImport from "./SkillImport";
import SourceUpdates from "./SourceUpdates";

export default function Import() {
  const { api, capabilities } = useWorkspace();
  const review = useResource(() => api.request<ImportReviewItem[]>("/api/import-review"), [api]);
  return <><PageHeading title="Bring your existing skills" description="Import a folder or ZIP containing SKILL.md files and their supporting references and scripts." /><div className="two-column"><Panel title="Import a skill package"><SkillImport onImported={review.refresh} /></Panel><Panel title="Keep the original structure"><pre className="tree">{"your-skills/\n└── expense-review/\n    ├── SKILL.md\n    ├── references/\n    │   └── policy.md\n    └── scripts/\n        └── check.sh"}</pre><p className="muted">OMS retains supporting files as part of the skill. Importing never runs an uploaded script.</p><p className="muted">Check the import result for safety warnings before publishing.</p></Panel></div>
    <section className="compact"><h2>Review imported changes</h2><p className="muted">Choose what to keep when an import changes existing guidance or raises a safety concern.</p><ResourceStatus resource={review} />{!review.loading && !review.error && review.data?.length === 0 && <EmptyState title="No import decisions waiting">Imported changes that need your attention appear here.</EmptyState>}<div className="stack">{review.data?.map((item) => <ImportReview key={item.id} item={item} onDecided={review.refresh} />)}</div></section>
    {capabilities.github_skill_sources && <SourceUpdates />}
  </>;
}
