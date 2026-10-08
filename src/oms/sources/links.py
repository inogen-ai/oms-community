"""Parse a GitHub link strictly, and pick one ref from what the repository advertises. A link that could mean
two things is refused rather than guessed."""
from dataclasses import dataclass
import re
from urllib.parse import unquote, urlsplit


class LinkError(ValueError):
    """A link or ref cannot be safely and unambiguously interpreted."""


def validate_path(value: str, *, allow_empty: bool = True) -> str:
    if not value and allow_empty:
        return value
    parts = value.split('/')
    if (not value or len(value.encode('utf-8')) > 4096 or len(parts) > 64
            or any(not p or p in ('.', '..') or p.casefold() == '.git' for p in parts)
            or any(ord(c) < 32 or ord(c) == 127 or c == '\\' for c in value)):
        raise LinkError('unsafe package path')
    return value


def validate_ref(value: str) -> str:
    validate_path(value, allow_empty=False)
    if (value.startswith('-') or value.endswith('.') or '..' in value or '@{' in value
            or any(c in value for c in ' ~^:?*[\\')
            or any(p.startswith('.') or p.endswith('.lock') for p in value.split('/'))):
        raise LinkError('invalid ref')
    return value


@dataclass(frozen=True)
class GitHubLink:
    repository_url: str
    ref_path: str = ''
    explicit_package: bool = False


@dataclass(frozen=True)
class SelectedRef:
    kind: str
    ref: str
    object_id: str
    path: str


def parse_link(raw: str) -> GitHubLink:
    if raw != raw.strip() or any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise LinkError('invalid GitHub URL')
    try:
        url = urlsplit(raw)
        valid = (url.scheme == 'https' and url.netloc in ('github.com', 'github.com:443')
                 and url.port in (None, 443) and not url.username and not url.password
                 and not url.query and not url.fragment)
    except ValueError:
        raise LinkError('invalid GitHub URL') from None
    if not valid or re.search(r'%(?:2f|5c)', url.path, re.IGNORECASE):
        raise LinkError('only HTTPS GitHub repository URLs are permitted')
    path = unquote(url.path, errors='strict').removeprefix('/').removesuffix('/')
    validate_path(path, allow_empty=False)
    parts = path.split('/')
    if len(parts) < 2 or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in parts[:2]):
        raise LinkError('invalid repository path')
    owner, repo = parts[:2]
    repo = repo.removesuffix('.git')
    if not repo or repo in ('.', '..'):
        raise LinkError('invalid repository path')
    endpoint = f'https://github.com/{owner.lower()}/{repo.lower()}.git'
    if len(parts) == 2:
        return GitHubLink(endpoint)
    if len(parts) < 4 or parts[2] not in ('tree', 'blob'):
        raise LinkError('expected repository, tree or SKILL.md blob URL')
    if parts[2] == 'blob':
        if parts[-1] != 'SKILL.md' or len(parts) < 5:
            raise LinkError('blob URL must end in SKILL.md')
        parts = parts[:-1]
    return GitHubLink(endpoint, '/'.join(parts[3:]), True)


def select_ref(link: GitHubLink, refs: dict[str, str], symbolic_head: str | None,
               *, ref_kind: str | None = None, ref_value: str | None = None,
               package_path: str | None = None) -> SelectedRef:
    if package_path is not None:
        validate_path(package_path)
    if ref_kind not in (None, 'branch', 'tag', 'commit'):
        raise LinkError('invalid ref kind')
    url_ref, _, url_path = link.ref_path.partition('/')
    if ref_kind is None and ref_value is None and re.fullmatch(r'[0-9a-f]{40}', url_ref):
        ref_kind, ref_value = 'commit', url_ref
        package_path = url_path if package_path is None else package_path
    if ref_kind == 'commit':
        if not ref_value or not re.fullmatch(r'[0-9a-f]{40}', ref_value):
            raise LinkError('commit must be a full object ID')
        if package_path is None and link.ref_path:
            if url_ref != ref_value:
                raise LinkError('explicit package path required for a different ref')
            package_path = url_path
        return SelectedRef('commit', ref_value, ref_value, package_path or '')
    if ref_value:
        validate_ref(ref_value)
    elif ref_kind:
        raise LinkError('ref value is required')
    canonical = bool(ref_value and ref_value.startswith('refs/'))
    if canonical:
        namespace = ('branch' if ref_value.startswith('refs/heads/') else 'tag'
                     if ref_value.startswith('refs/tags/') else None)
        if namespace is None or ref_kind is not None and namespace != ref_kind:
            raise LinkError('ref namespace does not match kind')
        ref_kind = namespace
    candidates = []
    for full_ref, oid in refs.items():
        kind = 'branch' if full_ref.startswith('refs/heads/') else 'tag' if full_ref.startswith('refs/tags/') else None
        if kind is None or full_ref.endswith('^{}') or ref_kind and kind != ref_kind:
            continue
        name = full_ref[len('refs/heads/' if kind == 'branch' else 'refs/tags/'):]
        path = package_path or ''
        if ref_value:
            matches = full_ref == ref_value if canonical else name == ref_value
            if matches and package_path is None and link.ref_path:
                if link.ref_path != name and not link.ref_path.startswith(name + '/'):
                    raise LinkError('explicit package path required for a different ref')
                path = link.ref_path[len(name):].lstrip('/')
        elif link.ref_path:
            matches = link.ref_path == name or link.ref_path.startswith(name + '/')
            path = package_path if package_path is not None else link.ref_path[len(name):].lstrip('/')
        else:
            matches = full_ref == symbolic_head
        if matches:
            validate_ref(full_ref)
            validate_path(path)
            commit = refs.get(full_ref + '^{}', oid) if kind == 'tag' else oid
            if not re.fullmatch(r'[0-9a-f]{40}', commit):
                raise LinkError('invalid advertised object ID')
            candidates.append(SelectedRef(kind, full_ref, commit, path))
    if len(candidates) != 1:
        raise LinkError('ambiguous ref; specify kind, value and package path' if candidates else 'ref not found')
    return candidates[0]
