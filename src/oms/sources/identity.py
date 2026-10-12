"""Match parts across snapshots by their graph entities, and compare their order by rank among shared
siblings. Used by the plan and by the apply so both see the same correspondence."""
from oms.sources.models import PartKind


def align_projection(parts, mappings, reference_parts, reference_mappings):
    identities, placements_seen = {}, set()
    for key, mapping in reference_mappings.items():
        if mapping.entity_ids and key in reference_parts:
            kind = reference_parts[key].kind
            identity = (kind, tuple(sorted(mapping.entity_ids)))
            if kind not in (PartKind.FIELD, PartKind.FILE):
                placement = (*identity, mapping.section_id if kind == PartKind.RULE else None)
                if placement in placements_seen:
                    raise ValueError('colliding mapped projection identities')
                placements_seen.add(placement)
            identities.setdefault(identity, []).append(key)
    aligned, mapped = {}, {}
    for key, part in parts.items():
        target = key
        mapping = mappings.get(key)
        if (key not in reference_parts and mapping is not None and mapping.entity_ids
                and part.kind not in (PartKind.FIELD, PartKind.FILE)):
            candidates = identities.get((part.kind, tuple(sorted(mapping.entity_ids))), ())
            placements = [candidate for candidate in candidates
                          if reference_mappings[candidate].section_id == mapping.section_id]
            if len(placements) == 1:
                target = placements[0]
            elif len(candidates) == 1:
                target = candidates[0]
        if target in aligned:
            raise ValueError('colliding mapped projection identities')
        aligned[target] = part.model_copy(update={'part_id': target})
        if mapping is not None:
            mapped[target] = mapping.model_copy(update={'part_id': target})
    return aligned, mapped


def align_local(local, snapshot):
    parts, mappings = align_projection(
        {part.part_id: part for part in local.parts} if local else {},
        {row.part_id: row for row in local.graph_mappings} if local else {},
        {part.part_id: part for part in snapshot.effective_projection},
        {row.part_id: row for row in snapshot.graph_mappings})
    if local is None:
        return None
    return local.model_copy(update={'parts': tuple(parts.values()), 'graph_mappings': tuple(mappings.values())})


def same_rule_content(rule, value):
    return (rule.body == value['body'] and rule.polarity.value == value['polarity']
            and rule.reference_only == value['reference_only'])


def placement_container(part):
    """The sibling sequence a part's order indexes: a rule's section, an example's parent, or the skill."""
    value = part.evidence.value if part.evidence.kind == 'known' and isinstance(part.evidence.value, dict) else {}
    if part.kind == PartKind.RULE:
        return part.kind, value.get('section_id')
    if part.kind == PartKind.EXAMPLE:
        return part.kind, value.get('parent_rule_id'), value.get('parent_section_id'), value.get('parent_skill_id')
    return part.kind, None


def placement_order(part):
    """A part's sibling order, or None for a rule or example placed without one.

    Unordered rules and examples (learned ones) render after the ordered
    siblings, by corroboration, so they never take a place in the sequence.
    """
    value = part.evidence.value if part.evidence.kind == 'known' and isinstance(part.evidence.value, dict) else {}
    if part.kind in (PartKind.RULE, PartKind.EXAMPLE) and 'order' in value and value['order'] is None:
        return None
    return part.order


def sequence_key(part):
    """Sort key placing unordered parts after every ordered sibling."""
    order = placement_order(part)
    return order is None, order or 0


def moved_parts(first, second):
    """Parts whose rank among the siblings both sides hold differs.

    An index shift caused only by insertions or removals is not a move. A part
    whose container or certainty differs falls back to its absolute order. A
    part unordered on either side has no rank to compare and is not a move.
    """
    shared = {key for key in first.keys() & second.keys() if first[key].kind not in (PartKind.FIELD, PartKind.FILE)
              and placement_order(first[key]) is not None and placement_order(second[key]) is not None}
    common = {key for key in shared if first[key].evidence.kind == second[key].evidence.kind == 'known'
              and placement_container(first[key]) == placement_container(second[key])}

    def ranks(parts):
        groups = {}
        for key in sorted(common, key=lambda key: (parts[key].order, key)):
            groups.setdefault(placement_container(parts[key]), []).append(key)
        return {key: rank for keys in groups.values() for rank, key in enumerate(keys)}
    before, after = ranks(first), ranks(second)
    return {key for key in shared
            if (before[key] != after[key] if key in common else first[key].order != second[key].order)}
