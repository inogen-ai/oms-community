import pytest

from oms.sources.models import PlanFlag
from merge_cases import merge_case, file, part


def test_declared_field_and_licence_file_have_different_gates():
    field_plan = merge_case(part('field:license', 'old')).change_field('license', 'new declaration')
    file_plan = merge_case(manifest=(file('LICENSE'),)).change_file('LICENSE', b'new contents')
    assert PlanFlag.DECLARED_LICENCE_CHANGED in field_plan.flags
    assert PlanFlag.DECLARED_LICENCE_CHANGED not in file_plan.flags


@pytest.mark.parametrize('path,old_mode,new_mode,body', [
    ('scripts/run.sh', 0o100644, 0o100644, b'new'),
    ('tool', 0o100755, 0o100755, b'new'),
    ('tool', 0o100644, 0o100755, b'old'),
    ('tool', 0o100755, 0o100644, b'old'),
])
def test_changed_script_occurrences_cover_bytes_and_both_modes(path, old_mode, new_mode, body):
    case = merge_case(manifest=(file(path, mode=old_mode),))
    plan = case.plan(incoming=case.incoming.model_copy(update={'manifest': (file(path, body, mode=new_mode),)}))
    assert PlanFlag.SCRIPT_CHANGES in plan.flags


def test_supporting_file_removal_requires_consent_but_preserves_local_only_files():
    case = merge_case(manifest=(file('source.txt'),))
    plan = case.plan(incoming=case.incoming.model_copy(update={'manifest': ()}), local=case.local.model_copy(update={'manifest': (*case.local.manifest, file('local.txt'))}))
    assert PlanFlag.DELETION_CONSENT in plan.flags
    assert next(c for c in plan.changes if c.part_id == 'file:local.txt').action == 'keep'


def test_raw_skill_file_is_compared_through_projection_only():
    case = merge_case(manifest=(file('SKILL.md'),))
    assert not case.change_file('SKILL.md', b'new').changes


def test_unknown_legacy_mode_is_a_hold_not_a_fabricated_local_edit():
    case = merge_case(manifest=(file('tool', mode=None),))
    plan = case.change_file('tool', b'new')
    assert PlanFlag.UNKNOWN_IDENTITY in plan.flags
    assert 'unknown_file_mode' in plan.conflicts[0].reason


@pytest.mark.parametrize('old_mode,new_mode,body', [(0o100755, 0o100755, b'new'), (0o100644, 0o100755, b'old'), (0o100755, 0o100644, b'old')])
def test_explicit_executable_skill_manifest_flags_without_duplicate_body_change(old_mode, new_mode, body):
    case = merge_case(manifest=(file('SKILL.md', mode=old_mode),))
    plan = case.plan(incoming=case.incoming.model_copy(update={'manifest': (file('SKILL.md', body, mode=new_mode),)}))
    assert PlanFlag.SCRIPT_CHANGES in plan.flags
    assert not plan.changes


def test_current_document_executable_mode_requires_no_invented_raw_file():
    case = merge_case(manifest=(file('SKILL.md'),))
    local = case.local.model_copy(update={'manifest': (), 'document_mode': 0o100755})
    incoming = case.incoming.model_copy(update={'manifest': (file('SKILL.md', b'new'),)})
    plan = case.plan(local=local, incoming=incoming)
    assert PlanFlag.SCRIPT_CHANGES in plan.flags
    assert not plan.changes


def test_document_mode_has_an_apply_unit_without_copying_raw_markdown():
    from merge_cases import part
    case = merge_case(part('field:document_mode', 0o100644), manifest=(file('SKILL.md'),))
    incoming = case.incoming.model_copy(update={
        'effective_projection': (part('field:document_mode', 0o100755),),
        'manifest': (file('SKILL.md', mode=0o100755),)})
    plan = case.plan(incoming=incoming)
    assert [(change.part_id, change.action, change.incoming.value) for change in plan.changes] == [
        ('field:document_mode', 'replace', 0o100755)]
    assert PlanFlag.SCRIPT_CHANGES in plan.flags


def test_generated_overflow_is_projected_as_rules_but_reserved_instruction_files_remain_compared():
    overflow = file('references/edge-cases.md', mode=0o100755).model_copy(update={
        'published': False, 'exclusion_reason': 'generated_rule_overflow'})
    reserved = file('CLAUDE.md').model_copy(update={'published': False, 'exclusion_reason': 'reserved_instruction_file'})
    case = merge_case(manifest=(overflow, reserved))
    incoming = case.incoming.model_copy(update={'manifest': (
        file(overflow.path, b'new', mode=0o100755).model_copy(update={'published': False, 'exclusion_reason': 'generated_rule_overflow'}),
        file(reserved.path, b'new').model_copy(update={'published': False, 'exclusion_reason': 'reserved_instruction_file'}))})
    plan = case.plan(incoming=incoming)
    assert {change.part_id for change in plan.changes} == {'file:CLAUDE.md'}
    assert PlanFlag.SCRIPT_CHANGES in plan.flags
