"use client";
import { useRef } from "react";
import type { Skill, SkillSourceSummary } from "@inogen/oms-client";
import SkillSourcePanel from "./SkillSourcePanel";
import SourceActions from "./SourceActions";
import SourceSkillHistory from "./SourceSkillHistory";
import { SourceRecovery } from "./SourceOperations";

export const skillGitHubPanelId = (skillId: string) => `github-${skillId}`;
const skillPath = (skill: string) => `/api/skills/${encodeURIComponent(skill)}/source-binding/`;

/** Everything GitHub about one skill: its binding, its repository and its source history. */
export default function SkillGitHubPanel({ skill, source, dirty }: { skill: Pick<Skill, "id" | "name">; source: SkillSourceSummary | null; dirty: boolean }) {
  // A removal whose response is lost leaves the skill unlinked; its notice must still show here.
  const seen = useRef<string | null>(null);
  if (source) seen.current = source.source_id;
  const own = (row: { path: string; method: string }) => {
    const path = row.path.split("?")[0];
    return path.startsWith(skillPath(skill.id)) || (row.method === "POST" && path === "/api/skill-sources")
      || (!!seen.current && path.startsWith(`/api/skill-sources/${encodeURIComponent(seen.current)}/`))
      || (row.method === "POST" && /^\/api\/skill-updates\/[^/]+\/undo$/.test(path));
  };
  return <section id={skillGitHubPanelId(skill.id)} aria-label="GitHub">
    <SourceRecovery match={own} />
    <SkillSourcePanel key={source ? "linked" : "unlinked"} skill={skill} startEditing={!source} />
    {source && <SourceActions sourceId={source.source_id} />}
    <SourceSkillHistory skillId={skill.id} dirty={dirty} />
  </section>;
}
