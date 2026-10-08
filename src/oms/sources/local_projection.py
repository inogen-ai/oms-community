"""Project the live graph into the shape of a source snapshot, so a plan can compare the two.

Everything is read from the graph, never from rendered Markdown, and each part says
who owns it and whether its provenance proves that the source wrote it. Text that
merely matches upstream is not proof: a hand edit that restored the same wording is
still a hand edit.
"""
import re

from oms.domain.identity import SkillRef
from oms.domain.custody import effective_example_parent
from oms.domain.types import Mutability, SignalType
from oms.publish.render import REFERENCES_FILE
from oms.sources.files import file_evidence
from oms.sources.models import (Evidence, GraphMapping, LocalState, ManifestEntry, PartKind, PartOwnership,
                                ProjectionPart, SourceSnapshot)
from oms.sources.mutation import content_digest
from oms.sources.primitives import exact_digest, example_value, rule_value, section_value


_MAX_PARTS = 10000


def project_local(graph, skill: SkillRef, *, baseline: SourceSnapshot | None,
                  origins: tuple[SourceSnapshot, ...] = (), content_generation: int = 0,
                  policy_version: str = "1", ownership: tuple[PartOwnership, ...] = ()) -> LocalState:
    """Call within a coherent read boundary or validate captured generations later.

    `baseline` and `origins` are admitted retained source snapshots, not snapshots
    manufactured from today's graph. Unrecognized or manual provenance defeats an
    ownership claim even when the text happens to match. All tenant skill owners
    are inventoried, including memberships that exist only through sections.
    """
    if any(claim.skill != skill for claim in ownership):
        raise ValueError("Live ownership proof belongs to another skill")
    if baseline is not None and baseline.ref.origin.skill != skill:
        raise ValueError("Local baseline belongs to another skill")
    retained = {}
    for snapshot in (*((baseline,) if baseline is not None else ()), *origins):
        previous = retained.get(snapshot.ref)
        if previous is not None and previous != snapshot:
            raise ValueError("Conflicting retained snapshot identity")
        retained[snapshot.ref] = snapshot
    snapshots = tuple(retained.values())
    if any(snapshot.ref.origin.skill.tenant_id != skill.tenant_id for snapshot in snapshots):
        raise ValueError("Local ownership evidence crosses tenant boundary")
    # Every skill of the tenant is walked, not just this one: a rule, block or
    # example can belong to several skills, and the plan must know every owner
    # before it may replace or remove a part; a missed owner is a skill that
    # silently loses content.
    skills = graph.skills_for_tenant(skill.tenant_id)
    if len(skills) > _MAX_PARTS:
        raise ValueError("Local ownership inventory exceeds its budget")
    selected = next((row for row in skills if row.id == skill.skill_id), None)
    if selected is None:
        raise KeyError("Skill not found in the admitted tenant")
    owners: dict[tuple[str, str], set[SkillRef]] = {}
    inventory = {}
    rule_examples = {}
    units = 0
    for owner in sorted(skills, key=lambda row: row.id):
        ref = SkillRef(owner.tenant_id, owner.id)
        member_rules = {row.id: row for row in graph.rules_for_skill(owner.id, tenant_id=owner.tenant_id)}
        sections = []
        placements = []
        examples = {}
        placed = set()
        for section in graph.sections_for_skill(owner.id, tenant_id=owner.tenant_id):
            if section.skill_id != owner.id or section.tenant_id != owner.tenant_id:
                raise ValueError("Contradictory section ownership")
            rows = graph.rule_placements_for_section(section.id, tenant_id=owner.tenant_id)
            by_rule = {row.rule_id: row for row in rows}
            if len(by_rule) != len(rows):
                raise ValueError("Ambiguous rule placement")
            section_rules = graph.rules_for_section(section.id, tenant_id=owner.tenant_id)
            blocks = graph.blocks_for_section(section.id, tenant_id=owner.tenant_id)
            section_examples = graph.examples_for_section(section.id, tenant_id=owner.tenant_id)
            sections.append((section, blocks))
            examples.update((example.id, example) for example in section_examples)
            owners.setdefault(("section", section.id), set()).add(ref)
            for block in blocks:
                owners.setdefault(("block", block.id), set()).add(ref)
            for rule in section_rules:
                member_rules[rule.id] = rule
                placement = by_rule.get(rule.id)
                placements.append((rule, section.id, placement.order if placement else None,
                                   placement.group if placement else None))
                placed.add(rule.id)
        for rule in member_rules.values():
            owners.setdefault(("rule", rule.id), set()).add(ref)
            if rule.id not in placed:
                placements.append((rule, None, None, None))
            if rule.id not in rule_examples:
                rule_examples[rule.id] = graph.examples_for_rule(rule.id, tenant_id=owner.tenant_id)
            examples.update((example.id, example) for example in rule_examples[rule.id])
        examples.update((example.id, example)
                        for example in graph.examples_for_skill(owner.id, tenant_id=owner.tenant_id))
        for example in examples.values():
            owners.setdefault(("example", example.id), set()).add(ref)
        files = graph.artefacts_for_skill(owner.id, tenant_id=owner.tenant_id)
        units += len(sections) + len(placements) + len(examples) + len(files)
        if units > _MAX_PARTS:
            raise ValueError("Local ownership inventory exceeds its part budget")
        inventory[ref] = sections, placements, examples, files
    sections, placements, examples, files = inventory[skill]
    records = []
    for snapshot in snapshots:
        parts = {part.part_id: part for part in snapshot.effective_projection}
        for mapping in snapshot.graph_mappings:
            part = parts.get(mapping.part_id)
            if part is not None:
                records.append((snapshot, part, mapping))
    parts, mappings, used = [], [], set()
    lineage = {}
    reserved = {mapping.part_id: mapping for mapping in baseline.graph_mappings} if baseline else {}
    locations = {(rule.id, section_id) for rule, section_id, _, _ in placements}

    def candidates(kind, entity_id, *, path=None):
        return [(snapshot, part, mapping) for snapshot, part, mapping in records
                if part.kind == kind and entity_id in mapping.entity_ids
                and (path is None or part.part_id == "file:" + path)]

    # What counts as proof that a snapshot's origin wrote a unit: the marker an
    # apply stamps (`source:<origin>:<revision>`), or the import's original
    # source reference when exactly one origin is admitted for the skill, so an
    # import linked after the fact can be recognised. An editor marker never
    # proves anything, whatever the text says.
    def provenance(reference, snapshot):
        if not reference or reference.startswith("skill-editor/"):
            return False
        if reference.startswith("source:"):
            prefix, separator, revision = reference.rpartition(":")
            return bool(separator and revision and prefix == f"source:{snapshot.ref.origin.origin_id}")
        original = selected.import_source_ref
        admitted_origins = {row.ref.origin.origin_id for row in snapshots if row.ref.origin.skill == skill}
        return bool(len(admitted_origins) == 1 and snapshot.ref.origin.skill == skill
                    and original and reference.partition("#")[0] == original)

    def proven(rows, *, kind, record=None, block_rows=(), section_id=None, curated=False):
        if curated:
            return ()
        result = set()
        witnesses = [snapshot for snapshot, part, mapping in rows
                     if part.evidence.kind == "known"
                     and snapshot.ref.origin.origin_id in part.owner_origins
                     and snapshot.ref.origin.skill in mapping.owner_skills]
        for snapshot, part, mapping in rows:
            origin_id = snapshot.ref.origin.origin_id
            if (part.evidence.kind != "known" or origin_id not in part.owner_origins
                    or snapshot.ref.origin.skill not in mapping.owner_skills):
                continue
            if kind == PartKind.FIELD:
                continue
            elif kind == PartKind.SECTION:
                if (record.mutability is Mutability.AUTHORIAL_PASSTHROUGH and len(block_rows) == 1
                    and provenance(block_rows[0].source_ref, snapshot)):
                    result.add(origin_id)
            elif kind == PartKind.RULE:
                if record.id not in lineage:
                    lineage[record.id] = graph.lineage(record.id)
                observations = lineage[record.id]
                if (observations and record.status.value == "active"
                        and all(txn.tenant_id == skill.tenant_id and txn.signal_type is SignalType.SKILL_IMPORT
                                and any(provenance(txn.source_ref, witness) for witness in witnesses)
                                for txn in observations)
                        and any(provenance(txn.source_ref, snapshot) for txn in observations)):
                    result.add(origin_id)
            elif provenance(record.source_ref, snapshot):
                result.add(origin_id)
        return tuple(sorted(result))

    def emit(kind, entity_id, value, *, section_id=None, order=0, path=None, record=None,
             block_rows=(), curated=False, evidence=None, fixed_id=None):
        matches = candidates(kind, entity_id, path=path)
        if kind == PartKind.FIELD:
            matches = [row for row in matches if row[1].part_id == fixed_id]
        preferred = sorted(matches,
                           key=lambda row: (row[0] is not baseline, row[2].section_id != section_id, row[1].part_id))

        def available(part_id):
            if part_id in used:
                return False
            original = reserved.get(part_id)
            if original is None:
                return True
            if entity_id not in original.entity_ids:
                return False
            return not (kind == PartKind.RULE and original.section_id != section_id
                        and (entity_id, original.section_id) in locations)
        # A live unit takes the part identity the baseline gave it when the
        # same entity is still behind it; a rule moved to another section
        # while its old placement still exists is a different part. Reusing an
        # identity for a different entity would make the plan compare unrelated
        # units.
        part_id = fixed_id or next((part.part_id for _, part, _ in preferred if available(part.part_id)), None)
        if part_id is None:
            # Custody claims retain an anchor even after a source withdraws it.
            # Only the exact proof below can grant ownership of its current value.
            anchors = {claim.part_id for claim in ownership
                       if claim.entity_ids == (entity_id,) and claim.section_id == section_id
                       and claim.part_id.startswith((kind.value + ":", "local:" + kind.value + ":"))
                       and available(claim.part_id)}
            if len(anchors) == 1:
                part_id = anchors.pop()
        if part_id is None:
            part_id = f"local:{kind.value}:{exact_digest([entity_id, section_id, path])[:32]}"
        if part_id in used:
            raise ValueError("Ambiguous live projection identity")
        used.add(part_id)
        owned_by = ((skill,) if kind in (PartKind.FIELD, PartKind.FILE) else
                    tuple(sorted(owners.get((kind.value, entity_id), ()))))
        if kind == PartKind.SECTION:
            owned_by = tuple(sorted(set(owned_by) | {owner for block in block_rows
                for owner in owners.get(("block", block.id), ())}))
        actual = evidence or Evidence(kind="known", value=value, source_digest=exact_digest(value),
                                      policy_version=policy_version)
        # `independent` marks a unit the source may not replace unattended:
        # curated, hand-edited, inactive, or with a line of descent that is not
        # purely this tenant's imports. The custody digest binds the proof to
        # the exact value, so a claim recorded for one wording cannot vouch for
        # another.
        facts = None
        independent = curated
        if kind == PartKind.FIELD:
            facts = {"original_source": selected.import_source_ref}
            if fixed_id == "field:tags":
                facts["tags"] = [(tag.id, tag.name)
                                 for tag in graph.tag_records_for_skill(skill.skill_id, tenant_id=skill.tenant_id)]
        elif kind == PartKind.SECTION:
            facts = [{"id": block.id, "source": block.source_ref, "content": block.content_ref,
                      "status": getattr(block.status, "value", None)}
                      for block in sorted(block_rows, key=lambda item: item.id)]
            independent |= any((block.source_ref or "").startswith("skill-editor/") for block in block_rows)
        elif kind == PartKind.RULE:
            if record.id not in lineage:
                lineage[record.id] = graph.lineage(record.id)
            facts = sorted((txn.id, txn.tenant_id, txn.signal_type.value, txn.source_ref or "")
                           for txn in lineage[record.id])
            independent |= (record.status.value != "active" or any(
                txn.tenant_id != skill.tenant_id or txn.signal_type is not SignalType.SKILL_IMPORT
                for txn in lineage[record.id]))
        else:
            facts = {"source": record.source_ref}
            independent |= (record.source_ref or "").startswith("skill-editor/")
        custody = exact_digest({"kind": kind.value, "field": fixed_id if kind == PartKind.FIELD else None,
            "state": actual.kind, "value": actual.value, "entities": (entity_id,), "section": section_id,
            "owners": [owner.storage_key for owner in owned_by], "curated": curated, "provenance": facts})
        origins_owned = set()
        if actual.kind == "known" and not independent:
            origins_owned.update(proven(matches, kind=kind, record=record, block_rows=block_rows,
                                        section_id=section_id, curated=curated))
            for claim in ownership:
                if (claim.entity_ids == (entity_id,) and claim.section_id == section_id
                        and claim.proof_digest == custody and (kind != PartKind.FIELD or claim.part_id == fixed_id)):
                    origins_owned.update(claim.origin_ids)
        parts.append(ProjectionPart(part_id=part_id, kind=kind, evidence=actual,
            order=order if order is not None else 0, curated=curated, owner_origins=tuple(sorted(origins_owned))))
        mappings.append(GraphMapping(part_id=part_id, entity_ids=(entity_id,), owner_skills=owned_by,
                                     section_id=section_id, custody_digest=custody))

    for name, value in (("name", selected.name), ("domain", selected.domain),
                        ("description", selected.description),
                        ("tags", sorted(set(graph.tags_for_skill(skill.skill_id, tenant_id=skill.tenant_id))))):
        emit(PartKind.FIELD, selected.id, value, fixed_id="field:" + name, curated=name in selected.curated)
    licence = (Evidence(kind="absent", policy_version=policy_version) if selected.declared_license is None else
               Evidence(kind="known", value=selected.declared_license,
                        source_digest=exact_digest(selected.declared_license), policy_version=policy_version))
    emit(PartKind.FIELD, selected.id, None, fixed_id="field:license", evidence=licence,
         curated="license" in selected.curated or "declared_license" in selected.curated)
    mode = (Evidence(kind="unknown", policy_version=policy_version) if selected.document_mode is None else
            Evidence(kind="known", value=selected.document_mode, policy_version=policy_version))
    emit(PartKind.FIELD, selected.id, None, fixed_id="field:document_mode", evidence=mode)
    for section, blocks in sorted(sections, key=lambda row: (row[0].order, row[0].id)):
        unknown = section.mutability is Mutability.AUTHORIAL_PASSTHROUGH and len(blocks) != 1
        emit(PartKind.SECTION, section.id, section_value(section, blocks[0].body if blocks else ""),
             order=section.order, record=section, block_rows=blocks,
             evidence=Evidence(kind="unknown", policy_version=policy_version) if unknown else None)
    for rule, section_id, order, group in sorted(placements,
                                                 key=lambda row: (row[1] or "", row[2] is None, row[2] or 0,
                                                                  row[0].id)):
        emit(PartKind.RULE, rule.id, rule_value(rule, section_id, order, group),
             section_id=section_id, order=order, record=rule)
    rule_sections = {rule.id: section_id for rule, section_id, _, _ in placements}
    for example in sorted(examples.values(), key=lambda row: (row.order is None, row.order or 0, row.id)):
        parent_kind, parent_id = effective_example_parent(example)
        section_id = (parent_id if parent_kind == "Section" else rule_sections.get(parent_id)
                      if parent_kind == "Rule" else None)
        emit(PartKind.EXAMPLE, example.id, example_value(example, rule_id=example.parent_rule_id,
             section_id=example.parent_section_id, skill_id=example.parent_skill_id, order=example.order),
             section_id=section_id, order=example.order, record=example)
    manifest = []
    for artefact, path in sorted(files, key=lambda row: row[1]):
        if path == "SKILL.md" or path == REFERENCES_FILE:
            continue
        if not re.fullmatch(r"sha256-[0-9a-f]{64}", artefact.content_ref):
            raise ValueError("Current file bytes have no trustworthy digest")
        entry = ManifestEntry(path=path, blob_ref=artefact.content_ref, digest=artefact.content_ref[7:],
            size=artefact.size, mode=artefact.mode, published=path.casefold() != "claude.md",
            exclusion_reason="reserved_instruction_file" if path.casefold() == "claude.md" else None)
        manifest.append(entry)
        emit(PartKind.FILE, artefact.id, None, path=path, record=artefact,
             fixed_id="file:" + path, evidence=file_evidence(entry, policy_version))
    # Parts the baseline installed that no longer exist locally are reported as
    # known-absent, under their baseline identity, so the plan sees a local
    # removal rather than a part it has never heard of.
    if baseline is not None:
        baseline_parts = {part.part_id: part for part in baseline.effective_projection}
        for mapping in baseline.graph_mappings:
            previous = baseline_parts.get(mapping.part_id)
            if previous is None or mapping.part_id in used:
                continue
            current_owners = {owner for identifier in mapping.entity_ids
                              for owner in owners.get((previous.kind.value, identifier), ())}
            parts.append(ProjectionPart(part_id=mapping.part_id, kind=previous.kind,
                         evidence=Evidence(kind="absent", policy_version=policy_version), order=previous.order))
            mappings.append(mapping.model_copy(update={"owner_skills": tuple(sorted(current_owners))}))
    parts.sort(key=lambda part: part.part_id)
    mappings.sort(key=lambda mapping: mapping.part_id)
    digest = exact_digest({"graph": content_digest(graph, skill),
                          "parts": [part.model_dump(mode="json") for part in parts],
                          "mappings": [mapping.model_dump(mode="json") for mapping in mappings]})
    return LocalState(skill=skill, content_generation=content_generation, digest=digest,
                      parts=tuple(parts), manifest=tuple(manifest), graph_mappings=tuple(mappings),
                      document_mode=selected.document_mode)
