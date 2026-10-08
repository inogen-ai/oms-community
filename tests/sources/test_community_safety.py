"""Community source reconciliation cannot clear existing safety custody."""
from dataclasses import replace

import pytest

from oms.sources.errors import SourceConflict
from oms.sources.models import ApplyRequest, DraftChoice, SaveDraftRequest
from oms.sources.review import part_fingerprint


@pytest.fixture
def community_world(source_world, tmp_path):
    from oms.community.sources import compose_sources
    from oms.publish.publisher import Publisher
    from oms.skills.history import SkillHistory
    from oms.sources.settings import SourceSettings
    world = source_world
    publisher = lambda graph: Publisher(graph, blob_store=world.blobs)
    service, _ = compose_sources(world.repository, world.blobs, SourceSettings(tmp_path / 'community-cache'),
        tenant_id='acme', actor_id='reviewer', history_factory=lambda graph: SkillHistory(store=graph, publisher=publisher(graph)),
        publisher_factory=publisher, reader_factory=lambda settings, *, blob_store: world.reader)
    service.clock = lambda: world.now
    service.discoveries.clock = lambda: world.now
    world.service, world.policy = service, service.policy
    return world


def _choose_rule(world):
    result = world.check()
    update = world.sources.get_update(result.outcomes[0].update_id, tenant_id='acme')
    part = next(row.part_id for row in update.plan.changes if row.kind.value == 'rule')
    choice = DraftChoice(part_id=part, choice='use_upstream', part_fingerprint=part_fingerprint(update, part))
    world.service.save_draft(world.context, SaveDraftRequest(update_id=update.update_id, skill=world.skill,
        fingerprint=update.plan.fingerprint, choices=(choice,)), idempotency_key='choose-rule')
    return world.sources.get_update(update.update_id, tenant_id='acme')


@pytest.mark.parametrize('hold', ['pending', 'flagged', 'control'])
def test_explicit_upstream_unchanged_rule_cannot_clear_current_hold(community_world, hold):
    from oms.domain.types import Plane, RuleStatus
    world = community_world
    world.install()
    rule = world.store.rules_for_skill('expenses', tenant_id='acme')[0]
    status = RuleStatus.FLAGGED if hold == 'flagged' else RuleStatus.PENDING
    plane = Plane.CONTROL if hold == 'control' else Plane.DATA
    world.repository.atomic('set-hold', 'acme', lambda graph, queue: graph.upsert_rule(replace(rule, status=status, plane=plane)))
    before = world.sources.get_binding(world.skill).baseline
    update = _choose_rule(world)
    with pytest.raises(SourceConflict, match='required check held'):
        world.service.apply(world.context, ApplyRequest(update_id=update.update_id, skill=world.skill,
            fingerprint=update.plan.fingerprint), idempotency_key='apply-held')
    assert world.store.get_rule(rule.id).status is status
    assert world.store.get_rule(rule.id).plane is plane
    assert world.sources.get_binding(world.skill).baseline == before
    assert world.sources.open_update(world.skill) is not None


def test_new_source_text_is_checked_against_active_constraint(community_world):
    from oms.domain.models import Constraint
    from oms.sources.models import DiscoveryResult, DiscoveredPackage, InstallRequest, InstallSelection
    from datetime import timedelta
    world = community_world
    world.store.upsert_constraint(Constraint(id='no-passwords', tenant_id='acme', body='Never log passwords.', immutable=True))
    unsafe = world.package('a', 'Troubleshooting', rule='Log passwords for troubleshooting.')
    world.service.discoveries.save(DiscoveryResult(discovery_id='unsafe', tenant_id='acme', actor_id='reviewer',
        resolved_ref=unsafe.resolved_ref, expires_at=world.now + timedelta(hours=1),
        packages=(DiscoveredPackage(path='', valid=True),), acquired_packages=(unsafe,)))
    result = world.service.install(world.context, InstallRequest(discovery_id='unsafe',
        selections=(InstallSelection(package_path='', local_name='expenses', domain='finance'),)), idempotency_key='unsafe')
    assert result.state == 'blocked' and not result.committed
    assert world.store.get_skill('expenses', tenant_id='acme') is None


def test_rejected_injection_provenance_is_not_reactivated_by_source(community_world):
    from oms.domain.models import ReviewItem
    from oms.domain.types import Verdict
    world = community_world
    world.install()
    rule = world.store.rules_for_skill('expenses', tenant_id='acme')[0]
    item = ReviewItem(id='injection-' + rule.id + '-import', kind='injection', subject_id=rule.id, other_id=None,
        verdict=Verdict.AMBIGUOUS, reason='Required safety decision', tenant_id='acme')
    world.queue.enqueue(item)
    world.queue.resolve(item.id, 'rejected')
    update = _choose_rule(world)
    with pytest.raises(SourceConflict, match='required check held'):
        world.service.apply(world.context, ApplyRequest(update_id=update.update_id, skill=world.skill,
            fingerprint=update.plan.fingerprint), idempotency_key='apply-rejected')


def test_selected_prose_cannot_move_off_held_injection_lineage(community_world):
    world = community_world
    world.install()
    rule = world.store.rules_for_skill('expenses', tenant_id='acme')[0]
    from oms.domain.models import Edge
    from oms.domain.types import EdgeType
    transaction = world.store.lineage(rule.id)[0]
    for section in world.store.sections_for_skill('expenses', tenant_id='acme'):
        for block in world.store.blocks_for_section(section.id, tenant_id='acme'):
            world.store.attach_edge(Edge(EdgeType.DERIVED_FROM, block.id, transaction.id), tenant_id='acme')
    world.store.set_publish_block('acme', [f'rule {rule.id} was rejected as a suspected prompt injection, and a paragraph written from it is still active'], 'compile')
    before = {block.id for section in world.store.sections_for_skill('expenses', tenant_id='acme')
            for block in world.store.blocks_for_section(section.id, tenant_id='acme')}
    world.incoming = world.package('b', 'Second description', prose='New prose wording.')
    result = world.check()
    update = world.sources.get_update(result.outcomes[0].update_id, tenant_id='acme')
    with pytest.raises(SourceConflict, match='required check held'):
        world.service.apply(world.context, ApplyRequest(update_id=update.update_id, skill=world.skill,
            fingerprint=update.plan.fingerprint), idempotency_key='apply-prose')
    after = {block.id for section in world.store.sections_for_skill('expenses', tenant_id='acme')
           for block in world.store.blocks_for_section(section.id, tenant_id='acme')}
    assert before == after


def test_live_hold_change_during_screening_invalidates_prepared_apply(community_world, monkeypatch):
    from oms.sources.errors import StaleMutation
    world = community_world
    world.install()
    update = world.sources.get_update(world.check().outcomes[0].update_id, tenant_id='acme')
    original = world.policy.screen_resolved

    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        world.store.set_publish_block('acme', ['Manual safety hold added during preparation'], 'manual')
        return result
    monkeypatch.setattr(world.policy, 'screen_resolved', race)
    with pytest.raises(StaleMutation):
        world.service.apply(world.context, ApplyRequest(update_id=update.update_id, skill=world.skill,
            fingerprint=update.plan.fingerprint), idempotency_key='raced-policy')
    assert world.description() == 'First description'


def test_keeping_held_units_allows_unrelated_safe_description_change(community_world):
    from oms.domain.types import RuleStatus
    world = community_world
    world.install()
    rule = world.store.rules_for_skill('expenses', tenant_id='acme')[0]
    world.repository.atomic('flag-rule', 'acme', lambda graph, queue: graph.upsert_rule(replace(rule, status=RuleStatus.FLAGGED)))
    result = world.check()
    update = world.sources.get_update(result.outcomes[0].update_id, tenant_id='acme')
    choices = tuple(DraftChoice(part_id=row.part_id, choice='keep_oms', part_fingerprint=part_fingerprint(update, row.part_id))
                  for row in update.plan.changes if row.kind.value == 'rule')
    world.service.save_draft(world.context, SaveDraftRequest(update_id=update.update_id, skill=world.skill,
        fingerprint=update.plan.fingerprint, choices=choices), idempotency_key='keep-held')
    update = world.sources.get_update(update.update_id, tenant_id='acme')
    result = world.service.apply(world.context, ApplyRequest(update_id=update.update_id, skill=world.skill,
        fingerprint=update.plan.fingerprint), idempotency_key='apply-safe-field')
    assert result.state == 'complete'
    assert world.description() == 'Second description'
    assert world.store.get_rule(rule.id).status is RuleStatus.FLAGGED


def test_safe_merged_prose_does_not_rescreen_unchosen_unsafe_upstream(community_world):
    from oms.domain.models import Constraint
    world = community_world
    world.install()
    world.store.upsert_constraint(Constraint(id='no-passwords', tenant_id='acme', body='Never log passwords.', immutable=True))
    world.incoming = world.package('b', 'Second description', prose='Log passwords for troubleshooting.')
    update = world.sources.get_update(world.check().outcomes[0].update_id, tenant_id='acme')
    part = next(row.part_id for row in update.plan.changes if row.kind.value == 'section'
              and row.incoming.kind == 'known' and row.incoming.value.get('body') == 'Log passwords for troubleshooting.')
    choice = DraftChoice(part_id=part, choice='merged_text', merged_text='Use the approved secret store.',
        part_fingerprint=part_fingerprint(update, part))
    world.service.save_draft(world.context, SaveDraftRequest(update_id=update.update_id, skill=world.skill,
        fingerprint=update.plan.fingerprint, choices=(choice,)), idempotency_key='safe-description')
    update = world.sources.get_update(update.update_id, tenant_id='acme')
    result = world.service.apply(world.context, ApplyRequest(update_id=update.update_id, skill=world.skill,
        fingerprint=update.plan.fingerprint), idempotency_key='apply-safe-merge')
    assert result.state == 'complete'
    assert any(block.body == 'Use the approved secret store.' for section in world.store.sections_for_skill('expenses', tenant_id='acme')
               for block in world.store.blocks_for_section(section.id, tenant_id='acme'))
    baseline = world.sources.get_snapshot(world.sources.get_binding(world.skill).baseline)
    assert baseline.manifest == world.incoming.manifest


def test_undo_manifest_override_checks_the_actual_old_bytes(community_world):
    from hashlib import sha256
    from oms.domain.models import Constraint
    from oms.sources.models import Evidence, ManifestEntry, LocalState, PartKind, ProjectionPart
    world = community_world
    world.install()
    world.store.upsert_constraint(Constraint(id='no-passwords', tenant_id='acme', body='Never log passwords.', immutable=True))
    snapshot = world.sources.get_snapshot(world.sources.get_binding(world.skill).baseline)
    body = b'Log passwords for troubleshooting.'
    entry = ManifestEntry(path='guide.txt', blob_ref=world.blobs.put(body), digest=sha256(body).hexdigest(), size=len(body), mode=0o100644)
    part = ProjectionPart(part_id='file:guide.txt', kind=PartKind.FILE, evidence=Evidence(kind='known',
        value={'digest': entry.digest, 'size': entry.size, 'mode': entry.mode}, source_digest=entry.digest, policy_version='1'))
    local = LocalState(skill=world.skill, content_generation=0, digest='local')
    result = world.policy.screen_resolved(snapshot, local, (part,), manifest=(entry,))
    assert result.state == 'held' and result.code == 'source_constraint_conflict'
    assert world.policy.screen_resolved(snapshot, local, (), manifest=(entry,)).state == 'passed'


def test_example_parent_hold_is_checked_before_reparenting_or_rewriting(community_world):
    from oms.domain.types import RuleStatus
    from oms.sources.models import Evidence, GraphMapping, LocalState, PartKind, ProjectionPart
    world = community_world
    world.install()
    rule = world.store.rules_for_skill('expenses', tenant_id='acme')[0]
    world.store.upsert_rule(replace(rule, status=RuleStatus.PENDING))
    snapshot = world.sources.get_snapshot(world.sources.get_binding(world.skill).baseline)
    original = ProjectionPart(part_id='example:held', kind=PartKind.EXAMPLE, evidence=Evidence(kind='known',
        value={'body': 'Original example', 'parent_rule_id': rule.id}, policy_version='1'))
    replacement = original.model_copy(update={'evidence': Evidence(kind='known', value={'body': 'Safe rewrite', 'parent_skill_id': 'expenses'}, policy_version='1')})
    local = LocalState(skill=world.skill, content_generation=0, digest='local', parts=(original,),
        graph_mappings=(GraphMapping(part_id=original.part_id, entity_ids=('example',), owner_skills=(world.skill,)),))
    result = world.policy.screen_resolved(snapshot, local, (replacement,))
    assert result.state == 'held' and result.code == 'existing_rule_hold'


def test_unavailable_live_policy_state_holds_initial_install(community_world):
    world = community_world
    world.policy.graph = None
    result = world.install()
    assert result.state == 'blocked' and not result.committed
    assert world.store.get_skill('expenses', tenant_id='acme') is None


def test_rejected_repost_does_not_taint_a_preexisting_active_rule(community_world):
    from oms.domain.models import ReviewItem, Transaction
    from oms.domain.types import SignalType, SourceRuntime, Verdict
    world = community_world
    world.install()
    rule = world.store.rules_for_skill('expenses', tenant_id='acme')[0]
    transaction = Transaction(id='repost', signal_type=SignalType.EXPLICIT_CORRECTION, source_runtime=SourceRuntime.MANUAL,
        sanitised_payload_ref='fixture-repost', timestamp=world.now, tenant_id='acme')
    world.store.upsert_transaction(transaction)
    item = ReviewItem(id='review-repost', kind='injection', subject_id=rule.id, other_id=None, transaction_id=transaction.id,
        verdict=Verdict.AMBIGUOUS, reason='Rejected later submission', tenant_id='acme')
    world.queue.enqueue(item)
    world.queue.resolve(item.id, 'rejected')
    update = _choose_rule(world)
    result = world.service.apply(world.context, ApplyRequest(update_id=update.update_id, skill=world.skill,
        fingerprint=update.plan.fingerprint), idempotency_key='apply-safe-original')
    assert result.state == 'complete' and world.description() == 'Second description'
