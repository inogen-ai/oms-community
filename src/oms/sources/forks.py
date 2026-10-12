"""Fork a shared rule before one skill changes it.

A rule that other skills also hold is given a new identity in this skill first, so
the change reaches only the skill the update is about and the other owners keep the
rule they had (spec §7.5).
"""
from dataclasses import replace

from oms.domain.models import Edge, Transaction
from oms.domain.types import EdgeType, SignalType, SourceRuntime
from oms.sources.errors import SourceConflict
from oms.sources.models import PartKind, UndoEdge
from oms.sources.primitives import exact_digest
from oms.sources.identity import same_rule_content


def isolate_rules(graph, ref, snapshot, selected, local, operation_id, now):
    tenant = ref.tenant_id
    mappings = {row.part_id: row for row in snapshot.graph_mappings}
    live = {row.part_id: row for row in local.graph_mappings} if local else {}
    parts = {part.part_id: part for part in local.parts} if local else {}
    explicit, rewritten, all_placements, same_content = {}, {}, set(), set()
    selected_content = {}
    for part in selected:
        if part.kind != PartKind.RULE or part.evidence.kind != "known" or part.part_id not in live:
            continue
        mapping = live[part.part_id]
        old_id = mapping.entity_ids[0]
        old = graph.get_rule(old_id)
        if old is None:
            raise SourceConflict("source_unit_identity_unknown", "local rule missing")
        value = part.evidence.value
        content = (value["body"], value["polarity"], value["reference_only"])
        if old_id in selected_content and selected_content[old_id] != content:
            raise SourceConflict("source_unit_identity_unknown", "source rule alias conflict")
        selected_content[old_id] = content
        explicit.setdefault(old_id, set()).add(part.part_id)
        if same_rule_content(old, part.evidence.value):
            same_content.add(old_id)
        if not same_rule_content(old, part.evidence.value) or any(owner != ref for owner in mapping.owner_skills):
            rewritten[old_id] = "source-rule-" + exact_digest([operation_id, old_id, ref.storage_key])
    removed_examples = set()
    for part in selected:
        if part.kind != PartKind.EXAMPLE:
            continue
        original = parts.get(part.part_id)
        value = (part.evidence.value if part.evidence.kind == "known" else original.evidence.value if original
                 and original.evidence.kind == "known" else None)
        if part.evidence.kind == "absent" and part.part_id in live:
            removed_examples.update(live[part.part_id].entity_ids)
        if not isinstance(value, dict) or value.get("parent_section_id") or not value.get("parent_rule_id"):
            continue
        old_id = value["parent_rule_id"]
        parents = [mapping for key, mapping in live.items() if key in parts
                   and parts[key].kind == PartKind.RULE and old_id in mapping.entity_ids]
        if any(owner != ref for mapping in parents for owner in mapping.owner_skills):
            rewritten[old_id] = "source-rule-" + exact_digest([operation_id, old_id, ref.storage_key])
            all_placements.add(old_id)
    members = {rule.id for rule in graph.rules_for_skill(ref.skill_id, tenant_id=tenant)}
    copy_marker = "skill-editor/source-fork/" + operation_id
    copy_txn = "source-custody-copy-" + exact_digest([operation_id, ref.storage_key])
    copies, edges = [], []
    for old_id, new_id in rewritten.items():
        old = graph.get_rule(old_id)
        if graph.get_rule(new_id) is not None:
            raise SourceConflict("source_already_exists", "fork identity already exists")
        clone = replace(old, id=new_id, embedding=None)
        existed = new_id in graph.superseders_of(old_id)
        graph.upsert_rule(clone)
        graph.attach_edge(Edge(EdgeType.SUPERSEDES, new_id, old_id), tenant_id=tenant)
        edges.append(UndoEdge(from_id=new_id, to_id=old_id, before=existed, after=True))
        if old_id not in explicit:
            graph.upsert_transaction(Transaction(id=copy_txn, signal_type=SignalType.EDIT,
                                                 source_runtime=SourceRuntime.MANUAL,
                sanitised_payload_ref="source-review:" + operation_id, timestamp=now, tenant_id=tenant,
                source_ref=copy_marker, workflow_state="applied"))
            graph.attach_edge(Edge(EdgeType.DERIVED_FROM, new_id, copy_txn), tenant_id=tenant)
        if old_id not in explicit or old_id in same_content:
            for txn in graph.lineage(old_id):
                graph.attach_edge(Edge(EdgeType.DERIVED_FROM, new_id, txn.id), tenant_id=tenant)
        if old_id in members:
            graph.detach_edge(Edge(EdgeType.BELONGS_TO, old_id, ref.skill_id), tenant_id=tenant)
            graph.attach_edge(Edge(EdgeType.BELONGS_TO, new_id, ref.skill_id), tenant_id=tenant)
        for key, mapping in list(live.items()):
            if key not in parts or parts[key].kind != PartKind.RULE or old_id not in mapping.entity_ids:
                continue
            if old_id not in all_placements and key not in explicit.get(old_id, ()):
                continue
            if mapping.section_id:
                placement = next((row for row in graph.rule_placements_for_section(mapping.section_id,
                    tenant_id=tenant) if row.rule_id == old_id), None)
                graph.detach_rule(old_id, mapping.section_id, tenant_id=tenant)
                graph.attach_rule(clone, mapping.section_id, order=placement.order if placement else None,
                    group=placement.group if placement else None, tenant_id=tenant)
            replacement = mapping.model_copy(update={"entity_ids": (new_id,), "owner_skills": (ref,)})
            live[key] = replacement
            if key in mappings:
                mappings[key] = mappings[key].model_copy(update={"entity_ids": (new_id,), "owner_skills": (ref,)})
        for example in graph.examples_for_rule(old_id, tenant_id=tenant):
            new_example_id = "source-example-" + exact_digest([operation_id, example.id, ref.storage_key])
            if example.id not in removed_examples:
                copies.append(replace(example, id=new_example_id, parent_rule_id=new_id, source_ref=copy_marker))
            for collection in (mappings, live):
                for key, mapping in list(collection.items()):
                    if mapping.entity_ids == (example.id,):
                        collection[key] = mapping.model_copy(update={"entity_ids": (new_example_id,),
                                                                     "owner_skills": (ref,)})
    return mappings, live, rewritten, copies, tuple(edges)


def affected_owners(local, incoming, changed_parts):
    owners = {local.skill}
    parts = {part.part_id: part for part in local.parts}
    upstream = {part.part_id: part for part in incoming.effective_projection}
    changed_entities = {(projection[row.part_id].kind, entity)
                        for projection, mappings in ((parts, local.graph_mappings), (upstream, incoming.graph_mappings))
                        for row in mappings if row.part_id in changed_parts and row.part_id in projection
                        for entity in row.entity_ids}
    parent_rules = set()
    for key in changed_parts:
        part = upstream.get(key) or parts.get(key)
        if part is not None and part.kind == PartKind.EXAMPLE and part.evidence.kind == "known":
            value = part.evidence.value
            if not value.get("parent_section_id") and value.get("parent_rule_id"):
                parent_rules.add(value["parent_rule_id"])
    for mapping in local.graph_mappings:
        part = parts.get(mapping.part_id)
        if (mapping.part_id in changed_parts or parent_rules.intersection(mapping.entity_ids)
                or part is not None and any((part.kind, entity) in changed_entities for entity in mapping.entity_ids)):
            owners.update(mapping.owner_skills)
    return tuple(sorted(owners))
