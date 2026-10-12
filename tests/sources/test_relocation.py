"""Explicit repository relocation preserves content custody without fetching."""
from dataclasses import replace

import pytest

from oms.sources.errors import SourceConflict, SourceForbidden, StaleMutation

DESTINATION = 'https://github.com/example/relocated.git'


def relocation(world, destination=DESTINATION):
    from oms.sources.models import RelocateSourceRequest
    binding = world.sources.get_binding(world.skill)
    source = world.sources.get_source(binding.source_id, tenant_id='acme')
    return RelocateSourceRequest(source_id=source.source_id, destination_url=destination,
        expected_source_generation=source.generation, expected_generations=(world.sources.get_generations(world.skill),))


def test_relocation_preserves_baseline_cards_drafts_and_content_without_fetch(source_world, monkeypatch):
    from oms.sources.models import DraftChoice, SaveDraftRequest
    from oms.sources.review import part_fingerprint
    world = source_world
    candidate = world.open_update()
    card = candidate.update
    choice = DraftChoice(part_id='field:description', choice='keep_oms', part_fingerprint=part_fingerprint(card, 'field:description'))
    world.service.save_draft(world.context, SaveDraftRequest(update_id=card.update_id, skill=world.skill,
        fingerprint=card.plan.fingerprint, choices=(choice,)), idempotency_key='saved-choice')
    card = world.sources.get_update(card.update_id, tenant_id='acme')
    binding = world.sources.get_binding(world.skill)
    source = world.sources.get_source(binding.source_id, tenant_id='acme')
    versions = world.store.skill_versions('acme', 'expenses')
    guard = world.sources.get_generations(world.skill)

    def forbidden(*args, **kwargs):
        raise AssertionError('Relocation fetched or screened content')
    for name in ('read', 'resolve', 'discover'):
        monkeypatch.setattr(world.reader, name, forbidden)
    monkeypatch.setattr(world.policy, 'screen', forbidden)
    request = relocation(world)
    result = world.service.relocate_source(world.context, request, idempotency_key='relocate')
    current = world.sources.get_source(source.source_id, tenant_id='acme')
    moved = world.sources.get_binding(world.skill)
    assert result.state == 'complete' and not result.committed and result.source_id == source.source_id
    assert current.canonical_url == DESTINATION and current.generation == source.generation + 1
    assert {source.canonical_url, DESTINATION} <= set(current.confirmed_aliases)
    assert moved.model_copy(update={'ref': binding.ref, 'status': binding.status}) == binding
    assert moved.ref == binding.ref.model_copy(update={'canonical_url': DESTINATION})
    assert current.status.state == moved.status.state == 'unchecked'
    assert current.status.checked_at is moved.status.checked_at is None
    assert world.sources.get_generations(world.skill) == guard
    assert world.sources.get_update(card.update_id, tenant_id='acme') == card
    assert world.store.skill_versions('acme', 'expenses') == versions
    assert world.description() == 'First description'
    assert world.service.relocate_source(world.context, request, idempotency_key='relocate') == result
    assert world.sources.get_source(source.source_id, tenant_id='acme') == current


@pytest.mark.parametrize('url', ['http://github.com/example/repo', 'https://user:password@github.com/example/repo',
    'https://other.example/example/repo', 'https://github.com/example/repo/tree/main',
    'https://github.com/example/repo/blob/main/SKILL.md', 'https://github.com/example/repo?x=1',
    'https://github.com/example/repo#part', 'https://github.com/example/repo?',
    'https://github.com/example/repo/extra', 'https://github.com/example/%2e%2e'])
def test_relocation_refuses_non_repository_destinations(source_world, url):
    from oms.sources.links import LinkError
    world = source_world
    world.install()
    request = relocation(world, url)
    before = world.sources.get_binding(world.skill)
    with pytest.raises(LinkError):
        world.service.relocate_source(world.context, request, idempotency_key='bad-location')
    assert world.sources.get_binding(world.skill) == before


def test_same_canonical_destination_is_an_explicit_noop(source_world):
    world = source_world
    world.install()
    binding = world.sources.get_binding(world.skill)
    source = world.sources.get_source(binding.source_id, tenant_id='acme')
    result = world.service.relocate_source(world.context, relocation(world, source.canonical_url), idempotency_key='same-location')
    assert result.state == 'complete' and not result.committed
    assert result.outcomes[0].code == 'repository_unchanged'
    assert world.sources.get_source(source.source_id, tenant_id='acme') == source
    assert world.sources.get_binding(world.skill) == binding


def test_full_guard_list_and_current_authority_are_required_even_for_replay(source_world, monkeypatch):
    world = source_world
    world.install()
    request = relocation(world)
    for guards in ((), request.expected_generations * 2):
        with pytest.raises(StaleMutation):
            world.service.relocate_source(world.context, request.model_copy(update={'expected_generations': guards}), idempotency_key='bad-scope')
    result = world.service.relocate_source(world.context, request, idempotency_key='relocated')
    assert result.state == 'complete'

    def deny(context, profile):
        raise SourceForbidden('profile_revoked')
    monkeypatch.setattr(world.policy, 'admit_profile', deny)
    with pytest.raises(SourceForbidden):
        world.service.relocate_source(world.context, request, idempotency_key='relocated')


def test_event_failure_rolls_back_aliases_bindings_and_source_generation(source_world, monkeypatch):
    from dataclasses import replace
    world = source_world
    world.install()
    before = world.sources.get_binding(world.skill)
    source = world.sources.get_source(before.source_id, tenant_id='acme')
    original = world.service.factory_for

    def factory_for(*args):
        factory = original(*args)

        def bind(graph, reviews):
            bound = factory(graph, reviews)

            class Events:
                def append_event(self, event):
                    raise OSError('required relocation event unavailable')
            return replace(bound, events=Events())
        return bind
    monkeypatch.setattr(world.service, 'factory_for', factory_for)
    with pytest.raises(OSError):
        world.service.relocate_source(world.context, relocation(world), idempotency_key='failed-relocation')
    assert world.sources.get_source(source.source_id, tenant_id='acme') == source
    assert world.sources.get_binding(world.skill) == before
    assert world.sources.source_for_url(DESTINATION, tenant_id='acme') is None


def test_existing_destination_identity_cannot_be_stolen(source_world):
    from oms.sources.models import Source
    world = source_world
    world.install()
    source = world.sources.get_source(world.sources.get_binding(world.skill).source_id, tenant_id='acme')
    other = Source(source_id='other-source', tenant_id='acme', canonical_url=DESTINATION)
    world.sources.put_source(other)
    with pytest.raises(SourceConflict, match='repository url already owned'):
        world.service.relocate_source(world.context, relocation(world), idempotency_key='collision')
    assert world.sources.get_source(source.source_id, tenant_id='acme') == source
    assert world.sources.source_for_url(DESTINATION, tenant_id='acme') == other


def test_subsequent_check_fetches_new_endpoint_and_retains_history_proof(source_world):
    from oms.sources.models import PlanFlag
    world = source_world
    world.install()
    base = world.sources.get_binding(world.skill).baseline
    world.service.relocate_source(world.context, relocation(world), idempotency_key='move-before-fetch')
    requests = []

    def resolve(request):
        requests.append(request)
        assert request.url == DESTINATION
        return world.incoming.resolved_ref.model_copy(update={'canonical_url': DESTINATION})

    def read(request):
        requests.append(request)
        assert request.resolved_ref.canonical_url == DESTINATION
        assert request.baseline_commit == 'a' * 40
        return world.incoming.model_copy(update={'resolved_ref': request.resolved_ref, 'history_evidence': 'unproven'})
    world.reader.resolve = resolve
    world.reader.read = read
    result = world.check()
    assert len(requests) == 2 and result.state == 'awaiting_review'
    card = world.sources.open_update(world.skill)
    assert PlanFlag.UNPROVEN_HISTORY in card.plan.flags
    assert world.sources.get_binding(world.skill).baseline == base


def test_relocation_preserves_pinned_commit_and_check_does_not_resolve_branch(source_world):
    world = source_world
    world.install()
    binding = world.sources.get_binding(world.skill)
    origin = binding.origin.model_copy(update={'generation': binding.origin.generation + 1})
    pinned = binding.model_copy(update={'origin': origin, 'ref': binding.ref.model_copy(update={'kind': 'commit', 'name': 'a' * 40})})
    world.sources.put_binding(pinned)
    world.service.relocate_source(world.context, relocation(world), idempotency_key='move-pin')
    moved = world.sources.get_binding(world.skill)
    assert moved.origin == origin and moved.ref.kind == 'commit' and moved.ref.commit == 'a' * 40

    def forbidden(*args):
        raise AssertionError('Pinned check resolved a moving ref')
    world.reader.resolve = forbidden

    def read(request):
        assert request.resolved_ref.canonical_url == DESTINATION
        assert request.resolved_ref.commit == 'a' * 40
        package = world.package('a', 'First description')
        return package.model_copy(update={'resolved_ref': request.resolved_ref, 'history_evidence': 'proven_ancestor'})
    world.reader.read = read
    assert world.check().outcomes[0].state == 'unchanged'


def test_added_peer_and_profile_revocation_after_preparation_refuse_atomically(source_world, monkeypatch):
    from oms.domain.models import Skill
    from oms.domain.identity import SkillRef
    from oms.sources.models import OriginRef
    world = source_world
    world.install()
    before = world.sources.get_binding(world.skill)
    request = relocation(world)
    atomic = world.repository.atomic_skill_change

    def add_peer_then_commit(*args, **kwargs):
        world.store.upsert_skill(Skill(id='peer', tenant_id='acme', name='Peer', description='', domain='finance'))
        world.sources.put_binding(before.model_copy(update={'origin': OriginRef(skill=SkillRef('acme', 'peer'),
            origin_id='peer-origin', kind='github', generation=1), 'package_path': 'peer', 'baseline': None}))
        return atomic(*args, **kwargs)
    monkeypatch.setattr(world.repository, 'atomic_skill_change', add_peer_then_commit)
    with pytest.raises(StaleMutation):
        world.service.relocate_source(world.context, request, idempotency_key='new-peer')
    assert world.sources.get_binding(world.skill) == before


def test_profile_is_readmitted_at_native_commit_boundary(source_world, monkeypatch):
    world = source_world
    world.install()
    before = world.sources.get_binding(world.skill)
    calls = []

    def admit(context, profile):
        calls.append(profile)
        if len(calls) > 1:
            raise SourceForbidden('source_action_forbidden', 'source profile access denied')
    monkeypatch.setattr(world.policy, 'admit_profile', admit)
    with pytest.raises(SourceForbidden):
        world.service.relocate_source(world.context, relocation(world), idempotency_key='profile-race')
    assert len(calls) == 2 and world.sources.get_binding(world.skill) == before


def test_new_alias_discovery_reuses_source_identity_for_another_package(source_world):
    from datetime import timedelta
    from oms.sources.models import DiscoveryResult, DiscoveredPackage, InstallRequest, InstallSelection
    from oms.domain.identity import SkillRef
    world = source_world
    world.install()
    original = world.sources.get_binding(world.skill)
    world.service.relocate_source(world.context, relocation(world), idempotency_key='moved')
    package = world.package('a', 'New package').model_copy(update={'package_path': 'extra',
        'resolved_ref': world.incoming.resolved_ref.model_copy(update={'canonical_url': DESTINATION})})
    world.discoveries.save(DiscoveryResult(discovery_id='new-alias', tenant_id='acme', actor_id='reviewer',
        resolved_ref=package.resolved_ref, expires_at=world.now + timedelta(hours=1),
        packages=(DiscoveredPackage(path='extra', valid=True),), acquired_packages=(package,)))
    result = world.service.install(world.context, InstallRequest(discovery_id='new-alias', selections=(
        InstallSelection(package_path='extra', local_name='extra', domain='finance'),)), idempotency_key='extra-package')
    assert result.state == 'complete'
    assert world.sources.get_binding(SkillRef('acme', 'extra')).source_id == original.source_id
    assert world.sources.source_for_url(DESTINATION, tenant_id='acme').source_id == original.source_id


def test_http_relocation_uses_typed_receipt_and_replay(source_world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from oms.sources.api import source_routes
    from oms.sources.reads import SourceReads
    world = source_world
    world.install()
    request = relocation(world)
    app = FastAPI()
    app.include_router(source_routes(world.service, SourceReads(world.service, world.blobs), lambda request, action: world.context).router)
    client = TestClient(app)
    body = {'destination_url': request.destination_url, 'expected_source_generation': request.expected_source_generation,
        'expected_generations': [{'skill_id': row.skill.skill_id, 'content': row.content, 'binding': row.binding} for row in request.expected_generations]}
    path = '/api/skill-sources/' + request.source_id + '/relocate'
    result = client.post(path, json=body, headers={'Idempotency-Key': 'http-relocate'})
    assert result.status_code == 200, result.text
    assert result.json()['source_id'] == request.source_id and result.json()['committed'] is False
    assert client.post(path, json=body, headers={'Idempotency-Key': 'http-relocate'}).json() == result.json()
    assert client.post(path, json=body, headers={'Idempotency-Key': 'stale-http-relocate'}).status_code == 409


def test_orphan_active_binding_cannot_be_omitted_from_source_guard_set(source_world):
    from oms.domain.identity import SkillRef
    from oms.sources.models import OriginRef
    world = source_world
    world.install()
    request = relocation(world)
    binding = world.sources.get_binding(world.skill)
    world.sources.put_binding(binding.model_copy(update={'origin': OriginRef(skill=SkillRef('acme', 'orphan'),
        origin_id='orphan-origin', kind='github', generation=1), 'package_path': 'orphan', 'baseline': None}))
    with pytest.raises(StaleMutation, match='source scope changed'):
        world.service.relocate_source(world.context, request, idempotency_key='omitted-orphan')
    assert world.sources.get_binding(world.skill) == binding


def test_existing_profile_is_required_when_reusing_alias_discovery(source_world, monkeypatch):
    from datetime import timedelta
    from oms.sources.models import DiscoveryResult, DiscoveredPackage, InstallRequest, InstallSelection, CreateSourceRequest
    world = source_world
    world.install()
    source = world.sources.get_source(world.sources.get_binding(world.skill).source_id, tenant_id='acme')
    source = source.model_copy(update={'credential_profile_id': 'private-profile'})
    world.sources.put_source(source)
    package = world.package('a', 'Extra').model_copy(update={'package_path': 'extra'})
    world.discoveries.save(DiscoveryResult(discovery_id='public-alias', tenant_id='acme', actor_id='reviewer',
        resolved_ref=package.resolved_ref, credential_profile_id=None, expires_at=world.now + timedelta(hours=1),
        packages=(DiscoveredPackage(path='extra', valid=True),), acquired_packages=(package,)))

    def profile(context, identifier):
        if identifier == 'private-profile':
            raise SourceForbidden('source_action_forbidden', 'source profile access denied')
    monkeypatch.setattr(world.policy, 'admit_profile', profile)
    with pytest.raises(SourceForbidden):
        world.service.create_source(world.context, CreateSourceRequest(discovery_id='public-alias'), idempotency_key='create-reuse')
    with pytest.raises(SourceForbidden):
        world.service.install(world.context, InstallRequest(discovery_id='public-alias', selections=(
            InstallSelection(package_path='extra', local_name='extra', domain='finance'),)), idempotency_key='install-reuse')
    assert world.store.get_skill('extra', tenant_id='acme') is None
    assert world.sources.get_source(source.source_id, tenant_id='acme') == source


def test_preserved_card_apply_and_undo_keep_approved_endpoint_unchecked(source_world):
    from oms.sources.models import ApplyRequest, UndoRequest
    world = source_world
    candidate = world.open_update()
    old_base = world.sources.get_binding(world.skill).baseline
    world.service.relocate_source(world.context, relocation(world), idempotency_key='relocate-before-apply')
    card = candidate.update
    result = world.service.apply(world.context, ApplyRequest(update_id=card.update_id, skill=world.skill,
        fingerprint=card.plan.fingerprint), idempotency_key='apply-retained')
    assert result.state == 'complete' and world.description() == 'Second description'
    binding = world.sources.get_binding(world.skill)
    assert binding.ref.canonical_url == DESTINATION and binding.status.state == 'unchecked'
    receipt = world.sources.get_undo(result.operation_id + ':undo', tenant_id='acme')
    undo = UndoRequest(undo_id=receipt.undo_id, skill=world.skill, expected_generations=receipt.expected_generations,
                     policy_version=receipt.policy_version)
    world.service.undo(world.context, undo, idempotency_key='undo-retained')
    binding = world.sources.get_binding(world.skill)
    assert binding.ref.canonical_url == DESTINATION and binding.baseline == old_base
    assert binding.status.state == 'unchecked' and binding.status.checked_at is None
    assert world.description() == 'First description'


def test_inflight_apply_is_fenced_by_holder_endpoint_change(source_world, monkeypatch):
    from oms.sources.models import ApplyRequest
    world = source_world
    candidate = world.open_update()
    original = world.policy.screen_resolved

    def relocate_during_screen(*args, **kwargs):
        result = original(*args, **kwargs)
        world.service.relocate_source(world.context, relocation(world), idempotency_key='move-inflight')
        return result
    monkeypatch.setattr(world.policy, 'screen_resolved', relocate_during_screen)
    with pytest.raises(StaleMutation):
        world.service.apply(world.context, ApplyRequest(update_id=candidate.update.update_id, skill=world.skill,
            fingerprint=candidate.update.plan.fingerprint), idempotency_key='old-holder-apply')
    assert world.description() == 'First description'
    assert world.sources.get_binding(world.skill).ref.canonical_url == DESTINATION
    assert world.sources.open_update(world.skill) is not None


def test_relocation_fences_an_already_fetching_check(source_world):
    world = source_world
    world.install()
    world.reader.before_read = lambda: world.service.relocate_source(world.context, relocation(world), idempotency_key='move-during-check')
    with pytest.raises(StaleMutation):
        world.check()
    assert world.description() == 'First description'
    assert world.sources.get_binding(world.skill).ref.canonical_url == DESTINATION
    assert world.sources.open_update(world.skill) is None


def test_undo_created_before_relocation_does_not_restore_old_endpoint(source_world):
    from oms.sources.models import UndoRequest
    world = source_world
    result = world.apply(world.open_update())
    receipt = world.sources.get_undo(result.operation_id + ':undo', tenant_id='acme')
    world.service.relocate_source(world.context, relocation(world), idempotency_key='move-before-undo')
    assert world.service.undo_status(world.context, receipt.undo_id).available
    world.service.undo(world.context, UndoRequest(undo_id=receipt.undo_id, skill=world.skill,
        expected_generations=receipt.expected_generations, policy_version=receipt.policy_version), idempotency_key='undo-after-move')
    binding = world.sources.get_binding(world.skill)
    assert binding.ref.canonical_url == DESTINATION and binding.status.state == 'unchecked'
    assert world.description() == 'First description'
