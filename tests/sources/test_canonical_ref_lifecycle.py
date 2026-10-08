"""Stored discovery refs survive service acquisition and link revalidation."""
import pytest

from oms.domain.models import Skill
from oms.sources.git_reader import GitReader
from oms.sources.models import CreateSourceRequest, DiscoveryRequest, InstallRequest, InstallSelection, LinkRequest, RefRequest
from oms.sources.settings import SourceSettings
from fixture_git import FixtureGitTransport


@pytest.fixture
def real_reader(source_world, tmp_path):
    world = source_world
    transport = FixtureGitTransport(tmp_path / 'remote')
    endpoint = 'https://github.com/example/roundtrip.git'
    commit = transport.register(endpoint, {'SKILL.md': b'---\nname: expenses\ndescription: Source content\n---\n## Procedure\nKeep the receipts.\n'}, branch='feature/tools')
    transport._git(transport.registered[endpoint], 'update-ref', 'refs/tags/releases/v1', commit)
    world.service.reader = GitReader(SourceSettings(tmp_path / 'cache'), blob_store=world.blobs, transport=transport)
    return world, endpoint


def discover(world, endpoint, kind):
    return world.service.discover(world.context, DiscoveryRequest(actor_id=world.context.actor_id,
        ref=RefRequest(tenant_id='acme', url=endpoint, ref_kind=kind,
            ref_name='feature/tools' if kind == 'branch' else 'releases/v1')), idempotency_key='discover-real')


def test_check_reuses_full_branch_ref_retained_by_discovery(real_reader):
    world, endpoint = real_reader
    discovery = discover(world, endpoint, 'branch')
    assert discovery.resolved_ref.name == 'refs/heads/feature/tools'
    installed = world.service.install(world.context, InstallRequest(discovery_id=discovery.discovery_id,
        selections=(InstallSelection(package_path='', local_name='expenses', domain='finance'),)), idempotency_key='install-real')
    assert installed.committed
    result = world.check('check-real')
    assert result.state == 'complete'
    assert result.outcomes[0].state == 'unchanged'
    assert world.sources.get_binding(world.skill).ref == discovery.resolved_ref


@pytest.mark.parametrize('kind', ['branch', 'tag'])
def test_link_revalidates_full_canonical_ref_from_discovery(real_reader, kind):
    world, endpoint = real_reader
    world.store.upsert_skill(Skill(id='expenses', name='Expenses', description='Local correction', domain='finance',
        tenant_id='acme', import_source_ref='legacy/SKILL.md'))
    discovery = discover(world, endpoint, kind)
    source = world.service.create_source(world.context, CreateSourceRequest(discovery_id=discovery.discovery_id), idempotency_key='source-real')
    guard = world.sources.get_generations(world.skill)
    request = LinkRequest(skill=world.skill, source_id=source.source_id, package_path='', ref=discovery.resolved_ref,
        expected_content_generation=guard.content, expected_binding_generation=guard.binding)
    result = world.service.link(world.context, request, idempotency_key='link-real')
    assert result.state == 'awaiting_review'
    assert world.sources.get_binding(world.skill).ref == discovery.resolved_ref
    assert world.description() == 'Local correction'
