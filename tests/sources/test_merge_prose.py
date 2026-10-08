from oms.sources.models import PlanFlag
from tests.sources.merge_cases import merge_case, part


def test_unambiguous_section_edit_preserves_part_identity():
    case = merge_case(part('section:summary', {'heading': 'Summary', 'body': 'old', 'order': 0}, kind='section'))
    incoming = case.incoming.model_copy(update={'effective_projection': (part('section:summary', {'heading': 'Summary', 'body': 'new', 'order': 0}, kind='section'),)})
    plan = case.plan(incoming=incoming)
    assert [(c.part_id, c.action) for c in plan.changes] == [('section:summary', 'replace')]


def test_local_only_prose_survives_upstream_edit():
    case = merge_case(part('section:summary', 'old', kind='section'))
    local = case.local.model_copy(update={'parts': (*case.local.parts, part('section:local', 'addition', kind='section', owners=('local',), order=1))})
    incoming = case.incoming.model_copy(update={'effective_projection': (part('section:summary', 'new', kind='section'),)})
    plan = case.plan(local=local, incoming=incoming)
    assert next(c for c in plan.changes if c.part_id == 'section:local').action == 'keep'


def test_ambiguous_section_replacement_links_removal_and_requires_consent():
    case = merge_case(part('section:old', 'old guidance', kind='section'))
    incoming = case.incoming.model_copy(update={'effective_projection': (part('section:new', 'new guidance', kind='section'),)})
    plan = case.plan(incoming=incoming)
    assert PlanFlag.DELETION_CONSENT in plan.flags
    added = next(c for c in plan.changes if c.part_id == 'section:new')
    assert added.linked_removals == ('section:old',)
    assert added.action == 'conflict'


def test_examples_and_local_order_are_not_reparented_by_a_clean_body_edit():
    case = merge_case(part('example:e', {'body': 'example', 'parent': 'section:s', 'order': 2}, kind='example'))
    local = case.local.model_copy(update={'parts': (part('example:e', {'body': 'local', 'parent': 'section:s', 'order': 2}, kind='example'),)})
    incoming = case.incoming.model_copy(update={'effective_projection': (part('example:e', {'body': 'upstream', 'parent': 'section:other', 'order': 0}, kind='example'),)})
    plan = case.plan(local=local, incoming=incoming)
    assert plan.changes[0].action == 'conflict'


def test_recorded_exact_mapping_preserves_anchor_across_proven_rename():
    from oms.sources.models import GraphMapping
    case = merge_case(part('section:old', 'text', kind='section'))
    base = case.base.model_copy(update={'graph_mappings': (GraphMapping(part_id='section:old', entity_ids=('stable-section',)),)})
    incoming = case.incoming.model_copy(update={'effective_projection': (part('section:new', 'changed', kind='section'),),
        'graph_mappings': (GraphMapping(part_id='section:new', entity_ids=('stable-section',)),)})
    local = case.local.model_copy(update={'graph_mappings': (GraphMapping(part_id='section:old',
        entity_ids=('stable-section',), owner_skills=(case.local.skill,)),)})
    plan = case.plan(base=base, incoming=incoming, local=local)
    assert [(change.part_id, change.action) for change in plan.changes] == [('section:old', 'replace')]
    assert PlanFlag.DELETION_CONSENT not in plan.flags


def test_changed_source_and_local_placement_requires_explicit_review():
    case = merge_case(part('section:s', 'old', kind='section', order=0), part('section:t', 'other', kind='section', order=1))
    incoming = case.incoming.model_copy(update={'effective_projection': (part('section:s', 'new', kind='section', order=0), part('section:t', 'other', kind='section', order=1))})
    local = case.local.model_copy(update={'parts': (part('section:s', 'old', kind='section', order=3), part('section:t', 'other', kind='section', order=0))})
    plan = case.plan(local=local, incoming=incoming)
    assert next(change for change in plan.changes if change.part_id == 'section:s').action == 'conflict'
    assert [(conflict.part_id, conflict.reason) for conflict in plan.conflicts] == [('section:s', 'local_placement')]


def test_order_only_change_is_not_lost_when_text_is_identical():
    case = merge_case(part('section:s', 'text', kind='section', order=0), part('section:t', 'text', kind='section', order=1))
    incoming = case.incoming.model_copy(update={'effective_projection': (part('section:s', 'text', kind='section', order=1), part('section:t', 'text', kind='section', order=0))})
    plan = case.plan(incoming=incoming)
    assert [change.action for change in plan.changes] == ['conflict', 'conflict']
    assert all('changed_order' in conflict.reason for conflict in plan.conflicts)


def test_upstream_section_insertion_is_not_a_reorder():
    def section(name, order):
        return part('section:' + name, {'heading': name, 'body': name, 'order': order}, kind='section', order=order)
    case = merge_case(section('s', 0), section('t', 1))
    plan = case.plan(incoming=case.incoming.model_copy(update={'effective_projection': (section('s', 0), section('n', 1), section('t', 2))}))
    assert {change.part_id: change.action for change in plan.changes} == {'section:s': 'keep', 'section:n': 'add', 'section:t': 'keep'}
    assert plan.conflicts == () and plan.flags == ()


def test_same_projection_with_different_tuple_order_has_identical_plan():
    case = merge_case(part('section:a', 'a', kind='section', order=1), part('section:b', 'b', kind='section', order=0))
    reordered = case.incoming.model_copy(update={'effective_projection': tuple(reversed(case.incoming.effective_projection)),
        'parsed_projection': tuple(reversed(case.incoming.parsed_projection))})
    assert case.plan() == case.plan(incoming=reordered)


def test_duplicate_live_identity_is_a_structural_error():
    import pytest
    case = merge_case(part('section:s', 'text', kind='section'))
    local = case.local.model_copy(update={'parts': (*case.local.parts, *case.local.parts)})
    with pytest.raises(ValueError, match='duplicate'):
        case.plan(local=local)


def test_oversized_replacement_region_is_refused_instead_of_partially_linked():
    from dataclasses import replace
    import pytest
    case = merge_case(*(part(f'section:old-{i}', 'old', kind='section') for i in range(3)))
    incoming = case.incoming.model_copy(update={'effective_projection': tuple(part(f'section:new-{i}', 'new', kind='section') for i in range(3))})
    with pytest.raises(ValueError, match='replacement link budget'):
        case.plan(incoming=incoming, policy=replace(case.policy, max_replacement_links=4))


def test_comparison_part_budget_cannot_return_partial_inventory():
    from dataclasses import replace
    import pytest
    case = merge_case(part('section:a', 'a', kind='section'), part('section:b', 'b', kind='section'))
    with pytest.raises(ValueError, match='part budget'):
        case.plan(policy=replace(case.policy, max_parts=1))
