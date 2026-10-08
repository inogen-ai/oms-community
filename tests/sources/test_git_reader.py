from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from oms.adapters.blobs.file_blob_store import FileBlobStore
from oms.sources.git_reader import GitReader, GitTransport, AcquisitionError
from oms.sources.limits import AcquisitionLimits, BudgetExceeded
from oms.sources.models import DiscoveryRequest, PackageRequest, RefRequest
from oms.sources.settings import SourceSettings
from tests.sources.fixture_git import FixtureGitTransport


@pytest.fixture(params=[False, True], ids=['unfiltered', 'filtered'])
def reader_case(tmp_path, request):
    transport = FixtureGitTransport(tmp_path / 'fixtures')
    endpoint = 'https://github.com/example/skills.git'
    commit = transport.register(endpoint, {
        'tools/SKILL.md': b'---\nname: tools\ndescription: Test tools\nlicense: MIT\n---\n# Tools\n',
        'tools/scripts/run.sh': b'echo harmless\n',
        'tools/nested/SKILL.md': b'---\nname: nested\n---\n# Nested\n',
        'README.md': b'Repository only',
    }, filter_blobs=request.param)
    blobs = FileBlobStore(tmp_path / 'custody')
    reader = GitReader(SourceSettings(tmp_path / 'cache'), blob_store=blobs, transport=transport)
    return reader, transport, endpoint, commit, blobs


def request(endpoint):
    return RefRequest(tenant_id='tenant', url=endpoint)


def test_cold_cache_reads_exact_bytes_and_excludes_nested_packages(reader_case):
    reader, transport, endpoint, commit, blobs = reader_case
    resolved = reader.resolve(request(endpoint))
    assert resolved.commit == commit
    acquired = reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools'))
    assert [entry.path for entry in acquired.manifest] == ['SKILL.md', 'scripts/run.sh']
    assert blobs.get(acquired.manifest[1].blob_ref) == b'echo harmless\n'
    assert acquired.history_evidence == 'initial'
    assert acquired.raw_frontmatter.kind == 'known'
    before = transport.fetches
    assert reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools')).manifest == acquired.manifest
    assert transport.fetches == before


def test_source_reader_ignores_ambient_rewrite(reader_case, monkeypatch):
    reader, transport, endpoint, _, _ = reader_case
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'url.file:///private/.insteadOf')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', 'https://github.com/')
    reader.resolve(request(endpoint))
    assert transport.endpoints and set(transport.endpoints) == {endpoint}
    assert transport.local_path_reads == []
    assert all(env['GIT_CONFIG_COUNT'] == '0' for env in transport.environments)


@pytest.mark.parametrize('failed_probe', [False, True], ids=['native-probe', 'aborted-probe'])
def test_filtered_cache_fetches_only_selected_blobs(tmp_path, monkeypatch, failed_probe):
    transport = FixtureGitTransport(tmp_path / 'fixtures')
    endpoint = 'https://github.com/example/partial.git'
    commit = transport.register(endpoint, {
        'tools/SKILL.md': b'# Tools\n',
        'tools/script.sh': b'echo harmless\n',
        'tools/nested/SKILL.md': b'# Nested\n',
        'README.md': b'Outside the package\n',
    }, filter_blobs=True)
    blobs = FileBlobStore(tmp_path / 'custody')
    reader = GitReader(SourceSettings(tmp_path / 'cache'), blob_store=blobs, transport=transport)
    resolved = reader.resolve(request(endpoint))
    cache = next(path.parent for path in reader.settings.cache_root.glob('*/HEAD'))
    remote = transport.registered[endpoint]
    selected = {transport._git(remote, 'rev-parse', f'{commit}:{path}').strip()
                for path in ('tools/SKILL.md', 'tools/script.sh')}
    excluded = {transport._git(remote, 'rev-parse', f'{commit}:{path}').strip()
                for path in ('tools/nested/SKILL.md', 'README.md')}

    def local_objects():
        return set(transport._git(cache, 'cat-file', '--batch-all-objects',
                                  '--batch-check=%(objectname)').splitlines())

    assert not (selected | excluded) & local_objects()
    original = transport.run
    fetched = []

    def traced(args, env, cache, budget, **kwargs):
        result = original(args, env, cache, budget, **kwargs)
        if 'fetch' in args:
            fetched.append(set(kwargs['stdin'].decode('ascii').splitlines()))
        if failed_probe and '--batch-check=%(objectname) %(objecttype) %(objectsize)' in args and not fetched:
            # Older Git aborts instead of listing missing promisor objects.
            return 128, b'partial inventory\n'
        return result

    monkeypatch.setattr(transport, 'run', traced)
    package_request = PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools')
    acquired = reader.read(package_request)
    assert {entry.path: blobs.get(entry.blob_ref) for entry in acquired.manifest} == {
        'SKILL.md': b'# Tools\n', 'script.sh': b'echo harmless\n'}
    assert fetched == [selected]
    assert selected <= local_objects()
    assert not excluded & local_objects()
    assert reader.read(package_request).manifest == acquired.manifest
    assert fetched == [selected]


@pytest.mark.parametrize('failed_stage', ['fetch', 'verification'])
def test_aborted_blob_inventory_fails_closed_after_one_fetch(reader_case, monkeypatch, failed_stage):
    reader, transport, endpoint, _, _ = reader_case
    resolved = reader.resolve(request(endpoint))
    original = transport.run
    fetches = 0

    def fail(args, env, cache, budget, **kwargs):
        nonlocal fetches
        if '--batch-check=%(objectname) %(objecttype) %(objectsize)' in args:
            return 128, b'partial inventory\n'
        if 'fetch' in args:
            fetches += 1
            if failed_stage == 'fetch':
                return 128, b''
        return original(args, env, cache, budget, **kwargs)

    monkeypatch.setattr(transport, 'run', fail)
    with pytest.raises(AcquisitionError):
        reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools'))
    assert fetches == 1


def test_production_transport_rejects_private_dns_and_disables_redirects(monkeypatch):
    monkeypatch.setattr('oms.sources.git_reader.socket.getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(AcquisitionError, match='destination'):
        GitTransport().network_options()
    monkeypatch.setattr('oms.sources.git_reader.socket.getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', ('140.82.114.3', 443))])
    options = GitTransport().network_options()
    assert 'http.followRedirects=false' in options
    assert 'http.sslVerify=true' in options
    assert 'http.curloptResolve=github.com:443:140.82.114.3' in options


def test_missing_pinned_object_and_file_budget(reader_case):
    reader, _, endpoint, _, _ = reader_case
    resolved = reader.resolve(request(endpoint))
    reader.limits = replace(AcquisitionLimits(), file_bytes=5)
    with pytest.raises(BudgetExceeded, match='file'):
        reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools'))
    with pytest.raises(AcquisitionError):
        reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved.model_copy(update={'commit': 'f' * 40}), package_path='tools'))


def test_discovery_preselects_explicit_folder(reader_case):
    reader, _, endpoint, _, _ = reader_case
    discovery = reader.discover(DiscoveryRequest(ref=request(endpoint.replace('.git', '/tree/main/tools')), actor_id='alice'))
    assert discovery.preselected_path == 'tools'
    assert [package.path for package in discovery.packages] == ['tools', 'tools/nested']
    assert discovery.packages[0].upstream_name == 'tools'
    assert discovery.expires_at > datetime.now(timezone.utc) + timedelta(minutes=29)


def test_lfs_and_symlink_packages_are_not_installable(reader_case):
    reader, transport, _, _, _ = reader_case
    endpoint = 'https://github.com/example/unsafe.git'
    transport.register(endpoint, {'SKILL.md': b'# Test', 'asset': b'version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 1\n'})
    resolved = reader.resolve(request(endpoint))
    with pytest.raises(AcquisitionError, match='LFS'):
        reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path=''))


def test_discoveries_are_bound_expiring_and_restart_safe(reader_case):
    from oms.sources.discovery import Discoveries, DiscoveryAccessError
    from oms.adapters.memory.source_store import InMemorySourceRepository
    from oms.adapters.memory.store import InMemoryGraphStore
    reader, _, endpoint, _, blobs = reader_case
    result = reader.discover(DiscoveryRequest(ref=request(endpoint), actor_id='alice'))

    graph = InMemoryGraphStore()
    repo = InMemorySourceRepository(graph)
    store = Discoveries(repo, authorise=lambda *args: None)
    store.save(result)
    restarted_graph = InMemoryGraphStore()
    restarted_graph._source_records = dict(graph._source_records)
    restarted = Discoveries(InMemorySourceRepository(restarted_graph), authorise=lambda *args: None)
    assert restarted.download(result.discovery_id, 'tools', 'scripts/run.sh', tenant_id='tenant', actor_id='alice', blobs=blobs) == b'echo harmless\n'
    with pytest.raises(DiscoveryAccessError):
        restarted.get(result.discovery_id, tenant_id='tenant', actor_id='mallory')
    with pytest.raises(DiscoveryAccessError):
        restarted.get(result.discovery_id, tenant_id='other', actor_id='alice')
    with pytest.raises(DiscoveryAccessError):
        restarted.download(result.discovery_id, 'tools', '../README.md', tenant_id='tenant', actor_id='alice', blobs=blobs)
    moments = iter([result.expires_at - timedelta(seconds=1), result.expires_at])
    expiring = Discoveries(repo, authorise=lambda *args: None, clock=lambda: next(moments))
    with pytest.raises(DiscoveryAccessError):
        expiring.select(result.discovery_id, ('tools',), tenant_id='tenant', actor_id='alice')
    expired = Discoveries(repo, authorise=lambda *args: None, clock=lambda: result.expires_at)
    with pytest.raises(DiscoveryAccessError):
        expired.download(result.discovery_id, 'tools', 'SKILL.md', tenant_id='tenant', actor_id='alice', blobs=blobs)


def test_moved_tag_keeps_previously_resolved_commit(reader_case):
    reader, transport, endpoint, commit, blobs = reader_case
    remote = transport.registered[endpoint]
    transport._git(remote, 'tag', 'v1', commit)
    pin = reader.resolve(RefRequest(tenant_id='tenant', url=endpoint, ref_kind='tag', ref_name='v1'))
    work = transport.root / 'work-0'
    (work / 'tools/SKILL.md').write_bytes(b'# Changed\n')
    transport._git(work, 'add', '.')
    transport._git(work, 'commit', '-qm', 'Second revision')
    latest = transport._git(work, 'rev-parse', 'HEAD').strip()
    transport._git(work, 'push', '-q', str(remote), 'main')
    transport._git(remote, 'tag', '-f', 'v1', latest)
    package = reader.read(PackageRequest(tenant_id='tenant', resolved_ref=pin, package_path='tools'))
    assert package.resolved_ref.commit == commit
    assert blobs.get(package.manifest[0].blob_ref).startswith(b'---')
    updated = reader.resolve(RefRequest(tenant_id='tenant', url=endpoint, ref_kind='tag', ref_name='v1'))
    assert updated.commit == latest


def test_annotated_tag_is_peeled_and_missing_package_is_an_error(reader_case):
    reader, transport, endpoint, commit, _ = reader_case
    remote = transport.registered[endpoint]
    transport._git(remote, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'tag', '-a', 'release', '-m', 'Release', commit)
    pin = reader.resolve(RefRequest(tenant_id='tenant', url=endpoint, ref_kind='tag', ref_name='release'))
    assert pin.commit == commit
    with pytest.raises(AcquisitionError, match='package not found'):
        reader.read(PackageRequest(tenant_id='tenant', resolved_ref=pin, package_path='missing'))


def test_symlink_and_submodule_entries_refuse_whole_selected_package(reader_case):
    reader, transport, endpoint, commit, _ = reader_case
    work = transport.root / 'work-0'
    (work / 'tools/link').symlink_to('/outside')
    transport._git(work, 'add', 'tools/link')
    transport._git(work, 'update-index', '--add', '--cacheinfo', f'160000,{commit},tools/submodule')
    transport._git(work, 'commit', '-qm', 'Unsupported entries')
    transport._git(work, 'push', '-q', str(transport.registered[endpoint]), 'main')
    discovery = reader.discover(DiscoveryRequest(ref=request(endpoint), actor_id='alice'))
    tools = next(package for package in discovery.packages if package.path == 'tools')
    assert not tools.valid
    assert not any(package.package_path == 'tools' for package in discovery.acquired_packages)
    assert next(package for package in discovery.packages if package.path == 'tools/nested').valid


def test_tree_budget_never_returns_partial_discovery(reader_case):
    reader, _, endpoint, _, _ = reader_case
    reader.limits = replace(AcquisitionLimits(), tree_entries=2)
    with pytest.raises(BudgetExceeded, match='tree'):
        reader.discover(DiscoveryRequest(ref=request(endpoint), actor_id='alice'))


def test_cache_lease_rejects_duplicate_acquisition_and_eviction_preserves_custody(reader_case):
    reader, _, endpoint, _, blobs = reader_case
    resolved = reader.resolve(request(endpoint))
    acquired = reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools'))
    with reader._operation('tenant', None, endpoint):
        with pytest.raises(BudgetExceeded, match='already in progress'):
            reader.resolve(request(endpoint))
        with pytest.raises(BudgetExceeded):
            reader.evict_cache('tenant', None, endpoint)
    reader.evict_cache('tenant', None, endpoint)
    assert blobs.exists(acquired.manifest[0].blob_ref)
    assert reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools')).manifest == acquired.manifest


def test_history_fetch_proves_ancestor_and_bounds_unknown_history(reader_case):
    reader, transport, endpoint, baseline, _ = reader_case
    work = transport.root / 'work-0'
    for number in range(3):
        (work / 'tools/version').write_text(str(number))
        transport._git(work, 'add', '.')
        transport._git(work, 'commit', '-qm', f'Revision {number}')
    transport._git(work, 'push', '-q', str(transport.registered[endpoint]), 'main')
    resolved = reader.resolve(request(endpoint))
    package = reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools', baseline_commit=baseline))
    assert package.history_evidence == 'proven_ancestor'
    reader.evict_cache('tenant', None, endpoint)
    reader.limits = replace(AcquisitionLimits(), history_commits=2)
    package = reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools', baseline_commit=baseline))
    assert package.history_evidence == 'unproven'


def test_ref_file_count_and_aggregate_content_budgets(reader_case):
    reader, _, endpoint, _, _ = reader_case
    resolved = reader.resolve(request(endpoint))
    for limits, message in [
        (replace(AcquisitionLimits(), files=1), 'file count'),
        (replace(AcquisitionLimits(), package_bytes=10), 'content'),
    ]:
        reader.limits = limits
        with pytest.raises(BudgetExceeded, match=message):
            reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools'))


def test_ref_advertisement_is_bounded(reader_case):
    reader, transport, endpoint, _, _ = reader_case
    remote = transport.registered[endpoint]
    transport._git(remote, 'tag', 'extra')
    reader.limits = replace(AcquisitionLimits(), refs=1)
    with pytest.raises(BudgetExceeded, match='ref advertisement'):
        reader.resolve(request(endpoint))


def test_profile_grants_rechecked_for_cached_objects(reader_case, monkeypatch):
    from oms.sources.credentials import ProfileAccessError
    from oms.sources.settings import CredentialProfile
    reader, _, endpoint, _, _ = reader_case
    monkeypatch.setenv('SOURCE_TEST_TOKEN', 'synthetic-test-value')
    reader.settings = replace(reader.settings, profiles=(CredentialProfile('private', 'SOURCE_TEST_TOKEN'),))
    reader.profile_grants = lambda tenant: frozenset({'private'})
    resolved = reader.resolve(RefRequest(tenant_id='tenant', url=endpoint, credential_profile_id='private'))
    reader.profile_grants = lambda tenant: frozenset()
    with pytest.raises(ProfileAccessError):
        reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools', credential_profile_id='private'))


def test_redirect_failure_never_retries_a_new_location(reader_case):
    reader, transport, endpoint, _, _ = reader_case
    original = transport.run
    endpoints = []

    def redirect(args, *arguments, **kwargs):
        if 'ls-remote' in args:
            endpoints.extend(arg for arg in args if arg.startswith('https://'))
            return 128, b''
        return original(args, *arguments, **kwargs)
    transport.run = redirect
    with pytest.raises(AcquisitionError):
        reader.resolve(request(endpoint))
    assert endpoints == [endpoint]


def test_discovery_preserves_raw_frontmatter_and_discloses_nested_unsupported_fields(reader_case):
    reader, transport, _, _, blobs = reader_case
    endpoint = 'https://github.com/example/metadata.git'
    transport.register(endpoint, {'SKILL.md': b'---\r\nname: metadata\r\nx-extra:\r\n  nested: retained\r\n---\r\n# Test\r\n'})
    result = reader.discover(DiscoveryRequest(ref=request(endpoint), actor_id='alice'))
    assert result.packages[0].unsupported_metadata == ('x-extra',)
    assert 'nested: retained' in result.acquired_packages[0].raw_frontmatter.value
    assert blobs.get(result.acquired_packages[0].manifest[0].blob_ref).endswith(b'# Test\r\n')


def test_batch_session_reuses_ref_tree_and_package_with_one_budget(reader_case, monkeypatch):
    reader, transport, endpoint, _, _ = reader_case
    calls = []
    original = transport.run

    def traced(args, env, cache, budget, **kwargs):
        calls.append((tuple(args), id(budget)))
        return original(args, env, cache, budget, **kwargs)
    monkeypatch.setattr(transport, 'run', traced)
    with reader.batch_session(tenant_id='tenant', credential_profile_id=None, canonical_url=endpoint) as batch:
        resolved = batch.resolve(request(endpoint))
        assert batch.resolve(request(endpoint)) == resolved
        first = batch.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools'))
        assert batch.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools')) == first
        batch.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools/nested'))
    assert len({identity for _, identity in calls}) == 1
    assert sum('ls-remote' in args for args, _ in calls) == 1
    assert sum('ls-tree' in args for args, _ in calls) == 1


def test_batch_session_rejects_scope_changes_and_accumulates_expanded_budget(reader_case):
    reader, _, endpoint, _, _ = reader_case
    with reader.batch_session(tenant_id='tenant', credential_profile_id=None, canonical_url=endpoint) as batch:
        resolved = batch.resolve(request(endpoint))
        with pytest.raises(AcquisitionError, match='scope'):
            batch.read(PackageRequest(tenant_id='foreign', resolved_ref=resolved, package_path='tools'))
        with pytest.raises(AcquisitionError, match='scope'):
            batch.resolve(RefRequest(tenant_id='tenant', url='https://github.com/other/repository'))
    with pytest.raises(BudgetExceeded):
        with reader.batch_session(tenant_id='tenant', credential_profile_id=None, canonical_url=endpoint) as batch:
            batch.budget.expanded = reader.limits.expanded_bytes
            batch.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools'))


@pytest.mark.parametrize('kind', ['branch', 'tag'])
def test_discovered_canonical_ref_is_accepted_by_real_reader_on_revalidation(reader_case, kind):
    reader, transport, endpoint, commit, _ = reader_case
    remote = transport.registered[endpoint]
    full = 'refs/heads/feature/tools' if kind == 'branch' else 'refs/tags/releases/v1'
    transport._git(remote, 'update-ref', full, commit)
    found = reader.discover(DiscoveryRequest(actor_id='alice', ref=RefRequest(tenant_id='tenant', url=endpoint,
        ref_kind=kind, ref_name='feature/tools' if kind == 'branch' else 'releases/v1', package_path='tools')))
    assert found.resolved_ref.name == full
    repeated = reader.resolve(RefRequest(tenant_id='tenant', url=endpoint,
        ref_kind=kind, ref_name=found.resolved_ref.name, package_path='tools'))
    assert repeated == found.resolved_ref


def _many_packages(tmp_path, count):
    transport = FixtureGitTransport(tmp_path / f'fixtures-{count}')
    endpoint = 'https://github.com/example/many.git'
    files = {f'skills/p{index}/SKILL.md': f'---\nname: p{index}\n---\n# P{index}\n'.encode() for index in range(count)}
    files.update({f'skills/p{index}/notes.md': f'Notes {index}\n'.encode() for index in range(count)})
    files['skills/shared/SKILL.md'] = b'# Shared\n'
    files['skills/lfs/SKILL.md'] = b'# LFS\n'
    files['skills/lfs/asset.bin'] = b'version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 1\n'
    transport.register(endpoint, files, filter_blobs=True)
    reader = GitReader(SourceSettings(tmp_path / f'cache-{count}'), blob_store=FileBlobStore(tmp_path / f'custody-{count}'),
                       transport=transport)
    return reader, transport, endpoint


@pytest.mark.parametrize('failed_probe', [False, True], ids=['native-probe', 'aborted-probe'])
def test_discovery_git_invocations_do_not_grow_with_package_count(tmp_path, monkeypatch, failed_probe):
    counts = {}
    for count in (2, 12):
        reader, transport, endpoint = _many_packages(tmp_path, count)
        original = transport.run
        calls = []

        def traced(args, env, cache, budget, *, original=original, calls=calls, **kwargs):
            calls.append(tuple(args))
            if failed_probe and '--batch-check=%(objectname) %(objecttype) %(objectsize)' in args \
                    and not any('--stdin' in call for call in calls):
                return 128, b'partial inventory\n'
            return original(args, env, cache, budget, **kwargs)

        monkeypatch.setattr(transport, 'run', traced)
        result = reader.discover(DiscoveryRequest(ref=request(endpoint), actor_id='alice'))
        packages = {package.path: package for package in result.packages}
        assert not packages['skills/lfs'].valid and 'LFS' in packages['skills/lfs'].reasons[0]
        assert all(package.valid for path, package in packages.items() if path != 'skills/lfs')
        acquired = {package.package_path: package for package in result.acquired_packages}
        assert len(acquired) == count + 1
        notes = next(entry for entry in acquired['skills/p1'].manifest if entry.path == 'notes.md')
        assert reader.blob_store.get(notes.blob_ref) == b'Notes 1\n'
        assert not any(call[-3:-1] == ('cat-file', 'blob') for call in calls)
        counts[count] = (sum('fetch' in call for call in calls), sum('cat-file' in call for call in calls), len(calls))
    assert counts[2] == counts[12]
    assert counts[12][0] == 2


@pytest.mark.parametrize('unavailable', ['remote', 'one-object'])
def test_discovery_blob_fetch_failure_isolates_only_the_packages_it_concerns(tmp_path, monkeypatch, unavailable):
    reader, transport, endpoint = _many_packages(tmp_path, 4)
    original = transport.run
    fetches, bad = [], None

    def fail(args, env, cache, budget, **kwargs):
        nonlocal bad
        if 'ls-tree' in args:
            code, raw = original(args, env, cache, budget, **kwargs)
            bad = next(line for line in raw.split(b'\0') if line.endswith(b'\tskills/p1/notes.md')).split(b'\t')[0].split()[2]
            return code, raw
        if '--stdin' in args:
            fetches.append(args)
            if unavailable == 'remote' or bad in kwargs['stdin']:
                return 128, b''
        return original(args, env, cache, budget, **kwargs)

    monkeypatch.setattr(transport, 'run', fail)
    result = reader.discover(DiscoveryRequest(ref=request(endpoint), actor_id='alice'))
    # A failed shared fetch cannot name its object, so each package it concerned is fetched on its own.
    assert len(fetches) == 7
    valid = {package.path for package in result.packages if package.valid}
    assert len(result.packages) == 6
    assert valid == (set() if unavailable == 'remote' else {'skills/p0', 'skills/p2', 'skills/p3', 'skills/shared'})
    assert {package.package_path for package in result.acquired_packages} == valid


@pytest.mark.parametrize('failure', ['missing', 'not-a-blob', 'size-changed'])
def test_discovery_blob_failure_invalidates_only_the_package_that_references_it(tmp_path, monkeypatch, failure):
    reader, transport, endpoint = _many_packages(tmp_path, 4)
    original = transport.run
    calls = []
    bad = None

    def faulty(args, env, cache, budget, **kwargs):
        nonlocal bad
        calls.append(tuple(args))
        if 'ls-tree' in args:
            code, raw = original(args, env, cache, budget, **kwargs)
            line = next(line for line in raw.split(b'\0') if line.endswith(b'\tskills/p1/notes.md'))
            bad = line.split(b'\t')[0].split()[2]
            return code, raw
        code, raw = original(args, env, cache, budget, **kwargs)
        if '--batch-check=%(objectname) %(objecttype) %(objectsize)' in args and not code:
            replacement = {'missing': bad + b' missing', 'not-a-blob': bad + b' tree 7'}.get(failure)
            if replacement is not None:
                raw = b'\n'.join(replacement if line.startswith(bad) else line for line in raw.splitlines()) + b'\n'
        if '--batch' in args and failure == 'size-changed' and bad + b' blob ' in raw:
            raw = raw.replace(bad + b' blob ', bad + b' blob 9', 1)
        return code, raw

    monkeypatch.setattr(transport, 'run', faulty)
    result = reader.discover(DiscoveryRequest(ref=request(endpoint), actor_id='alice'))
    packages = {package.path: package for package in result.packages}
    assert not packages['skills/p1'].valid
    assert packages['skills/p1'].reasons[0] in ('required blob unavailable', 'blob size changed')
    assert all(package.valid for path, package in packages.items() if path not in ('skills/p1', 'skills/lfs'))
    assert {package.package_path for package in result.acquired_packages} == {
        'skills/p0', 'skills/p2', 'skills/p3', 'skills/shared'}
    assert sum('--stdin' in call for call in calls) == 1


def test_production_transport_resolves_dns_once_per_operation(tmp_path, monkeypatch):
    from oms.sources.limits import Budget
    commands = []

    def bounded(argv, env, cwd, budget, **kwargs):
        commands.append(argv)
        return (0, b'140.82.114.3\n') if argv[0] != '/usr/bin/git' else (0, b'')

    monkeypatch.setattr('oms.sources.git_reader.run_bounded', bounded)
    transport = GitTransport()
    first, second = Budget(AcquisitionLimits()), Budget(AcquisitionLimits())
    for budget in (first, first, second):
        transport.run(['fetch', 'https://github.com/example/skills.git'], {}, tmp_path, budget)
    transport.run(['cat-file', '--batch'], {}, tmp_path, first)
    git = [argv for argv in commands if argv[0] == '/usr/bin/git']
    assert len(commands) - len(git) == 2
    assert all('http.curloptResolve=github.com:443:140.82.114.3' in argv for argv in git[:3])
    assert 'http.curloptResolve=github.com:443:140.82.114.3' not in git[3]


def test_batch_blob_reads_stay_within_stdout_budget(tmp_path, monkeypatch):
    transport = FixtureGitTransport(tmp_path / 'fixtures')
    endpoint = 'https://github.com/example/large.git'
    files = {'tools/SKILL.md': b'# Tools\n', 'tools/large.txt': b'x' * 680, 'tools/small.txt': b'small\n'}
    transport.register(endpoint, files, filter_blobs=True)
    blobs = FileBlobStore(tmp_path / 'custody')
    reader = GitReader(SourceSettings(tmp_path / 'cache'), blob_store=blobs, transport=transport,
                       limits=replace(AcquisitionLimits(), stdout_bytes=800))
    original = transport.run
    reads = []

    def traced(args, env, cache, budget, **kwargs):
        if '--batch' in args:
            reads.append(kwargs['stdout_cap'])
        return original(args, env, cache, budget, **kwargs)

    monkeypatch.setattr(transport, 'run', traced)
    resolved = reader.resolve(request(endpoint))
    acquired = reader.read(PackageRequest(tenant_id='tenant', resolved_ref=resolved, package_path='tools'))
    assert {entry.path: blobs.get(entry.blob_ref) for entry in acquired.manifest} == {
        name.removeprefix('tools/'): body for name, body in files.items()}
    assert len(reads) == 2 and all(cap <= 800 for cap in reads)
