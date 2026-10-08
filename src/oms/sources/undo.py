"""Undo one applied update.

The receipt written at apply time records each changed unit as it was before and
after. Undo is offered only while the after-state still matches that record exactly;
it then puts back the before-state of those units, restores this origin's previous
baseline and suppresses the revision it reverted. Other origins, local additions and
the history already captured are left alone (spec §8.5).
"""
from dataclasses import dataclass, replace

from pydantic import TypeAdapter

from oms.domain.models import Artefact, ContentBlock, Edge, Example, Rule, Section
from oms.domain.types import EdgeType, SkillVersionCause
from oms.sources import operations, review
from oms.sources.errors import SourceConflict, SourceError, SourceNotFound, SourceForbidden
from oms.sources.local_projection import project_local
from oms.sources.models import (
    Binding, DurableEvent, Evidence, LocalState, LocalStream, ManifestEntry, OperationResult, PartKind, ProjectionPart,
    SkillOutcome, SourceSnapshot, UndoRecord, UndoRequest, UndoStatus,
)
from oms.sources.mutation import watch_changes


@dataclass(frozen=True)
class UndoState:
    record: UndoRecord
    holder: Binding | LocalStream
    snapshot: SourceSnapshot
    local: LocalState
    custody: dict[str, Evidence]


def _record(service, context, undo_id):
    def read(bound):
        record = bound.sources.get_undo(undo_id, tenant_id=context.tenant_id)
        if record is None:
            raise SourceNotFound("undo_not_found")
        # A receipt from before the skill was deleted names content that no
        # longer exists; it reads as not found rather than as an undo that
        # would recreate a deleted skill.
        fence = bound.sources.get_retirement(record.origin.skill)
        guard = next(row for row in record.expected_generations if row.skill == record.origin.skill)
        if fence is not None and guard.content <= fence.content:
            raise SourceNotFound("undo_not_found")
        service.policy.admit("read", context, (record.origin.skill,))
        return record
    return service._read(context, "read", (), read)


def _capture(service, context, record, bound):
    current = bound.sources.get_undo(record.undo_id, tenant_id=context.tenant_id)
    if current != record or record.undone_by_operation_id is not None:
        raise SourceConflict("undo_already_used")
    # A receipt that lacks custody, a policy digest or its update was written
    # by an earlier shape of this feature; without the exact after-state there
    # is nothing to compare against, so such an apply is not undoable.
    if record.update_id is None or record.policy_digest is None or record.writes and not record.custody:
        raise SourceConflict("undo_not_supported", "undo receipt incomplete")
    if {write.part_id for write in record.writes} != {item.part_id for item in record.custody}:
        raise SourceConflict("undo_not_supported", "undo custody incomplete")
    kinds = {write.part_id: write.kind.value for write in record.writes}
    for item in record.custody:
        if item.skill != record.origin.skill:
            raise SourceConflict("undo_not_supported", "undo custody scope unsupported")
        for evidence in (item.before, item.after):
            if evidence.kind == "unknown" or evidence.kind == "known" and (
                    not isinstance(evidence.value, dict) or evidence.value.get("kind") != kinds[item.part_id]):
                raise SourceConflict("undo_not_supported", "undo custody incomplete")
            if evidence.kind == "known" and evidence.value["kind"] == "rule" and "superseders" not in evidence.value:
                raise SourceConflict("undo_not_supported", "undo custody incomplete")
    bound.sources.compare_generations(record.expected_generations)
    holder = review.origin_state(bound, record.origin)
    if holder.baseline != record.resulting_base:
        raise SourceConflict("undo_state_changed", "undo baseline changed")
    if bound.sources.open_update(record.origin.skill) is not None:
        raise SourceConflict("undo_state_changed", "update already open")
    if (service.builder.policy_version != record.policy_version
        or service._policy_revision(context, bound, (record.origin.skill,)) != record.policy_digest):
        raise SourceConflict("undo_state_changed", "undo policy changed")
    if bound.sources.ownership_for_skill(record.origin.skill) != record.resulting_ownership:
        raise SourceConflict("undo_state_changed", "undo ownership changed")
    snapshot = bound.sources.get_snapshot(record.resulting_base)
    if (snapshot is None or record.previous_base is not None
        and bound.sources.get_snapshot(record.previous_base) is None):
        raise SourceConflict("record_unavailable", "retained snapshot missing")
    guard = bound.sources.get_generations(record.origin.skill)
    local = project_local(bound.graph, record.origin.skill, baseline=snapshot, content_generation=guard.content,
        ownership=record.resulting_ownership, policy_version=record.policy_version)
    if local.graph_mappings != record.resulting_mappings:
        raise SourceConflict("undo_state_changed", "undo graph identity changed")
    # The after-state guard. Every unit the apply changed must still be exactly
    # as the apply left it, down to placements, owners, lineage and supersession:
    # the rendered document reading the same is not enough, since a later edit,
    # a compile or another origin's apply can leave the text alone and change
    # what the units are. Any such change disables the shortcut; ordinary
    # history remains for restoring a document.
    custody = capture_units(bound.graph, local, policy_version=record.policy_version)
    absent = Evidence(kind="absent", policy_version=record.policy_version)
    for item in record.custody:
        if custody.get(item.part_id, absent) != item.after:
            raise SourceConflict("undo_state_changed", "undo after state changed")
    for edge in record.edges:
        endpoints = (bound.graph.get_rule(edge.from_id), bound.graph.get_rule(edge.to_id))
        if any(rule is None or rule.tenant_id != context.tenant_id for rule in endpoints):
            raise SourceConflict("undo_state_changed", "undo relationship changed")
        if (edge.from_id in bound.graph.superseders_of(edge.to_id)) != edge.after:
            raise SourceConflict("undo_state_changed", "undo relationship changed")
    validate_retained(bound.graph, record)
    return UndoState(record, holder, snapshot, local, custody)


def _screen(service, state):
    record = state.record
    writes = tuple(ProjectionPart(part_id=write.part_id, kind=write.kind, evidence=write.before)
                   for write in record.writes)
    manifest = []
    for item in record.custody:
        if item.before.kind != "known" or item.before.value["kind"] != "file":
            continue
        value = item.before.value
        file = value["record"]
        manifest.append(ManifestEntry(path=value["path"], blob_ref=file["content_ref"],
            digest=file["content_ref"].removeprefix("sha256-"), size=file["size"], mode=file.get("mode")))
    return service.policy.screen_resolved(state.snapshot, state.local, writes, manifest=tuple(manifest))


def undo_status(service, context, undo_id):
    record = _record(service, context, undo_id)
    if record.update_id is None:
        raise SourceConflict("undo_not_supported", "undo receipt incomplete")
    skills = tuple(guard.skill for guard in record.expected_generations)
    guards = record.expected_generations
    try:
        state = service._read(context, "undo", skills, lambda bound: _capture(service, context, record, bound))
        check = _screen(service, state)
        reasons = () if check.state == "passed" else (check.code,)
    except (PermissionError, SourceForbidden, SourceNotFound):
        reasons, guards = ("not_authorized",), ()
    except SourceError as error:
        reasons = (error.code,)
        if error.code in {"source_forbidden", "source_scope_denied"}:
            guards = ()
    return UndoStatus(undo_id=undo_id, update_id=record.update_id, skill=record.origin.skill,
        expected_generations=guards, policy_version=record.policy_version, available=not reasons, reasons=reasons)


def undo(service, context, request: UndoRequest, *, idempotency_key):
    operation, fresh = service._begin(context, "undo", request, idempotency_key, (request.skill,))
    if not fresh:
        return operation.result
    skills = (request.skill,)
    try:
        record = _record(service, context, request.undo_id)
        if record.origin.skill != request.skill:
            raise SourceNotFound("undo_not_found")
        if (tuple(sorted(request.expected_generations, key=lambda row: row.skill)) != record.expected_generations
                or request.policy_version != record.policy_version):
            raise SourceConflict("request_identity_changed", "undo request changed")
        skills = tuple(guard.skill for guard in record.expected_generations)
        state = service._read(context, "undo", skills, lambda bound: _capture(service, context, record, bound))
        if _screen(service, state).state != "passed":
            raise SourceConflict("required_check_held", "undo safety hold")

        def commit(bound):
            if _capture(service, context, record, bound) != state:
                raise SourceConflict("undo_state_changed")
            content_changes = watch_changes(bound.graph, context.tenant_id)
            restore(bound.graph, record)
            for claim in bound.sources.ownership_for_skill(record.origin.skill):
                bound.sources.clear_ownership(record.origin.skill, claim.part_id, entity_ids=claim.entity_ids,
                                              section_id=claim.section_id)
            for claim in record.previous_ownership:
                bound.sources.put_ownership(claim)
            # Only this origin's baseline goes back; another origin bound to the
            # same skill keeps its own. The reverted revision is then suppressed
            # for this binding, or the next check (automatic in the paid
            # edition) would offer or apply the very update just undone.
            review.advance_baseline(bound, record.origin, record.previous_base,
                reconciled=not record.previous_first_reconciliation if isinstance(state.holder, Binding) else True)
            bound.sources.suppress_candidate(record.origin, state.snapshot.revision)
            bound.sources.put_undo(record.model_copy(update={"undone_by_operation_id": operation.operation_id}))
            changed = content_changes()
            previous = bound.sources.get_snapshot(record.previous_base) if record.previous_base else None
            histories = []
            for ref in changed:
                version = bound.history.capture_required(ref.skill_id, ref.tenant_id,
                                                         cause=SkillVersionCause.SOURCE_UNDO,
                    actor=context.actor_id, source_operation_id=operation.operation_id,
                    source_origin_id=record.origin.origin_id, source_revision=previous.revision if previous else None)
                if version:
                    histories.append(version.id)
            event_id = operation.operation_id + ":undo"
            bound.events.append_event(DurableEvent(event_id=event_id, tenant_id=context.tenant_id,
                operation_id=operation.operation_id, action="source_undo", skills=changed or (request.skill,)))
            return operations.finish(bound.sources, operation, OperationResult(operation_id=operation.operation_id,
                state="complete", committed=bool(changed),
                outcomes=(SkillOutcome(skill=request.skill, state="applied"),)),
                history_ids=histories, event_ids=(event_id,))
        return service._commit(context, "undo", operation, record.expected_generations, commit)
    except Exception as error:
        service._failed(context, "undo", operation, skills, error=error)
        raise


def record(value):
    # Embeddings and creation timestamps are not content: a vector refresh or
    # a re-read must not make a unit look changed and block its undo.
    return TypeAdapter(type(value)).dump_python(value, mode="json", exclude={"embedding", "created_at"})


def capture_units(graph, local: LocalState, *, policy_version: str) -> dict[str, Evidence]:
    """Exact internal custody of every part of a skill, as the graph holds it.

    More than the projection shows: a rule's membership, placements, superseders
    and lineage, a section's blocks, a field's curation. This is what the undo
    receipt compares and restores, so a unit is only ever put back when its
    whole graph state is known and only the units that changed are touched.
    """
    ref = local.skill
    tenant = ref.tenant_id
    sections = graph.sections_for_skill(ref.skill_id, tenant_id=tenant)
    rules = {rule.id: rule for rule in graph.rules_for_skill(ref.skill_id, tenant_id=tenant)}
    members = set(rules)
    placements, examples = {}, {row.id: row for row in graph.examples_for_skill(ref.skill_id, tenant_id=tenant)}
    for section in sections:
        rules.update((row.id, row) for row in graph.rules_for_section(section.id, tenant_id=tenant))
        for placement in graph.rule_placements_for_section(section.id, tenant_id=tenant):
            placements.setdefault(placement.rule_id, []).append({"section_id": section.id, **record(placement)})
        examples.update((row.id, row) for row in graph.examples_for_section(section.id, tenant_id=tenant))
    for rule in rules.values():
        examples.update((row.id, row) for row in graph.examples_for_rule(rule.id, tenant_id=tenant))
    files = {path: artefact for artefact, path in graph.artefacts_for_skill(ref.skill_id, tenant_id=tenant)}
    section_rows = {section.id: section for section in sections}
    skill = graph.get_skill(ref.skill_id, tenant_id=tenant)
    mappings = {row.part_id: row for row in local.graph_mappings}
    result = {}
    for part in local.parts:
        mapping = mappings.get(part.part_id)
        if mapping is None or not mapping.entity_ids:
            continue
        identifier = mapping.entity_ids[0]
        data = {"kind": part.kind.value, "owners": [owner.storage_key for owner in mapping.owner_skills],
                "entity_id": identifier}
        if part.kind == PartKind.FIELD:
            name = part.part_id.removeprefix("field:")
            data.update(field=name, value=part.evidence.value, curated=name in skill.curated
                        or name == "license" and "declared_license" in skill.curated)
            if name == "tags":
                data["tags"] = [record(tag) for tag in graph.tag_records_for_skill(ref.skill_id, tenant_id=tenant)]
        elif part.kind == PartKind.SECTION and identifier in section_rows:
            data.update(record=record(section_rows[identifier]),
                blocks=[record(block) for block in graph.blocks_for_section(identifier, tenant_id=tenant,
                    include_inactive=True)],
                rules=[record(placement)
                       for placement in graph.rule_placements_for_section(identifier, tenant_id=tenant)],
                examples=sorted(row.id for row in graph.examples_for_section(identifier, tenant_id=tenant)))
        elif part.kind == PartKind.RULE and identifier in rules:
            data.update(record=record(rules[identifier]), member=identifier in members,
                superseders=sorted(graph.superseders_of(identifier)),
                placements=sorted(placements.get(identifier, []), key=lambda row: row["section_id"]),
                lineage=[record(transaction)
                         for transaction in sorted(graph.lineage(identifier), key=lambda row: row.id)],
                examples=sorted(row.id for row in graph.examples_for_rule(identifier, tenant_id=tenant)))
        elif part.kind == PartKind.EXAMPLE and identifier in examples:
            data["record"] = record(examples[identifier])
        elif part.kind == PartKind.FILE and part.part_id.removeprefix("file:") in files:
            path = part.part_id.removeprefix("file:")
            data.update(record=record(files[path]), path=path)
        else:
            result[part.part_id] = Evidence(kind="absent", policy_version=policy_version)
            continue
        result[part.part_id] = Evidence(kind="known", value=data, policy_version=policy_version)
    return result


def _validate_retained(graph, tenant, row, after, edges):
    # A unit the apply removed must not have been changed by anyone since, or
    # restoring the recorded form would destroy their work; such a change is a
    # history conflict, not a state the undo can reason about.
    kind = row["kind"]
    if kind == "field" and row.get("field") == "tags":
        for tag in row["tags"]:
            name = graph.tag_name(tag["id"])
            if name is not None and name != tag["name"]:
                raise SourceConflict("history_changed", "historical tag changed")
    data = row.get("record")
    if data is None:
        return
    identifier = data["id"]
    if after is not None and after.get("entity_id") == identifier:
        return
    getter = {"rule": lambda: graph.get_rule(identifier),
              "example": lambda: graph.get_example(identifier, tenant_id=tenant),
              "section": lambda: graph.get_section(identifier, tenant_id=tenant)}.get(kind)
    existing = getter() if getter else None
    if existing is not None and record(existing) != data:
        raise SourceConflict("history_changed", "historical custody changed")
    if kind == "rule" and existing is not None:
        expected = set(row["superseders"])
        for edge in edges:
            if edge.to_id == identifier:
                if edge.after:
                    expected.add(edge.from_id)
                else:
                    expected.discard(edge.from_id)
        if set(graph.superseders_of(identifier)) != expected:
            raise SourceConflict("history_changed", "historical supersession changed")
        if [record(txn) for txn in sorted(graph.lineage(identifier), key=lambda item: item.id)] != row["lineage"]:
            raise SourceConflict("history_changed", "historical provenance changed")
    for block in row.get("blocks", ()):
        current = graph.get_content_block(block["id"], tenant_id=tenant)
        if current is not None and record(current) != block:
            raise SourceConflict("history_changed", "historical custody changed")


def validate_retained(graph, undo):
    for item in undo.custody:
        if item.before.kind == "known":
            _validate_retained(graph, item.skill.tenant_id, item.before.value,
                               item.after.value if item.after.kind == "known" else None, undo.edges)


def restore(graph, undo):
    # Order matters: supersession edges first, then the after-state is detached
    # (rules from sections, blocks from sections) so nothing is attached to a
    # container that is about to change, sections are put back before what
    # they hold, examples after the rules they hang from, and sections that
    # exist only in the after-state go last, once they are empty.
    ref = undo.origin.skill
    tenant = ref.tenant_id
    for edge in undo.edges:
        relationship = Edge(EdgeType.SUPERSEDES, edge.from_id, edge.to_id)
        if edge.before:
            graph.attach_edge(relationship, tenant_id=tenant)
        else:
            graph.detach_edge(relationship, tenant_id=tenant)
        if (edge.from_id in graph.superseders_of(edge.to_id)) != edge.before:
            raise SourceConflict("undo_not_supported", "undo relationship restore failed")
    before = [item.before.value for item in undo.custody if item.before.kind == "known"]
    after = [item.after.value for item in undo.custody if item.after.kind == "known"]
    desired_examples = {row["entity_id"] for row in before if row["kind"] == "example"}
    desired_sections = {row["entity_id"] for row in before if row["kind"] == "section"}
    for row in after:
        if row["kind"] == "rule":
            for placement in row["placements"]:
                graph.detach_rule(row["entity_id"], placement["section_id"], tenant_id=tenant)
            if row["member"]:
                graph.detach_edge(Edge(EdgeType.BELONGS_TO, row["entity_id"], ref.skill_id), tenant_id=tenant)
        elif row["kind"] == "section":
            for block in row["blocks"]:
                graph.detach_block(block["id"], row["entity_id"], tenant_id=tenant)
        elif row["kind"] == "example" and row["entity_id"] not in desired_examples:
            graph.remove_example(row["entity_id"], tenant_id=tenant)
        elif row["kind"] == "file":
            graph.remove_artefact(ref.skill_id, row["path"], tenant_id=tenant)
    for row in before:
        if row["kind"] == "section":
            existing = graph.get_section(row["entity_id"], tenant_id=tenant)
            if existing is None or record(existing) != row["record"]:
                graph.upsert_section(TypeAdapter(Section).validate_python(row["record"]))
    for row in before:
        kind = row["kind"]
        if kind == "field":
            field = row["field"]
            if field == "tags":
                for tag in graph.tag_records_for_skill(ref.skill_id, tenant_id=tenant):
                    graph.detach_edge(Edge(EdgeType.TAGGED_WITH, ref.skill_id, tag.id), tenant_id=tenant)
                for tag in row["tags"]:
                    graph.upsert_tag(tag["id"], tag["name"])
                    graph.attach_edge(Edge(EdgeType.TAGGED_WITH, ref.skill_id, tag["id"]), tenant_id=tenant)
            else:
                attribute = "declared_license" if field == "license" else field
                if attribute not in {"name", "domain", "description", "declared_license", "document_mode"}:
                    raise SourceConflict("undo_not_supported", "undo field unknown")
                skill = graph.get_skill(ref.skill_id, tenant_id=tenant)
                graph.upsert_skill(replace(skill, **{attribute: row["value"]}))
        elif kind == "rule":
            data = row["record"]
            current = graph.get_rule(row["entity_id"])
            if current is None or record(current) != data:
                restored = TypeAdapter(Rule).validate_python(data)
                # The current embedding and timestamp stay; the record never
                # held them and a restored rule should not lose its vector.
                if current is not None:
                    restored = replace(restored, embedding=current.embedding, created_at=current.created_at)
                graph.upsert_rule(restored)
            expected = {txn["id"] for txn in row["lineage"]}
            for txn in graph.lineage(row["entity_id"]):
                if txn.id not in expected:
                    graph.detach_edge(Edge(EdgeType.DERIVED_FROM, row["entity_id"], txn.id), tenant_id=tenant)
            for txn in row["lineage"]:
                current_txn = graph.get_transaction(txn["id"])
                if current_txn is None or record(current_txn) != txn:
                    raise SourceConflict("history_changed", "historical provenance changed")
                graph.attach_edge(Edge(EdgeType.DERIVED_FROM, row["entity_id"], txn["id"]), tenant_id=tenant)
            restored = graph.get_rule(row["entity_id"])
            if row["member"]:
                graph.attach_edge(Edge(EdgeType.BELONGS_TO, row["entity_id"], ref.skill_id), tenant_id=tenant)
            for placement in row["placements"]:
                graph.attach_rule(restored, placement["section_id"], order=placement["order"],
                                  group=placement["group"], tenant_id=tenant)
        elif kind == "section":
            for data in row["blocks"]:
                current = graph.get_content_block(data["id"], tenant_id=tenant)
                if current is None:
                    graph.upsert_content_block(TypeAdapter(ContentBlock).validate_python(data))
                elif record(current) != data:
                    raise SourceConflict("history_changed", "historical custody changed")
                graph.attach_block(graph.get_content_block(data["id"], tenant_id=tenant), row["entity_id"],
                                   tenant_id=tenant)
        elif kind == "file":
            graph.upsert_artefact(TypeAdapter(Artefact).validate_python(row["record"]), ref.skill_id,
                                  row["path"], tenant_id=tenant)
    for row in before:
        if row["kind"] == "example":
            current = graph.get_example(row["entity_id"], tenant_id=tenant)
            if current is None or record(current) != row["record"]:
                graph.upsert_example(TypeAdapter(Example).validate_python(row["record"]), tenant_id=tenant)
    for row in after:
        if row["kind"] == "section" and row["entity_id"] not in desired_sections:
            graph.remove_section(row["entity_id"], tenant_id=tenant)
