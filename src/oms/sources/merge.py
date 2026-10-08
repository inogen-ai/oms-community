"""Build the merge plan for one skill: base snapshot, incoming snapshot and live state, three ways.

`build_plan` is pure. It fetches nothing, writes nothing and reads no clock, so the same
three inputs always give the same plan and a plan can be rebuilt at commit time and compared
with the one the reviewer saw. Every decision here is a proposal; safety, authority and
the write itself are judged elsewhere (spec §7).
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Literal

from oms.sources.compare import UnitDecision, compare_field, compare_prose, compare_rule, compare_unit, same_value
from oms.sources.identity import align_projection as _align, moved_parts, sequence_key
from oms.sources.files import compare_file, file_evidence, script_change
from oms.sources.models import (
    Change, CheckResult, Conflict, Evidence, GraphMapping, LocalState, MergePlan,
    PartKind, PlanFingerprint, PlanFlag, ProjectionPart, SourceSnapshot,
)
from oms.sources.safety import FileCheck, SafetyRestriction, SourceSafety


@dataclass(frozen=True)
class MergePolicy:
    policy_version: str
    projection_version: str
    update_generation: int = 0
    policy_digest: str | None = None
    binding_generation: int | None = None
    first_reconciliation: bool = False
    history_evidence: Literal['proven_ancestor', 'rewritten', 'unproven', 'initial'] = 'proven_ancestor'
    safety: SourceSafety = field(default_factory=SourceSafety)
    required_checks: tuple[CheckResult, ...] = ()
    file_checks: tuple[FileCheck, ...] = ()
    restrictions: tuple[SafetyRestriction, ...] = ()
    max_parts: int = 10_000
    max_replacement_links: int = 100_000

    def __post_init__(self):
        # The budgets bound a comparison that is quadratic in the worst case
        # (every withdrawn part linked to every new one), so a hostile or
        # runaway projection fails fast instead of holding the transaction.
        if any(type(value) is not int or value <= 0 for value in (self.max_parts, self.max_replacement_links)):
            raise ValueError('comparison budgets must be positive integers')


def _index(parts: tuple[ProjectionPart, ...]) -> dict[str, ProjectionPart]:
    result = {part.part_id: part for part in parts}
    if len(result) != len(parts):
        raise ValueError('duplicate projection identity')
    return result


def _mappings(rows: tuple[GraphMapping, ...]) -> dict[str, GraphMapping]:
    result = {row.part_id: row for row in rows}
    if len(result) != len(rows):
        raise ValueError('duplicate graph mapping identity')
    return result


def _evidence(part, policy, *, uncertain=False):
    # A part missing from a snapshot is known to be absent, except in a base we
    # never saw whole: there, absence is not knowledge, and the comparison must
    # not read it as a removal or an addition.
    return (part.evidence if part is not None
            else Evidence(kind='unknown' if uncertain else 'absent', policy_version=policy.policy_version))


def _texts(value):
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(text for item in value for text in _texts(item))
    if isinstance(value, dict):
        return tuple(text for key, item in sorted(value.items())
                     if key in ('body', 'heading', 'name', 'description', 'text', 'group') for text in _texts(item))
    return ()


def _content(kind, evidence):
    # Position is judged by relative order in the plan, never as unit content.
    if (kind in (PartKind.SECTION, PartKind.RULE, PartKind.EXAMPLE) and evidence.kind == 'known'
            and isinstance(evidence.value, dict) and 'order' in evidence.value):
        return evidence.model_copy(update={'value': {key: item for key, item in evidence.value.items()
                                                     if key != 'order'}})
    return evidence


def _choices(kind):
    return ('keep_oms', 'use_upstream', 'merged_text') if kind in (PartKind.SECTION, PartKind.RULE,
                                                                   PartKind.EXAMPLE) else ('keep_oms', 'use_upstream')


def _digest(snapshot):
    # Sorted by identity before hashing so two snapshots of the same content
    # taken by adapters that return parts in different orders fingerprint alike.
    value = snapshot.model_dump(mode='json')
    for key, identity in (('effective_projection', 'part_id'), ('parsed_projection', 'part_id'),
                          ('graph_mappings', 'part_id'), ('manifest', 'path')):
        value[key] = sorted(value[key], key=lambda part: part[identity])
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def build_plan(base: SourceSnapshot | None, incoming: SourceSnapshot,
               local: LocalState, policy: MergePolicy) -> MergePlan:
    if incoming.ref.origin.skill != local.skill or base is not None and not base.ref.is_base_for(incoming.ref.origin):
        raise ValueError('source comparison crosses skill or origin')
    # All three sides must have been projected under the same policy. A policy
    # change is handled by reprojecting the retained evidence, never by treating
    # the difference as an upstream edit or by copying live state into the base.
    for snapshot in (base, incoming):
        if (snapshot is not None
            and (snapshot.policy_version != policy.policy_version
                 or snapshot.projection_version != policy.projection_version)):
            raise ValueError('compatible source reprojection is required')
    if policy.history_evidence not in ('initial', 'proven_ancestor', 'unproven', 'rewritten'):
        raise ValueError('invalid history evidence')
    for part in (*incoming.effective_projection, *(base.effective_projection if base else ()), *local.parts):
        if part.evidence.policy_version != policy.policy_version:
            raise ValueError('compatible evidence reprojection is required')
    if any(owner.tenant_id != local.skill.tenant_id for mapping in local.graph_mappings
           for owner in mapping.owner_skills):
        raise ValueError('local mappings cross tenant')
    part_keys = {part.part_id
                 for part in (*incoming.effective_projection, *(base.effective_projection if base else ()),
                              *local.parts)}
    part_keys.update('file:' + entry.path for snapshot in (base, incoming, local) if snapshot is not None
                     for entry in snapshot.manifest)
    if len(part_keys) > policy.max_parts:
        raise ValueError('comparison part budget exceeded')
    # Identity is exact: a part is the same part on two sides when its recorded
    # graph entities match, never because its text looks alike. Upstream is
    # aligned to the base first and the live state to both, so a part keyed
    # differently on each side still meets itself once.
    prior = _index(base.effective_projection) if base else {}
    prior_maps = _mappings(base.graph_mappings) if base else {}
    upstream, upstream_maps = _align(_index(incoming.effective_projection), _mappings(incoming.graph_mappings),
                                     prior, prior_maps)
    live, live_maps = _align(_index(local.parts), _mappings(local.graph_mappings),
                             prior | upstream, prior_maps | upstream_maps)
    for parts in (prior, upstream, live):
        # Manifest occurrences are authoritative for whole-file comparison.
        for key in [key for key, part in parts.items() if part.kind == PartKind.FILE]:
            if key == 'file:SKILL.md':
                del parts[key]
    for snapshot, parts in ((base, prior), (incoming, upstream), (local, live)):
        if snapshot is None:
            continue
        entries = {entry.path: entry for entry in snapshot.manifest if entry.path != 'SKILL.md'
                   and entry.exclusion_reason != 'generated_rule_overflow'}
        if len({entry.path.casefold() for entry in snapshot.manifest}) != len(snapshot.manifest):
            raise ValueError('colliding manifest paths')
        for path, entry in entries.items():
            key = 'file:' + path
            original = parts.get(key)
            parts[key] = ProjectionPart(part_id=key, kind=PartKind.FILE,
                                        evidence=file_evidence(entry, policy.policy_version),
                                        owner_origins=original.owner_origins if original else (),
                                        curated=original.curated if original else False)
        for key in [key for key, part in parts.items() if part.kind == PartKind.FILE
                    and key.removeprefix('file:') not in entries]:
            del parts[key]
    flags = set()
    # SKILL.md's authorial content is projected, but an explicit executable mode
    # still gives its changed occurrence the same script eligibility flag.
    skill_entries = [next((entry for entry in snapshot.manifest if entry.path == 'SKILL.md'), None)
                     if snapshot is not None else None for snapshot in (base, local, incoming)]
    skill_evidence = [file_evidence(entry, policy.policy_version) for entry in skill_entries]
    if skill_entries[1] is None and local.document_mode is not None:
        skill_evidence[1] = Evidence(kind='known', value={'mode': local.document_mode},
                                     policy_version=policy.policy_version)
    if script_change('SKILL.md', *skill_evidence):
        flags.add(PlanFlag.SCRIPT_CHANGES)
    raw_manifests = [{entry.path: entry for entry in snapshot.manifest} if snapshot else {}
                     for snapshot in (base, local, incoming)]
    for path in sorted(raw_manifests[0].keys() | raw_manifests[2].keys()):
        if script_change(path, *(file_evidence(entries.get(path), policy.policy_version) for entries in raw_manifests)):
            flags.add(PlanFlag.SCRIPT_CHANGES)
    # Without a trustworthy base there is no three-way comparison, only a
    # two-way one that cannot tell a local edit from an upstream change. The
    # flag keeps such a plan out of automatic application (spec §8.4).
    if base is None or policy.first_reconciliation or policy.history_evidence == 'initial':
        flags.add(PlanFlag.FIRST_RECONCILIATION)
    if policy.history_evidence in ('unproven', 'rewritten'):
        flags.add(PlanFlag.UNPROVEN_HISTORY if policy.history_evidence == 'unproven' else PlanFlag.REWRITTEN_HISTORY)
    changes, conflicts = {}, {}
    restrictions = {item.part_id: item for item in policy.restrictions}
    if len(restrictions) != len(policy.restrictions):
        raise ValueError('duplicate safety restrictions')
    if restrictions:
        flags.add(PlanFlag.SAFETY_HOLD)
    file_checks = {check.part_id: check for check in policy.file_checks}
    if len(file_checks) != len(policy.file_checks):
        raise ValueError('duplicate file checks')
    for check in policy.required_checks:
        if check.state != 'passed':
            flags.add(PlanFlag.REQUIRED_CHECK_UNAVAILABLE if check.state == 'unavailable' else PlanFlag.SAFETY_HOLD)
    keys = sorted(prior.keys() | upstream.keys() | live.keys())
    # Order is compared as rank among shared siblings, not as absolute index, so
    # an upstream insertion that shifts everything below it is not a hundred
    # moves. A genuine upstream reorder or a local move is reviewed, not
    # applied, because the plan cannot know which placement the reader wants.
    reordered, locally_moved, unmatched = (moved_parts(prior, upstream), moved_parts(prior, live),
                                           moved_parts(live, upstream))
    for key in keys:
        b, u, l = prior.get(key), upstream.get(key), live.get(key)
        kind = (u or b or l).kind
        if any(part is not None and part.kind != kind for part in (b, u, l)):
            raise ValueError('part kind changed under a stable identity')
        recorded = (_evidence(b, policy, uncertain=base is None or base.raw_frontmatter.kind == 'unknown'),
                    _evidence(u, policy), _evidence(l, policy))
        # The three-way table of spec §7.4 is in compare.py; what follows adds
        # the reasons the table cannot see from one unit alone: placement,
        # ownership and identity. Each pushes the decision to review, since
        # a guess about a reword, a move or an owner would silently overwrite
        # somebody's work.
        before, after, current = (_content(kind, evidence) for evidence in recorded)
        if (before.kind == 'unknown'
            and not (kind == PartKind.FIELD and key not in ('field:description', 'field:tags', 'field:license'))):
            flags.add(PlanFlag.FIRST_RECONCILIATION)
        owners = set(l.owner_origins) if l else set()
        mapped_owners = live_maps.get(key).owner_skills if key in live_maps else ()
        independent = (bool(owners - {incoming.ref.origin.origin_id})
                       or any(owner != local.skill for owner in mapped_owners))
        # A live part with no recorded owner was written before ownership was
        # tracked; it may be another origin's or a hand edit, so it is never
        # replaced unattended.
        unknown_owner = l is not None and current.kind != 'absent' and (not owners or not mapped_owners)
        old_mapping, new_mapping, local_mapping = prior_maps.get(key), upstream_maps.get(key), live_maps.get(key)
        identity_known = not (old_mapping and any(mapping is not None
            and set(old_mapping.entity_ids) != set(mapping.entity_ids) for mapping in (new_mapping, local_mapping)))
        if kind == PartKind.FIELD:
            decision = compare_field(key.removeprefix('field:'), before, current, after,
                                     curated=bool(l and l.curated), independently_owned=independent or unknown_owner)
            if (key == 'field:license' and before.kind != 'unknown' and after.kind != 'unknown'
                and not same_value(before, after)):
                flags.add(PlanFlag.DECLARED_LICENCE_CHANGED)
        elif kind == PartKind.RULE:
            decision = compare_rule(before, current, after, independently_owned=independent,
                                    identity_known=identity_known)
        elif kind == PartKind.FILE:
            decision = compare_file(before, current, after, independently_owned=independent)
            if script_change(key.removeprefix('file:'), before, current, after):
                flags.add(PlanFlag.SCRIPT_CHANGES)
        else:
            decision = compare_prose(before, current, after, independently_owned=independent,
                                     identity_known=identity_known)
        if kind not in (PartKind.FIELD, PartKind.FILE) and b is not None and u is not None:
            if key in reordered and (l is None or key in unmatched):
                decision = UnitDecision('review', tuple(dict.fromkeys((*decision.reasons, 'changed_order'))))
            elif l is not None and key in locally_moved and not same_value(before, after):
                decision = UnitDecision('review', tuple(dict.fromkeys((*decision.reasons, 'local_placement'))))
        if (old_mapping is not None and local_mapping is not None
            and local_mapping.section_id != old_mapping.section_id and not same_value(before, after)):
            decision = UnitDecision('review', tuple(dict.fromkeys((*decision.reasons, 'local_placement'))))
        if (old_mapping is not None and new_mapping is not None
            and new_mapping.section_id != old_mapping.section_id
            and (local_mapping is None or local_mapping.section_id != new_mapping.section_id)):
            decision = UnitDecision('review', tuple(dict.fromkeys((*decision.reasons, 'changed_placement'))))
        for mapping, evidence in ((old_mapping, before), (local_mapping, current), (new_mapping, after)):
            if (mapping is not None and evidence.kind == 'known' and isinstance(evidence.value, dict)
                    and 'section_id' in evidence.value and evidence.value['section_id'] != mapping.section_id):
                decision = UnitDecision('review', tuple(dict.fromkeys((*decision.reasons, 'unknown_placement'))))
        if unknown_owner and decision.action == 'take_incoming':
            decision = UnitDecision('review', ('unknown_ownership',))
        elif unknown_owner and decision.action == 'review' and not same_value(before, after):
            decision = UnitDecision('review', tuple(dict.fromkeys((*decision.reasons, 'unknown_ownership'))))
        reasons = list(decision.reasons) if decision.action == 'review' else []
        choices = _choices(kind)
        if (after.kind == 'absent' and current.kind != 'absent' and before.kind != 'absent'
                and not same_value(before, after)
                and not (kind == PartKind.FIELD and key not in ('field:description', 'field:tags', 'field:license'))):
            flags.add(PlanFlag.DELETION_CONSENT)
        checks = []
        if (after.kind == 'known' and not same_value(before, after)
            and not (kind == PartKind.FIELD and key not in ('field:description', 'field:tags', 'field:license'))):
            if kind == PartKind.FILE:
                checked = file_checks.get(key)
                checks.append(checked.result if checked is not None and checked.digest == after.value.get('digest')
                              else CheckResult(state='unavailable', code='file_check_unavailable'))
            else:
                if type(policy.safety) is not SourceSafety:
                    checks.append(CheckResult(state='unavailable', code='deterministic_checks_unavailable'))
                else:
                    checks.extend(policy.safety.check_text(text) for text in _texts(after.value))
        # A check that could not run is not a pass (spec §7.7); it holds the
        # part like a failure does, and taking the unscreened upstream text is
        # withdrawn from the reviewer's choices in both cases.
        for check in checks:
            if check.state != 'passed':
                flags.add(PlanFlag.REQUIRED_CHECK_UNAVAILABLE if check.state == 'unavailable' else PlanFlag.SAFETY_HOLD)
                reasons.append(check.code)
                choices = tuple(choice for choice in choices if choice != 'use_upstream')
        restriction = restrictions.get(key)
        if restriction is not None:
            flags.add(PlanFlag.SAFETY_HOLD)
            reasons.append(restriction.reason)
            choices = tuple(choice for choice in choices if choice in restriction.allowed_choices)
        if any(reason.startswith('unknown') for reason in reasons):
            flags.add(PlanFlag.UNKNOWN_IDENTITY)
        action = ('conflict' if reasons else 'keep' if decision.action in ('keep_local', 'already_matches')
                  else 'remove' if after.kind == 'absent' else 'add' if current.kind == 'absent' else 'replace')
        # A replacement whose identity is in doubt withdraws something, so it
        # carries the removal with it: splitting it into an add and a delete
        # would otherwise slip past the consent a deletion needs (spec §7.5).
        linked = (key,) if not identity_known and current.kind != 'absent' and after.kind != 'absent' else ()
        if linked:
            flags.add(PlanFlag.DELETION_CONSENT)
        changes[key] = Change(part_id=key, kind=kind, action=action, base=recorded[0], local=recorded[2],
                              incoming=recorded[1], linked_removals=linked)
        if reasons:
            conflicts[key] = Conflict(part_id=key, reason=','.join(dict.fromkeys(reasons)), allowed_choices=choices)
    # New source identities alongside withdrawals are a replacement region, never
    # an inferred rename or permission to delete a former source contribution.
    removed = [change for change in changes.values() if change.base.kind == 'known'
               and change.incoming.kind == 'absent' and change.local.kind != 'absent']

    def region_key(change, evidence):
        value = evidence.value
        region = value.get('section_id') if change.kind == PartKind.RULE and isinstance(value, dict) else None
        return change.kind, json.dumps(region, sort_keys=True)
    removed_regions = {}
    for old in removed:
        removed_regions.setdefault(region_key(old, old.base), []).append(old.part_id)
    link_count = 0
    for key, change in tuple(changes.items()):
        if change.base.kind != 'absent' or change.incoming.kind != 'known':
            continue
        linked = tuple(sorted(removed_regions.get(region_key(change, change.incoming), ())))
        if linked and change.kind != PartKind.FIELD:
            link_count += len(linked)
            if link_count > policy.max_replacement_links:
                raise ValueError('replacement link budget exceeded')
            flags.update((PlanFlag.DELETION_CONSENT, PlanFlag.UNKNOWN_IDENTITY))
            changes[key] = change.model_copy(update={'action': 'conflict', 'linked_removals': linked})
            old_conflict = conflicts.get(key)
            conflicts[key] = Conflict(part_id=key,
                                      reason='ambiguous_replacement' + (',' + old_conflict.reason
                                                                        if old_conflict else ''),
                                      allowed_choices=old_conflict.allowed_choices if old_conflict
                                      else _choices(change.kind))
    if conflicts:
        flags.add(PlanFlag.CONFLICT)
    ordered = sorted(changes, key=lambda key: (*sequence_key(live.get(key) or upstream.get(key) or prior[key]), key))
    # The fingerprint pins everything the plan was computed from. At commit the
    # live state is digested again and compared against it, so a reviewer's
    # decision is only ever applied to the state it was made about.
    return MergePlan(skill=local.skill, origin=incoming.ref.origin, base=base.ref if base else None,
                     incoming=incoming.ref,
                     fingerprint=PlanFingerprint(
                         content_generation=local.content_generation,
                         binding_generation=(incoming.ref.origin.generation
                                             if policy.binding_generation is None else policy.binding_generation),
                         update_generation=policy.update_generation, policy_version=policy.policy_version,
                         policy_digest=policy.policy_digest, local_digest=local.digest,
                         candidate_digest=_digest(incoming)),
                     changes=tuple(changes[key] for key in ordered),
                     conflicts=tuple(conflicts[key] for key in sorted(conflicts)),
                     flags=tuple(sorted(flags, key=lambda flag: flag.value)))
