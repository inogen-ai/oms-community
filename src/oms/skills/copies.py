"""Copy groups (one rule, one skill, spec 4.1).

A copy group is the set of rules joined by EQUIVALENT_TO edges whose kind is
"copy". Every member is joined to every other in both directions, so one read
from any member finds the whole group.
"""
from __future__ import annotations

from collections.abc import Sequence

from oms.domain.copies import COPY_CONFIDENCE, COPY_KIND, CopyCause
from oms.domain.types import EdgeType
from oms.ports.graph_store import GraphStore


def copy_group(store: GraphStore, rule_id: str, *, tenant_id: str) -> tuple[str, ...]:
    """The rule and every rule in its copy group, sorted."""
    others = store.copy_groups([rule_id], tenant_id=tenant_id).get(rule_id, ())
    return tuple(sorted({rule_id, *others}))


def link_copies(store: GraphStore, rule_ids: Sequence[str], *, cause: CopyCause,
                tenant_id: str) -> tuple[str, ...]:
    """Join these rules, and every rule already in a group with one of them,
    into one copy group. Returns the members, sorted. One rule alone writes
    nothing. An edge that already exists keeps the cause it was written with."""
    groups = store.copy_groups(list(rule_ids), tenant_id=tenant_id)
    members = sorted({*rule_ids, *(member for found in groups.values() for member in found)})
    if len(members) < 2:
        return tuple(members)
    for left in members:
        for right in members:
            # A pair the read found is joined already, and the store would
            # leave its edge as it is, so it costs no write.
            if left != right and right not in groups.get(left, ()):
                store.upsert_cross_skill_edge(EdgeType.EQUIVALENT_TO, left, right, COPY_CONFIDENCE,
                                              kind=COPY_KIND, cause=cause.value)
    return tuple(members)
