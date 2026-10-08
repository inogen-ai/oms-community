from dataclasses import replace

import pytest

from oms.sources.compare import compare_unit, same_value
from oms.sources.models import PlanFlag
from merge_cases import absent, known, unknown, merge_case, part


@pytest.mark.parametrize('base,local,incoming,owned,action,reason', [
    (unknown(), known('x'), known('x'), False, 'review', 'unknown_base'),
    (known(None), known(None), known(None), False, 'keep_local', None),
    (known('x'), known('local'), known('x'), False, 'keep_local', None),
    (known('x'), known('x'), absent(), False, 'review', 'deletion_consent'),
    (known('x'), absent(), absent(), False, 'already_matches', None),
    (known('x'), known('new'), known('new'), False, 'already_matches', None),
    (known('x'), known('x'), known('new'), False, 'take_incoming', None),
    (absent(), absent(), known(None), False, 'take_incoming', None),
    (known('x'), known('x'), known('new'), True, 'review', 'independent_ownership'),
    (known('x'), known('local'), known('new'), False, 'review', 'local_divergence'),
    (known('x'), unknown(), absent(), False, 'review', 'unknown_local'),
    (known('x'), known('x'), unknown(), False, 'review', 'unknown_incoming'),
])
def test_unit_comparison(base, local, incoming, owned, action, reason):
    decision = compare_unit(base, local, incoming, independently_owned=owned)
    assert decision.action == action
    if reason:
        assert reason in decision.reasons


def test_known_null_absence_and_unknown_are_distinct():
    assert not same_value(known(None), absent())
    assert not same_value(unknown(), unknown())
    assert same_value(absent(), absent())


@pytest.mark.parametrize('name', ['name', 'domain'])
def test_source_update_never_changes_local_identity(name):
    plan = merge_case(part('field:' + name, 'local')).change_field(name, 'upstream')
    assert plan.changes[0].action == 'keep'
    assert not plan.conflicts


@pytest.mark.parametrize('name', ['description', 'tags'])
def test_explicit_curation_requires_a_choice_even_when_value_matches_base(name):
    case = merge_case(part('field:' + name, 'original', curated=True))
    plan = case.change_field(name, 'changed')
    assert plan.changes[0].action == 'conflict'
    assert 'curated_field' in plan.conflicts[0].reason


def test_declared_license_add_change_remove_are_manual_holds():
    for case, incoming in [
        (merge_case(), (part('field:license', 'MIT'),)),
        (merge_case(part('field:license', 'MIT')), (part('field:license', 'Apache-2.0'),)),
        (merge_case(part('field:license', 'MIT')), ()),
    ]:
        plan = case.plan(incoming=case.incoming.model_copy(update={'effective_projection': incoming}))
        assert PlanFlag.DECLARED_LICENCE_CHANGED in plan.flags


def test_policy_mismatch_is_not_silently_rebased_from_local():
    case = merge_case(part('field:description', 'original'))
    with pytest.raises(ValueError, match='reprojection'):
        case.plan(policy=replace(case.policy, policy_version='2'))


def test_unsupported_fields_remain_evidence_only():
    plan = merge_case(part('field:x-extra', 'old')).change_field('x-extra', 'new')
    assert plan.changes[0].action == 'keep'


def test_equal_tag_sets_are_not_a_source_change():
    plan = merge_case(part('field:tags', ['one', 'two'])).change_field('tags', ['two', 'one'])
    assert plan.changes[0].action == 'keep'
    assert not plan.flags


def test_unknown_baseline_cannot_be_treated_as_absent_field():
    case = merge_case()
    base = case.base.model_copy(update={'raw_frontmatter': unknown()})
    incoming = case.incoming.model_copy(update={'effective_projection': (part('field:license', 'MIT'),)})
    plan = case.plan(base=base, incoming=incoming)
    assert PlanFlag.UNKNOWN_IDENTITY in plan.flags
    assert PlanFlag.DECLARED_LICENCE_CHANGED not in plan.flags


def test_prior_origin_generation_is_valid_but_foreign_origin_is_not():
    case = merge_case(part('field:description', 'original'))
    newer = case.incoming.ref.origin.model_copy(update={'generation': 2})
    incoming = case.incoming.model_copy(update={'ref': case.incoming.ref.model_copy(update={'origin': newer})})
    assert case.plan(incoming=incoming).fingerprint.binding_generation == 2
    foreign = newer.model_copy(update={'origin_id': 'other'})
    with pytest.raises(ValueError, match='origin'):
        case.plan(incoming=incoming.model_copy(update={'ref': incoming.ref.model_copy(update={'origin': foreign})}))


def test_partial_unknown_baseline_requires_first_manual_reconciliation():
    case = merge_case(part('field:description', 'old'))
    unknown_part = case.base.effective_projection[0].model_copy(update={'evidence': unknown()})
    plan = case.plan(base=case.base.model_copy(update={'effective_projection': (unknown_part,)}))
    assert PlanFlag.FIRST_RECONCILIATION in plan.flags
