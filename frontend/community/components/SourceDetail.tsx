"use client";
import Link from "next/link";
import { EmptyState, Panel } from "@inogen/oms-ui-core";
import SkillSourcePanel from "./SkillSourcePanel";
import SourceActions from "./SourceActions";

export default function SourceDetail({ id }: { id: string }) {
  return <Panel title="Source details" className="oms-source-details"><Link href="/import/?github=sources">All sources</Link>
    <SourceActions sourceId={id}>{data => <>
      {!data.bindings.length && <EmptyState title="No linked skills" />}
      {data.bindings.map(binding => <SkillSourcePanel key={binding.origin.skill.skill_id} skill={data.skills.find(skill => skill.id === binding.origin.skill.skill_id) ?? { id: binding.origin.skill.skill_id, name: binding.origin.skill.skill_id }} />)}
    </>}</SourceActions>
  </Panel>;
}
