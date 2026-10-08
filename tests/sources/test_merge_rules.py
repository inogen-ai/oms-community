from oms.sources.models import GraphMapping, PlanFlag
from oms.domain.identity import SkillRef
from tests.sources.merge_cases import merge_case, part
import pytest


def test_opposite_polarity_requires_review():
    case = merge_case(part('rule:r', {'body': 'Retain audit records', 'polarity': 'prescribe'}, kind='rule'))
    incoming = case.incoming.model_copy(update={'effective_projection': (part('rule:r', {'body': 'Retain audit records', 'polarity': 'proscribe'}, kind='rule'),)})
    plan = case.plan(incoming=incoming)
    assert plan.changes[0].action == 'conflict'
    assert 'opposite_polarity' in plan.conflicts[0].reason


def test_independent_owners_prevent_unattended_rewording():
    case = merge_case(part('rule:r', {'body': 'old', 'polarity': 'prescribe'}, kind='rule'))
    local = case.local.model_copy(update={'graph_mappings': (GraphMapping(part_id='rule:r', entity_ids=('r',), owner_skills=(case.local.skill, SkillRef('acme', 'other'))),)})
    incoming = case.incoming.model_copy(update={'effective_projection': (part('rule:r', {'body': 'new', 'polarity': 'prescribe'}, kind='rule'),)})
    plan = case.plan(local=local, incoming=incoming)
    assert 'independent_ownership' in plan.conflicts[0].reason


def test_reword_with_new_hash_cannot_hide_withdrawn_rule():
    case = merge_case(part('rule:old', {'body': 'old', 'section_id': 's', 'polarity': 'prescribe'}, kind='rule'))
    incoming = case.incoming.model_copy(update={'effective_projection': (part('rule:new', {'body': 'new', 'section_id': 's', 'polarity': 'prescribe'}, kind='rule'),)})
    plan = case.plan(incoming=incoming)
    assert next(c for c in plan.changes if c.part_id == 'rule:new').linked_removals == ('rule:old',)
    assert PlanFlag.DELETION_CONSENT in plan.flags


def test_already_present_rule_creates_no_duplicate_change():
    case = merge_case(part('rule:r', 'old', kind='rule'))
    updated = part('rule:r', 'new', kind='rule')
    plan = case.plan(local=case.local.model_copy(update={'parts': (updated,)}), incoming=case.incoming.model_copy(update={'effective_projection': (updated,)}))
    assert plan.changes[0].action == 'keep'


def test_independent_corroboration_cannot_transfer_to_new_wording():
    case = merge_case(part('rule:r', {'body': 'old', 'polarity': 'prescribe'}, kind='rule'))
    local = case.local.model_copy(update={'parts': (part('rule:r', {'body': 'old', 'polarity': 'prescribe', 'corroboration_count': 3}, kind='rule'),)})
    incoming = case.incoming.model_copy(update={'effective_projection': (part('rule:r', {'body': 'new', 'polarity': 'prescribe'}, kind='rule'),)})
    plan = case.plan(local=local, incoming=incoming)
    assert 'independent_corroboration' in plan.conflicts[0].reason
    assert 'corroboration_count' not in plan.changes[0].incoming.value


def test_changed_entity_mapping_requires_consent_even_with_stable_source_part_id():
    case = merge_case(part('rule:r', 'old', kind='rule'))
    base = case.base.model_copy(update={'graph_mappings': (GraphMapping(part_id='rule:r', entity_ids=('old-node',)),)})
    incoming = case.incoming.model_copy(update={'effective_projection': (part('rule:r', 'new', kind='rule'),),
        'graph_mappings': (GraphMapping(part_id='rule:r', entity_ids=('new-node',)),)})
    plan = case.plan(base=base, incoming=incoming)
    assert plan.changes[0].linked_removals == ('rule:r',)
    assert PlanFlag.DELETION_CONSENT in plan.flags
    assert PlanFlag.UNKNOWN_IDENTITY in plan.flags


@pytest.mark.parametrize("missing", ["origin", "graph"])
def test_both_origin_and_graph_ownership_must_be_known(missing):
    case = merge_case(part('rule:r', 'old', kind='rule'))
    mapping = GraphMapping(part_id='rule:r', entity_ids=('r',), owner_skills=(case.local.skill,))
    base = case.base.model_copy(update={'graph_mappings': (mapping,)})
    incoming = case.incoming.model_copy(update={'graph_mappings': (mapping,),
        'effective_projection': (part('rule:r', 'new', kind='rule'),)})
    local = case.local.model_copy(update={'graph_mappings': (mapping,) if missing == 'origin' else (),
        'parts': (part('rule:r', 'old', kind='rule', owners=() if missing == 'origin' else ('origin',)),)})
    plan = case.plan(base=base, incoming=incoming, local=local)
    assert plan.changes[0].action == 'conflict'
    assert 'unknown_ownership' in plan.conflicts[0].reason


def test_changed_local_entity_is_not_overwritten_even_when_text_matches_base():
    case = merge_case(part('rule:r', 'old', kind='rule'))
    original = GraphMapping(part_id='rule:r', entity_ids=('original',), owner_skills=(case.local.skill,))
    replacement = GraphMapping(part_id='rule:r', entity_ids=('local-replacement',), owner_skills=(case.local.skill,))
    base = case.base.model_copy(update={'graph_mappings': (original,)})
    incoming = case.incoming.model_copy(update={'graph_mappings': (original,),
        'effective_projection': (part('rule:r', 'new', kind='rule'),)})
    local = case.local.model_copy(update={'graph_mappings': (replacement,)})
    plan = case.plan(base=base, incoming=incoming, local=local)
    assert plan.changes[0].action == 'conflict'
    assert 'unknown_identity' in plan.conflicts[0].reason


def test_changed_local_mapping_placement_requires_review():
    case = merge_case(part('rule:r', 'old', kind='rule'))
    original = GraphMapping(part_id='rule:r', entity_ids=('r',), owner_skills=(case.local.skill,), section_id='original')
    moved = GraphMapping.model_validate(original.model_dump() | {'section_id': 'local-section'})
    base = case.base.model_copy(update={'graph_mappings': (original,)})
    incoming = case.incoming.model_copy(update={'graph_mappings': (original,),
        'effective_projection': (part('rule:r', 'new', kind='rule'),)})
    plan = case.plan(base=base, incoming=incoming, local=case.local.model_copy(update={'graph_mappings': (moved,)}))
    assert plan.changes[0].action == 'conflict'
    assert 'local_placement' in plan.conflicts[0].reason


def _placed(name, order, body=None):
    return part('rule:' + name, {'body': body or name, 'polarity': 'prescribe', 'reference_only': False,
        'section_id': 's', 'order': order, 'group': None}, kind='rule', order=order)


def test_upstream_insertion_mid_section_only_shifts_followers():
    case = merge_case(_placed('a', 0), _placed('b', 1), _placed('c', 2))
    incoming = case.incoming.model_copy(update={'effective_projection': (_placed('a', 0), _placed('x', 1), _placed('b', 2), _placed('c', 3))})
    plan = case.plan(incoming=incoming)
    changes = {c.part_id: c for c in plan.changes}
    assert {key: c.action for key, c in changes.items()} == {
        'rule:a': 'keep', 'rule:x': 'add', 'rule:b': 'keep', 'rule:c': 'keep'}
    assert [changes['rule:' + name].incoming.value['order'] for name in 'axbc'] == [0, 1, 2, 3]
    assert plan.conflicts == () and plan.flags == ()


def test_upstream_removal_mid_section_does_not_reorder_followers():
    case = merge_case(_placed('a', 0), _placed('b', 1), _placed('c', 2))
    plan = case.plan(incoming=case.incoming.model_copy(update={'effective_projection': (_placed('a', 0), _placed('c', 1))}))
    assert [(c.part_id, c.reason) for c in plan.conflicts] == [('rule:b', 'deletion_consent')]
    assert next(c for c in plan.changes if c.part_id == 'rule:c').action == 'keep'
    assert PlanFlag.DELETION_CONSENT in plan.flags


def test_genuine_upstream_swap_is_still_reviewed():
    case = merge_case(_placed('a', 0), _placed('b', 1), _placed('c', 2))
    incoming = case.incoming.model_copy(update={'effective_projection': (_placed('b', 0), _placed('a', 1), _placed('c', 2))})
    plan = case.plan(incoming=incoming)
    assert [(c.part_id, c.reason) for c in plan.conflicts] == [('rule:a', 'changed_order'), ('rule:b', 'changed_order')]
    assert next(c for c in plan.changes if c.part_id == 'rule:c').action == 'keep'


def test_local_reorder_keeps_its_placement_against_upstream_insertion():
    case = merge_case(_placed('a', 0), _placed('b', 1))
    local = case.local.model_copy(update={'parts': (_placed('b', 0), _placed('a', 1))})
    incoming = case.incoming.model_copy(update={'effective_projection': (_placed('x', 0), _placed('a', 1), _placed('b', 2))})
    plan = case.plan(local=local, incoming=incoming)
    assert {c.part_id: c.action for c in plan.changes} == {'rule:x': 'add', 'rule:a': 'keep', 'rule:b': 'keep'}
    assert plan.conflicts == () and plan.flags == ()


def test_local_correction_survives_an_upstream_index_shift():
    case = merge_case(_placed('a', 0), _placed('b', 1))
    local = case.local.model_copy(update={'parts': (_placed('a', 0), _placed('b', 1, 'b corrected'))})
    incoming = case.incoming.model_copy(update={'effective_projection': (_placed('x', 0), _placed('a', 1), _placed('b', 2))})
    plan = case.plan(local=local, incoming=incoming)
    assert {c.part_id: c.action for c in plan.changes} == {'rule:x': 'add', 'rule:a': 'keep', 'rule:b': 'keep'}
    assert plan.conflicts == () and plan.flags == ()


def test_local_insertion_does_not_turn_upstream_edits_into_placement_reviews():
    case = merge_case(_placed('a', 0), _placed('b', 1))
    local = case.local.model_copy(update={'parts': (_placed('a', 0), part('rule:local', {'body': 'local', 'polarity': 'prescribe',
        'reference_only': False, 'section_id': 's', 'order': 1, 'group': None}, kind='rule', owners=('local',), order=1), _placed('b', 2))})
    incoming = case.incoming.model_copy(update={'effective_projection': (_placed('a', 0), _placed('b', 1, 'b reworded'))})
    plan = case.plan(local=local, incoming=incoming)
    assert next(c for c in plan.changes if c.part_id == 'rule:b').action == 'replace'
    assert plan.conflicts == ()


def test_local_reorder_with_an_upstream_edit_is_still_a_placement_review():
    case = merge_case(_placed('a', 0), _placed('b', 1))
    local = case.local.model_copy(update={'parts': (_placed('b', 0), _placed('a', 1))})
    plan = case.plan(local=local, incoming=case.incoming.model_copy(update={'effective_projection': (_placed('a', 0, 'a reworded'), _placed('b', 1))}))
    assert [(c.part_id, c.reason) for c in plan.conflicts] == [('rule:a', 'local_placement')]
