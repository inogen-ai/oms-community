"""Pure source/local projections used by comparison tests."""
from dataclasses import dataclass, replace
from hashlib import sha256

from oms.domain.identity import SkillRef
from oms.sources.models import Evidence, GraphMapping, LocalState, ManifestEntry, OriginRef, ProjectionPart, SnapshotRef, SourceSnapshot
from oms.sources.merge import MergePolicy, build_plan
from oms.sources.files import file_evidence
from oms.sources.safety import FileCheck


def known(value):
    return Evidence(kind='known', value=value, policy_version='1')


def absent():
    return Evidence(kind='absent', policy_version='1')


def unknown():
    return Evidence(kind='unknown', policy_version='1')


def part(identifier, value, *, kind='field', owners=('origin',), curated=False, order=0):
    return ProjectionPart(part_id=identifier, kind=kind, evidence=known(value), owner_origins=owners, curated=curated, order=order)


def file(path, body=b'old', mode=0o100644):
    digest = sha256(body).hexdigest()
    return ManifestEntry(path=path, blob_ref='sha256-' + digest, digest=digest, size=len(body), mode=mode)


@dataclass(frozen=True)
class MergeCase:
    base: SourceSnapshot
    incoming: SourceSnapshot
    local: LocalState
    policy: MergePolicy

    def plan(self, **changes):
        case = replace(self, **changes)
        return build_plan(case.base, case.incoming, case.local, case.policy)

    def change_field(self, name, value):
        incoming = self.incoming.model_copy(update={'effective_projection': (part('field:' + name, value),)})
        return self.plan(incoming=incoming)

    def change_file(self, path, body):
        checked = self.policy.safety.check_file(body)
        policy = replace(self.policy, file_checks=(FileCheck(f'file:{path}', sha256(body).hexdigest(), checked),))
        return self.plan(incoming=self.incoming.model_copy(update={'manifest': (file(path, body),)}), policy=policy)


def merge_case(*parts, manifest=()):
    parts = (*parts, *(ProjectionPart(part_id='file:' + entry.path, kind='file', evidence=file_evidence(entry, '1'), owner_origins=('origin',)) for entry in manifest if entry.path != 'SKILL.md'))
    skill = SkillRef('acme', 'tools')
    origin = OriginRef(skill=skill, origin_id='origin', kind='github', generation=1)
    mappings = tuple(GraphMapping(part_id=item.part_id, entity_ids=(item.part_id,), owner_skills=(skill,),
        section_id=item.evidence.value.get('section_id') if isinstance(item.evidence.value, dict) else None) for item in parts)
    base = SourceSnapshot(ref=SnapshotRef(snapshot_id='base', origin=origin), revision='a' * 40,
        raw_frontmatter=absent(), effective_projection=parts, parsed_projection=parts,
        manifest=manifest, graph_mappings=mappings, policy_version='1', projection_version='1')
    incoming = base.model_copy(update={'ref': SnapshotRef(snapshot_id='incoming', origin=origin), 'revision': 'b' * 40})
    local = LocalState(skill=skill, content_generation=3, digest='local-digest', parts=parts, manifest=manifest,
                       graph_mappings=mappings)
    return MergeCase(base, incoming, local, MergePolicy(policy_version='1', projection_version='1'))
