import pytest

from oms.sources.links import LinkError, parse_link, select_ref, validate_path


def test_repository_and_explicit_package_links():
    assert parse_link('https://github.com/Example/Skills.git').repository_url == 'https://github.com/example/skills.git'
    link = parse_link('https://github.com/example/skills/blob/main/tools/SKILL.md')
    assert link.ref_path == 'main/tools'
    assert link.explicit_package
    selected = select_ref(link, {'refs/heads/main': 'a' * 40}, 'refs/heads/main')
    assert (selected.ref, selected.path, selected.object_id) == ('refs/heads/main', 'tools', 'a' * 40)


@pytest.mark.parametrize('url', [
    'http://github.com/a/b', 'https://user@github.com/a/b',
    'https://github.com:444/a/b', 'https://github.com/a/b?token=x',
    'https://github.com/a/b#x', 'https://evil.example/a/b',
    'file:///tmp/repo', 'https://github.com/a/../b',
    'https://github.com/a/b/tree/main/%2e%2e/secret',
    'https://github.com/a/b/blob/main/README.md',
    'https://github.com/a/b/tree/main%2fsecret', ' https://github.com/a/b',
    'https://github.com/a/b/tree/main/a\\b',
])
def test_rejects_unsafe_links(url):
    with pytest.raises(LinkError):
        parse_link(url)


def test_slash_refs_and_same_name_kinds_require_disambiguation():
    refs = {'refs/heads/feature': 'a' * 40, 'refs/heads/feature/tool': 'b' * 40}
    link = parse_link('https://github.com/a/b/tree/feature/tool/pkg')
    with pytest.raises(LinkError, match='ambiguous'):
        select_ref(link, refs, 'refs/heads/feature')
    selected = select_ref(link, refs, None, ref_kind='branch', ref_value='feature/tool', package_path='pkg')
    assert selected.object_id == 'b' * 40
    refs['refs/tags/feature'] = 'c' * 40
    with pytest.raises(LinkError, match='ambiguous'):
        select_ref(parse_link('https://github.com/a/b'), refs, None, ref_value='feature')


def test_default_branch_and_pinned_commit():
    link = parse_link('https://github.com/a/b')
    assert select_ref(link, {'refs/heads/main': 'a' * 40}, 'refs/heads/main').kind == 'branch'
    assert select_ref(link, {}, None, ref_kind='commit', ref_value='b' * 40).object_id == 'b' * 40
    with pytest.raises(LinkError):
        select_ref(link, {}, None, ref_kind='commit', ref_value='abcdef')


@pytest.mark.parametrize('path', ['../x', '/x', 'x//y', 'x/.git/y', 'x\\y', 'x\x00y', '/'.join(['x'] * 65)])
def test_unsafe_package_paths(path):
    with pytest.raises(LinkError):
        validate_path(path)


def test_commit_url_resolves_without_an_advertised_ref():
    link = parse_link('https://github.com/a/b/blob/' + 'a' * 40 + '/pkg/SKILL.md')
    result = select_ref(link, {}, None)
    assert result.kind == 'commit'
    assert result.path == 'pkg'
    assert result.object_id == 'a' * 40


def test_explicit_ref_cannot_silently_drop_link_package_path():
    link = parse_link('https://github.com/a/b/tree/main/pkg')
    result = select_ref(link, {'refs/heads/main': 'a' * 40}, None, ref_kind='branch', ref_value='main')
    assert result.path == 'pkg'


@pytest.mark.parametrize('kind,prefix', [('branch', 'refs/heads/'), ('tag', 'refs/tags/')])
def test_canonical_refs_round_trip_without_stripping_nested_names(kind, prefix):
    link = parse_link('https://github.com/a/b')
    full = prefix + 'refs/tags/release/v1'
    refs = {full: 'a' * 40, prefix + 'release/v1': 'b' * 40}
    result = select_ref(link, refs, None, ref_kind=kind, ref_value=full, package_path='tools')
    assert (result.kind, result.ref, result.path, result.object_id) == (kind, full, 'tools', 'a' * 40)
    assert select_ref(link, refs, None, ref_value=full).ref == full


def test_canonical_namespace_is_explicit_and_short_name_ambiguity_is_unchanged():
    link = parse_link('https://github.com/a/b')
    refs = {'refs/heads/release/v1': 'a' * 40, 'refs/tags/release/v1': 'b' * 40,
            'refs/tags/release/v1^{}': 'c' * 40}
    assert select_ref(link, refs, None, ref_value='refs/tags/release/v1').object_id == 'c' * 40
    with pytest.raises(LinkError, match='ambiguous'):
        select_ref(link, refs, None, ref_value='release/v1')
    with pytest.raises(LinkError):
        select_ref(link, refs, None, ref_kind='branch', ref_value='refs/tags/release/v1')
    with pytest.raises(LinkError):
        select_ref(link, {'refs/heads/refs/pull/1': 'a' * 40}, None, ref_value='refs/pull/1')
