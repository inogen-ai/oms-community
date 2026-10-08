"""Apply a reviewed update to the graph and commit everything that goes with it.

The selected parts are written, the sibling sequences they land in are renumbered,
ownership is recorded, and then the baseline, the undo receipt, required history and
the durable event are committed together. Manual approval and automatic application
both end in `commit_apply`; only how they got there differs.
"""
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from pathlib import PurePosixPath

from oms.domain.models import Artefact, ContentBlock, Edge, Example, Rule, Section, Transaction
from oms.domain.types import (ArtefactKind, EdgeType, ExampleKind, Mutability, Polarity, SectionKind, SignalType,
    SkillVersionCause, SourceRuntime)
from oms.ports.mutation import MutationContext
from oms.ports.source_store import SourcePolicy
from oms.sources import operations, review
from oms.sources.errors import SourceConflict, SourceNotFound, StaleMutation
from oms.sources.forks import isolate_rules
from oms.sources.identity import align_local, moved_parts, placement_container, placement_order, same_rule_content
from oms.import_skills.vocabulary import load_default_vocabulary
from oms.sources.local_projection import project_local
from oms.sources.models import (
    ActionContext, Binding, DurableEvent, Evidence, Generations, LocalState, LocalStream, Operation, OperationResult,
    PartKind, PartOwnership, ProjectionPart, SkillOutcome, SourceSnapshot, UndoCustody, UndoRecord, UndoWrite, Update,
)
from oms.sources.mutation import watch_changes
from oms.sources.primitives import exact_digest
from oms.sources.undo import capture_units


def apply_parts(context, snapshot, selected, *, now, operation_id, local=None, manual_parts=frozenset(), edges=None):
    graph, sources = context.graph, context.sources
    try:
        local = align_local(local, snapshot)
    except ValueError as error:
        raise SourceConflict("source_unit_identity_unknown") from error
    ref = snapshot.ref.origin.skill
    tenant = ref.tenant_id
    mappings = {row.part_id: row for row in snapshot.graph_mappings}
    live = {row.part_id: row for row in local.graph_mappings} if local else {}
    # Every rule written here is derived from a transaction, as the importer's
    # are, so lineage and the compiler's duplicate checks see upstream content
    # the same way. Merged text a reviewer wrote gets a transaction of its own:
    # a human's wording must never be attributed to the upstream revision.
    marker = f"source:{snapshot.ref.origin.origin_id}:{snapshot.revision}"
    txn_id = "source-transaction-" + exact_digest([operation_id, ref.storage_key])
    graph.upsert_transaction(Transaction(id=txn_id, signal_type=SignalType.SKILL_IMPORT,
        source_runtime=SourceRuntime.MANUAL, sanitised_payload_ref="source-snapshot:" + snapshot.ref.snapshot_id,
        timestamp=now, tenant_id=tenant, source_ref=marker, workflow_state="applied"))
    manual_txn = txn_id + ":manual"
    if manual_parts:
        graph.upsert_transaction(Transaction(id=manual_txn, signal_type=SignalType.EDIT,
            source_runtime=SourceRuntime.MANUAL, sanitised_payload_ref="source-review:" + operation_id,
            timestamp=now, tenant_id=tenant, source_ref="skill-editor/source-review/" + operation_id,
            workflow_state="applied"))
    selected = list(selected)
    mappings, live, rewritten, example_forks, created_edges = isolate_rules(graph, ref, snapshot, selected,
        local, operation_id, now)
    if edges is not None:
        edges.extend(created_edges)
    # Writes go parent before child (sections before the rules placed in them,
    # rules before their examples); removals run afterwards in the reverse
    # order, so nothing is attached to a container that no longer exists.
    rank = {PartKind.FIELD: 0, PartKind.SECTION: 1, PartKind.RULE: 2, PartKind.EXAMPLE: 3, PartKind.FILE: 4}
    ordered = sorted(selected, key=lambda part: (part.evidence.kind == "absent",
        -rank[part.kind] if part.evidence.kind == "absent" else rank[part.kind], part.order, part.part_id))
    vocabulary = kinds = None
    for part in ordered:
        if part.evidence.kind == "unknown":
            raise SourceConflict("source_unit_identity_unknown", "source unit unknown")
        mapping = mappings.get(part.part_id) or live.get(part.part_id)
        if mapping is None or len(mapping.entity_ids) != 1:
            raise SourceConflict("source_unit_identity_unknown")
        identifier = mapping.entity_ids[0]
        value = part.evidence.value
        part_marker = "skill-editor/source-review/" + operation_id if part.part_id in manual_parts else marker
        if part.evidence.kind == "absent":
            remove_part(graph, ref, part, live.get(part.part_id) or mapping)
            sources.clear_ownership(ref, part.part_id)
            continue
        if part.kind == PartKind.FIELD:
            name = part.part_id.removeprefix("field:")
            if name in {"name", "domain"}:
                continue
            skill = graph.get_skill(ref.skill_id, tenant_id=tenant)
            if name == "tags":
                for tag in graph.tag_records_for_skill(ref.skill_id, tenant_id=tenant):
                    graph.detach_edge(Edge(EdgeType.TAGGED_WITH, ref.skill_id, tag.id), tenant_id=tenant)
                for tag in value:
                    graph.upsert_tag(tag, tag)
                    graph.attach_edge(Edge(EdgeType.TAGGED_WITH, ref.skill_id, tag), tenant_id=tenant)
            else:
                attribute = "declared_license" if name == "license" else name
                if attribute not in {"description", "declared_license", "document_mode"}:
                    raise SourceConflict("invalid_request", "unsupported source field")
                graph.upsert_skill(replace(skill, **{attribute: value}))
        elif part.kind == PartKind.SECTION:
            section = Section(id=identifier, skill_id=ref.skill_id, tenant_id=tenant,
                heading=value["heading"], kind=SectionKind(value["kind"]), order=value["order"],
                mutability=Mutability(value["mutability"]))
            old = graph.get_section(identifier, tenant_id=tenant)
            if old is not None and old.mutability != section.mutability:
                raise SourceConflict("section_structure_changed")
            graph.upsert_section(section)
            if section.mutability is Mutability.AUTHORIAL_PASSTHROUGH:
                for block in graph.blocks_for_section(identifier, tenant_id=tenant):
                    graph.detach_block(block.id, identifier, tenant_id=tenant)
                digest = sha256(value["body"].encode()).hexdigest()
                block = ContentBlock(id="source-block-" + exact_digest([identifier, digest, operation_id]),
                    content_ref="sha256-" + digest, body=value["body"], kind=section.kind,
                    tenant_id=tenant, source_ref=part_marker)
                graph.upsert_content_block(block)
                graph.attach_block(block, identifier, tenant_id=tenant)
        elif part.kind == PartKind.RULE:
            # A rule whose content already matches is re-attached as it is, so
            # its status, corroboration and history survive; only a changed
            # body or polarity produces a fresh record.
            existing = graph.get_rule(identifier)
            rule = (existing if existing is not None and same_rule_content(existing, value) else
                    Rule(id=identifier, body=value["body"], tenant_id=tenant,
                         polarity=Polarity(value["polarity"]), reference_only=value["reference_only"]))
            graph.upsert_rule(rule)
            graph.attach_edge(Edge(EdgeType.BELONGS_TO, identifier, ref.skill_id), tenant_id=tenant)
            graph.attach_edge(Edge(EdgeType.DERIVED_FROM, identifier, manual_txn
                                   if part.part_id in manual_parts else txn_id), tenant_id=tenant)
            if value["section_id"]:
                graph.attach_rule(rule, value["section_id"], order=value["order"], group=value["group"],
                                  tenant_id=tenant)
        elif part.kind == PartKind.EXAMPLE:
            value = dict(value)
            if value.get("parent_rule_id") in rewritten:
                value["parent_rule_id"] = rewritten[value["parent_rule_id"]]
            graph.upsert_example(Example(id=identifier, tenant_id=tenant, source_ref=part_marker,
                                         **(dict(value) | {"kind": ExampleKind(value["kind"])})), tenant_id=tenant)
        elif part.kind == PartKind.FILE:
            path = part.part_id.removeprefix("file:")
            entry = next(entry for entry in snapshot.manifest if entry.path == path)
            if kinds is None:
                # No tenant vocabulary is bound here. An existing occurrence
                # keeps its kind; only a new path is classified by default.
                vocabulary = load_default_vocabulary()
                kinds = {existing: artefact.kind
                         for artefact, existing in graph.artefacts_for_skill(ref.skill_id, tenant_id=tenant)}
            kind = kinds.get(path)
            if kind is None:
                kind = ArtefactKind.DOCUMENTATION if path in vocabulary["documentation_files"] else ArtefactKind.OTHER
                if "/" in path:
                    kind = next((ArtefactKind(value) for value, directories in vocabulary["artefact_dirs"].items()
                                 if path.split("/", 1)[0] in directories), ArtefactKind.OTHER)
            graph.upsert_artefact(Artefact(id=identifier, content_ref=entry.blob_ref, kind=kind,
                name=PurePosixPath(path).name, size=entry.size, tenant_id=tenant, source_ref=marker,
                mode=entry.mode), ref.skill_id, path, tenant_id=tenant)
    selected_example_ids = {(mappings.get(part.part_id) or live[part.part_id]).entity_ids[0]
                            for part in selected if part.kind == PartKind.EXAMPLE}
    # An example forked along with its rule but not itself selected still has
    # to land somewhere. If its rule is no longer a member of this skill it is
    # reparented to the skill rather than left pointing at a rule the reader
    # cannot reach.
    for example in example_forks:
        if example.id not in selected_example_ids:
            members = {rule.id for rule in graph.rules_for_skill(ref.skill_id, tenant_id=tenant)}
            placed = {rule.id for section in graph.sections_for_skill(ref.skill_id, tenant_id=tenant)
                      for rule in graph.rules_for_section(section.id, tenant_id=tenant)}
            if example.parent_rule_id not in members | placed:
                example = replace(example, parent_rule_id=None, parent_skill_id=ref.skill_id)
            graph.upsert_example(example, tenant_id=tenant)
    repositioned = place_parts(graph, ref, snapshot, selected, local, mappings, live) if local is not None else set()
    effective = []
    for part in snapshot.effective_projection:
        if (part.kind == PartKind.EXAMPLE and part.evidence.kind == "known"
            and part.evidence.value.get("parent_rule_id") in rewritten):
            value = dict(part.evidence.value)
            value["parent_rule_id"] = rewritten[value["parent_rule_id"]]
            part = part.model_copy(update={"evidence": part.evidence.model_copy(update={"value": value})})
        effective.append(part)
    resolved = snapshot.model_copy(update={"graph_mappings": tuple(mappings.values()),
                                           "effective_projection": tuple(effective)})
    actual = project_local(graph, ref, baseline=resolved, ownership=sources.ownership_for_skill(ref))
    actual_maps = {row.part_id: row for row in actual.graph_mappings}
    # Ownership is what lets the next plan replace a part unattended. A part
    # the reviewer rewrote is theirs now, so its origin claim is cleared and a
    # later upstream edit to it will be a conflict, not a silent overwrite.
    for part in selected:
        if part.part_id in manual_parts:
            sources.clear_ownership(ref, part.part_id)
            continue
        if (part.evidence.kind == "known" and part.part_id not in {"field:name", "field:domain"}
            and part.part_id in actual_maps):
            row = actual_maps[part.part_id]
            sources.put_ownership(PartOwnership(skill=ref, part_id=part.part_id,
                origin_ids=(snapshot.ref.origin.origin_id,), entity_ids=row.entity_ids,
                section_id=row.section_id, proof_digest=row.custody_digest))
    # A sibling renumbered around the selected parts keeps its recorded custody claim.
    claims = {claim.part_id: claim for claim in sources.ownership_for_skill(ref)}
    previous = {row.part_id: row for row in local.graph_mappings} if local else {}
    for key in sorted(repositioned - {part.part_id for part in selected}):
        claim, row = claims.get(key), actual_maps.get(key)
        if (claim is not None and row is not None and key in previous
            and claim.proof_digest == previous[key].custody_digest):
            sources.put_ownership(claim.model_copy(update={"entity_ids": row.entity_ids,
                "section_id": row.section_id, "proof_digest": row.custody_digest}))
    return resolved


def place_parts(graph, ref, snapshot, selected, local, mappings, live):
    """Renumber every sibling sequence a selected part touches from the merged sequence.

    Kept and local-only parts hold their local relative order. A new part, or
    one whose upstream move was taken, goes after its nearest upstream
    predecessor (past local-only parts anchored there). An unordered local
    part (a learned rule or example) is never renumbered into the sequence:
    learned rules have no order and render after the ordered siblings by
    corroboration, so giving them one would turn an upstream apply into a
    local reordering of content the source never knew about.
    """
    tenant = ref.tenant_id
    upstream = {part.part_id: part for part in snapshot.effective_projection if part.evidence.kind == "known"}
    current = {part.part_id: part for part in local.parts if part.evidence.kind == "known"}
    removed = {part.part_id for part in selected if part.evidence.kind == "absent"}
    taken = {part.part_id for part in selected if part.evidence.kind == "known" and part.part_id in upstream}
    moved = moved_parts(current, upstream)
    containers = ({placement_container(upstream[key]) for key in taken}
                  | {placement_container(current[key]) for key in removed | taken if key in current})
    repositioned = set()
    for container in sorted(containers, key=repr):
        if (container[0] not in (PartKind.SECTION, PartKind.RULE, PartKind.EXAMPLE)
            or container[0] == PartKind.RULE and container[1] is None):
            continue
        theirs = [key for key in sorted(upstream, key=lambda key: (upstream[key].order, key))
                  if placement_container(upstream[key]) == container and placement_order(upstream[key]) is not None]
        movers = {key for key in taken & set(theirs) if key not in current or key in moved
                  or placement_container(current[key]) != container or placement_order(current[key]) is None}
        ours = [key for key in sorted(current, key=lambda key: (current[key].order, key))
                if placement_container(current[key]) == container and placement_order(current[key]) is not None
                and key not in removed | movers
                and not (key in taken and key not in theirs)]
        for position, key in enumerate(theirs):
            if key not in movers:
                continue
            anchor = next((ours.index(other) for other in reversed(theirs[:position]) if other in ours), None)
            index = 0 if anchor is None else anchor + 1
            while anchor is not None and index < len(ours) and ours[index] not in theirs:
                index += 1
            ours.insert(index, key)
        # When the merged sequence is exactly upstream's, upstream's own numbers
        # are used, so the next comparison sees no move at all; otherwise the
        # sequence is numbered densely from zero.
        orders = [upstream[key].order for key in ours] if ours == theirs else range(len(ours))
        placements = ({row.rule_id: row for row in graph.rule_placements_for_section(container[1], tenant_id=tenant)}
                      if container[0] == PartKind.RULE else {})
        for key, order in zip(ours, orders):
            mapping = (mappings.get(key) if key in taken else None) or live.get(key) or mappings.get(key)
            if mapping is None or len(mapping.entity_ids) != 1:
                raise SourceConflict("source_unit_identity_unknown")
            identifier = mapping.entity_ids[0]
            if container[0] == PartKind.RULE:
                row = placements.get(identifier)
                if row is not None and row.order != order:
                    graph.attach_rule(graph.get_rule(identifier), container[1], order=order, group=row.group,
                                      tenant_id=tenant)
                    repositioned.add(key)
            elif container[0] == PartKind.SECTION:
                section = graph.get_section(identifier, tenant_id=tenant)
                if section is not None and section.order != order:
                    graph.upsert_section(replace(section, order=order))
                    repositioned.add(key)
            else:
                example = graph.get_example(identifier, tenant_id=tenant)
                if example is not None and example.order != order:
                    graph.upsert_example(replace(example, order=order), tenant_id=tenant)
                    repositioned.add(key)
    return repositioned


def remove_part(graph, ref, part, mapping):
    identifier = mapping.entity_ids[0]
    tenant = ref.tenant_id
    if part.kind == PartKind.FIELD:
        name = part.part_id.removeprefix("field:")
        if name == "license":
            graph.upsert_skill(replace(graph.get_skill(ref.skill_id, tenant_id=tenant), declared_license=None))
        elif name == "tags":
            for tag in graph.tag_records_for_skill(ref.skill_id, tenant_id=tenant):
                graph.detach_edge(Edge(EdgeType.TAGGED_WITH, ref.skill_id, tag.id), tenant_id=tenant)
        elif name not in {"name", "domain"}:
            raise SourceConflict("invalid_request", "unsupported field removal")
    elif part.kind == PartKind.FILE:
        graph.remove_artefact(ref.skill_id, part.part_id.removeprefix("file:"), tenant_id=tenant)
    elif part.kind == PartKind.RULE:
        if mapping.section_id:
            graph.detach_rule(identifier, mapping.section_id, tenant_id=tenant)
        graph.detach_edge(Edge(EdgeType.BELONGS_TO, identifier, ref.skill_id), tenant_id=tenant)
    elif part.kind == PartKind.EXAMPLE:
        if any(owner != ref for owner in mapping.owner_skills):
            raise SourceConflict("review_choice_not_allowed", "shared example removal requires parent fork")
        graph.remove_example(identifier, tenant_id=tenant)
    elif part.kind == PartKind.SECTION:
        for block in graph.blocks_for_section(identifier, tenant_id=tenant, include_inactive=True):
            graph.detach_block(block.id, identifier, tenant_id=tenant)
        graph.remove_section(identifier, tenant_id=tenant)


def capture_apply(service, context, request, bound):
    skill = request.skill
    update = bound.sources.get_update(request.update_id, tenant_id=context.tenant_id)
    if update is None or update.plan.skill != skill:
        raise SourceNotFound("update_not_found")
    if update.status != "open" or update.plan.fingerprint != request.fingerprint:
        raise StaleMutation("update_changed")
    binding = review.origin_state(bound, update.plan.origin)
    if binding.baseline != update.plan.base:
        raise StaleMutation("update_changed", "baseline changed")
    incoming = bound.sources.get_snapshot(update.plan.incoming)
    base = bound.sources.get_snapshot(update.plan.base) if update.plan.base else None
    guard = bound.sources.get_generations(skill)
    local = project_local(bound.graph, skill, baseline=base, content_generation=guard.content,
        ownership=bound.sources.ownership_for_skill(skill), policy_version=service.builder.policy_version)
    fp = request.fingerprint
    if (guard.content != fp.content_generation or guard.binding != fp.binding_generation
            or local.digest != fp.local_digest or fp.policy_version != service.builder.policy_version
            or fp.policy_digest != service._policy_revision(context, bound, (skill,))):
        raise StaleMutation("content_changed", "local or policy changed")
    changed_parts = {change.part_id for change in update.plan.changes if change.action != "keep"}
    from oms.sources.forks import affected_owners
    owners = affected_owners(local, incoming, changed_parts)
    guards = tuple(bound.sources.get_generations(owner) for owner in sorted(owners))
    return update, binding, incoming, local, guards


@dataclass(frozen=True)
class PreparedApply:
    update: Update
    holder: Binding | LocalStream
    incoming: SourceSnapshot
    aligned: SourceSnapshot
    local: LocalState
    guards: tuple[Generations, ...]
    selected: tuple[ProjectionPart, ...]
    manual_parts: frozenset[str] = frozenset()


def commit_apply(bound: MutationContext, context: ActionContext, operation: Operation,
                 prepared: PreparedApply, *, now: datetime, policy_version: str,
                 policy: SourcePolicy) -> OperationResult:
    """Caller revalidates captured authority, policy and state before entering here."""
    update, binding = prepared.update, prepared.holder
    incoming, aligned, local = prepared.incoming, prepared.aligned, prepared.local
    guards, selected, manual_parts = prepared.guards, prepared.selected, prepared.manual_parts
    skill = update.plan.skill
    # The update and its holder were captured before the write lock was taken.
    # Compared whole here, inside it, so anything that moved in between (a
    # second reviewer, an undo, a relink) refuses the apply instead of letting
    # a plan about the old state be written over the new one (spec §8.3).
    if (bound.sources.get_update(update.update_id, tenant_id=skill.tenant_id) != update
            or review.origin_state(bound, binding.origin) != binding):
        raise StaleMutation("update_changed", "candidate changed")
    content_changes = watch_changes(bound.graph, context.tenant_id)
    previous_ownership = bound.sources.ownership_for_skill(skill)
    before_custody = capture_units(bound.graph, local, policy_version=policy_version)
    edges = []
    resolved = apply_parts(bound, aligned, selected, now=now, operation_id=operation.operation_id, local=local,
                           manual_parts=manual_parts, edges=edges)
    # Forking a shared rule gives it a new identity, so what was installed can
    # differ from what upstream sent. The installed form is retained under a
    # derived id and becomes the baseline: the next plan must compare against
    # the identities that are actually in the graph.
    if resolved != incoming:
        resolved = resolved.model_copy(update={
            "ref": resolved.ref.model_copy(update={"snapshot_id": resolved.ref.snapshot_id + ":resolved"})})
        bound.sources.put_snapshot(resolved)
    review.advance_baseline(bound, binding.origin, resolved.ref)
    bound.sources.put_update(update.model_copy(update={"status": "applied", "operation_id": operation.operation_id}))
    review.close_review(bound, update, "approved", context.actor_id)
    changed = content_changes()
    actual = project_local(bound.graph, skill, baseline=resolved,
        ownership=bound.sources.ownership_for_skill(skill), policy_version=policy_version)
    after_custody = capture_units(bound.graph, actual, policy_version=policy_version)
    before_parts = {part.part_id: part for part in local.parts}
    after_parts = {part.part_id: part for part in actual.parts}
    absent = Evidence(kind="absent", policy_version=policy_version)
    before_changed = {key for key in before_custody if before_custody[key] != after_custody.get(key, absent)}
    after_changed = {key for key in after_custody if after_custody[key] != before_custody.get(key, absent)}
    # A unit whose custody reappears under another key was renamed, not
    # removed and re-added; the receipt records it once or undo would try to
    # restore it twice.
    for key in sorted(tuple(before_changed)):
        alias = next((other for other in sorted(after_changed) if before_custody[key] == after_custody[other]), None)
        if alias is not None:
            before_changed.remove(key)
            after_changed.remove(alias)
    changed_units = sorted(before_changed | after_changed)
    receipt = UndoRecord(undo_id=operation.operation_id + ":undo", operation_id=operation.operation_id,
                         update_id=update.update_id,
        origin=binding.origin, previous_base=binding.baseline, resulting_base=resolved.ref,
        writes=tuple(UndoWrite(skill=skill, part_id=key, kind=(before_parts.get(key) or after_parts[key]).kind,
            before=before_parts[key].evidence if key in before_parts else absent,
            after=after_parts[key].evidence if key in after_parts else absent) for key in changed_units),
        edges=tuple(edges),
        custody=tuple(UndoCustody(skill=skill, part_id=key, before=before_custody.get(key, absent),
                                 after=after_custody.get(key, absent)) for key in changed_units),
        # The tracker advances each changed skill when this transaction
        # commits, so the receipt stores the generations as they will be after
        # it; undo compares against those, not against the ones read before.
        expected_generations=tuple(guard.model_copy(update={"content": guard.content + (guard.skill in changed)})
                                   for guard in guards),
        policy_version=policy_version, policy_digest=policy.revision(context, bound.graph, bound.reviews, (skill,)),
        previous_first_reconciliation=binding.first_reconciliation if isinstance(binding, Binding) else None,
        previous_ownership=previous_ownership,
        resulting_ownership=bound.sources.ownership_for_skill(skill),
        previous_mappings=local.graph_mappings, resulting_mappings=actual.graph_mappings)
    bound.sources.put_undo(receipt)
    # `capture_required` raises when history cannot be written, and that
    # aborts the whole transaction: a content change without its version is
    # not a state the repository may be left in (spec §8.3).
    histories = []
    for affected in changed:
        history = bound.history.capture_required(affected.skill_id, affected.tenant_id,
            cause=SkillVersionCause.SOURCE_UPDATE, actor=context.actor_id,
            source_operation_id=operation.operation_id, source_origin_id=binding.origin.origin_id,
            source_revision=incoming.revision)
        if history:
            histories.append(history.id)
    # Notifications leave with the commit as a durable event and are delivered
    # afterwards; a failed delivery can retry without touching content.
    event_id = operation.operation_id + ":applied"
    bound.events.append_event(DurableEvent(event_id=event_id, tenant_id=context.tenant_id,
        operation_id=operation.operation_id, action="source_apply", skills=changed or (skill,)))
    return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
        state="complete", committed=bool(changed),
        outcomes=(SkillOutcome(skill=skill, state="applied", update_id=update.update_id),)),
        history_ids=histories, event_ids=(event_id,))
