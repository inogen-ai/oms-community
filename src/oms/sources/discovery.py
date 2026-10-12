"""Retained discovery results: what one actor's credential could see in a repository.

A result is stored for the actor who ran it and expires; reading it back needs the
same actor, so a listing is never installed from under somebody else's access.
"""
from collections.abc import Callable
from datetime import datetime, timezone

from oms.ports.blob_store import BlobStore
from oms.ports.source_store import SourceRepository
from oms.sources.models import AcquiredPackage, DiscoveryResult


class DiscoveryAccessError(PermissionError):
    """Discovery is unavailable to this caller or has expired."""


class Discoveries:
    def __init__(self, repository: SourceRepository, *,
                 authorise: Callable[[str, str, str | None], None],
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.repository = repository
        self.authorise = authorise
        self.clock = clock

    def save(self, discovery: DiscoveryResult) -> None:
        self.authorise(discovery.tenant_id, discovery.actor_id, discovery.credential_profile_id)
        if discovery.expires_at <= self.clock():
            raise DiscoveryAccessError('discovery unavailable or expired')
        self.repository.put_discovery(discovery)

    def get(self, discovery_id: str, *, tenant_id: str, actor_id: str) -> DiscoveryResult:
        now = self.clock()
        discovery = self.repository.get_discovery(discovery_id, tenant_id=tenant_id, actor_id=actor_id, now=now)
        if (discovery is None or discovery.discovery_id != discovery_id or discovery.tenant_id != tenant_id
            or discovery.actor_id != actor_id
                or discovery.expires_at <= now):
            raise DiscoveryAccessError('discovery unavailable or expired')
        self.authorise(tenant_id, actor_id, discovery.credential_profile_id)
        return discovery

    def select(self, discovery_id: str, paths: tuple[str, ...], *, tenant_id: str,
               actor_id: str) -> tuple[AcquiredPackage, ...]:
        discovery = self.get(discovery_id, tenant_id=tenant_id, actor_id=actor_id)
        allowed = {package.path for package in discovery.packages if package.valid}
        retained = {package.package_path: package for package in discovery.acquired_packages}
        if (not paths or len(paths) != len(set(paths))
            or any(path not in allowed or path not in retained for path in paths)):
            raise DiscoveryAccessError('invalid discovery selection')
        # Recheck time after selection: installation admission repeats this check
        # immediately before committing the selected batch.
        if discovery.expires_at <= self.clock():
            raise DiscoveryAccessError('discovery unavailable or expired')
        return tuple(retained[path] for path in paths)

    def download(self, discovery_id: str, package_path: str, file_path: str, *,
                 tenant_id: str, actor_id: str, blobs: BlobStore) -> bytes:
        package, = self.select(discovery_id, (package_path,), tenant_id=tenant_id, actor_id=actor_id)
        entry = next((entry for entry in package.manifest if entry.path == file_path), None)
        if entry is None:
            raise DiscoveryAccessError('file is outside discovery manifest')
        return blobs.get(entry.blob_ref)


def metadata_from_frontmatter(raw: str, cache, budget) -> tuple[dict[str, str], tuple[str, ...]]:
    """Parse untrusted YAML in a deadline/output-bounded child process."""
    import json
    import sys
    from oms.sources.credentials import isolated_environment
    from oms.sources.limits import run_bounded

    script = """import json, sys, yaml
from oms.import_skills.parser import parse_frontmatter
raw = sys.stdin.read()
value = yaml.load(raw, Loader=yaml.BaseLoader)
fields, _ = parse_frontmatter('---\\n' + raw + '\\n---\\n')
keys = sorted(key for key in value if isinstance(key, str)) if isinstance(value, dict) else sorted(fields)
sys.stdout.write(json.dumps([fields, keys]))
"""
    code, output = run_bounded([sys.executable, '-I', '-c', script], isolated_environment(cache),
                               cache, budget, stdin=raw.encode(), stdout_cap=64 * 1024)
    if code:
        raise ValueError('invalid package frontmatter')
    fields, keys = json.loads(output)
    return fields, tuple(keys)
