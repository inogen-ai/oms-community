"""Fetch a skill package from GitHub with Git, read-only and under a budget.

Nothing is checked out, no hook, helper or submodule runs, and no model is involved;
what comes back is the package's bytes, its frontmatter and whether the retained
baseline is still an ancestor. A batch session lets one check of many skills share
one fetch of their repository.
"""
import bisect
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import logging
from pathlib import Path
import re
import socket
import shutil
import sys
import threading
from typing import Protocol
import unicodedata
from uuid import uuid4
import weakref

from oms.adapters.blobs.file_blob_store import FileBlobStore
from oms.sources.discovery import metadata_from_frontmatter
from oms.ports.blob_store import BlobStore
from oms.sources.credentials import credential_environment, isolated_environment
from oms.sources.limits import AcquisitionLease, AcquisitionLimits, Budget, BudgetExceeded, run_bounded
from oms.sources.links import parse_link, select_ref, validate_path
from oms.sources.primitives import exact_digest
from oms.sources.models import (
    AcquiredPackage, DiscoveryRequest, DiscoveryResult, DiscoveredPackage, Evidence,
    ManifestEntry, PackageRequest, RefRequest, ResolvedRef,
)
from oms.sources.settings import SourceSettings

logger = logging.getLogger(__name__)


class AcquisitionError(RuntimeError):
    """Safe acquisition failure code; never contains Git diagnostics or credentials."""


class _BlobFailure(AcquisitionError):
    """A blob failure that names the objects it is sure about and the ones it merely suspects.

    Several packages share one inventory and one fetch, so a single bad object
    must be charged to the packages that reference it, not to every package in
    the batch; `reasons` is what Git said per object, `suspects` is everything
    the failed command touched when it did not say.
    """

    def __init__(self, message, reasons=None, suspects=()):
        super().__init__(message)
        self.reasons = dict(reasons or {})
        self.suspects = frozenset(suspects) | self.reasons.keys()


class Transport(Protocol):
    def run(self, args: list[str], env: dict[str, str], cache: Path, budget: Budget,
            *, stdin: bytes | None = None, stdout_cap: int | None = None) -> tuple[int, bytes]: ...


class GitTransport:
    """Pin validated public addresses into libcurl while preserving GitHub TLS identity.

    The hostname is resolved once per operation, every address is checked to be
    a global unicast one, and the lowest is handed to curl as the only address
    for github.com:443. TLS still verifies the github.com certificate, so a
    resolver that answered with a private address cannot steer the fetch at a
    machine on the inside of the network.
    """
    @staticmethod
    def network_options(addresses: list[str] | None = None) -> list[str]:
        if addresses is None:
            addresses = [item[4][0] for item in socket.getaddrinfo('github.com', 443, type=socket.SOCK_STREAM)]
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise AcquisitionError('unsafe network destination')
        # One pinned address prevents DNS rebinding. Connection errors remain errors.
        address = sorted(set(addresses))[0]
        if ':' in address:
            address = '[' + address + ']'
        return ['-c', 'http.followRedirects=false', '-c', 'http.sslVerify=true',
                '-c', 'http.proxy=', '-c', f'http.curloptResolve=github.com:443:{address}']

    def __init__(self):
        # One validated pin per operation budget: every command of an operation uses the same address.
        self._pinned = weakref.WeakKeyDictionary()
        self._lock = threading.Lock()

    def run(self, args, env, cache, budget, *, stdin=None, stdout_cap=None):
        options = []
        if any(arg.startswith('https://') for arg in args):
            with self._lock:
                options = self._pinned.get(budget)
            if options is None:
                # DNS is also a bounded child operation, not an unbounded resolver call.
                code, raw = run_bounded([sys.executable, '-I', '-S', '-c',
                    "import socket,sys; sys.stdout.write('\\n'.join(sorted({x[4][0] for x in "
                    "socket.getaddrinfo('github.com',443,type=socket.SOCK_STREAM)})))"],
                    isolated_environment(cache), cache, budget, stdout_cap=4096)
                if code:
                    raise AcquisitionError('network destination unavailable')
                options = self.network_options(raw.decode('ascii').splitlines())
                with self._lock:
                    self._pinned[budget] = options
        return run_bounded(['/usr/bin/git', *options, *args], env, cache, budget,
                           stdin=stdin, stdout_cap=stdout_cap)


class GitReader:
    def __init__(self, settings: SourceSettings, *, blob_store: BlobStore | None = None,
                 transport: Transport | None = None, limits: AcquisitionLimits | None = None,
                 profile_grants: Callable[[str], frozenset[str]] = lambda tenant: frozenset(),
                 cancelled: Callable[[], bool] = lambda: False):
        self.settings = settings
        self.blob_store = blob_store
        self.transport = transport or GitTransport()
        self.limits = limits or AcquisitionLimits()
        self.profile_grants = profile_grants
        self.cancelled = cancelled

    @contextmanager
    def batch_session(self, *, tenant_id: str, credential_profile_id: str | None, canonical_url: str):
        with self._operation(tenant_id, credential_profile_id, canonical_url) as (cache, env, budget):
            batch = GitBatch(self, tenant_id, credential_profile_id, canonical_url, cache, env, budget)
            try:
                yield batch
            finally:
                batch.closed = True
                if budget.expanded > budget.limits.expanded_bytes:
                    raise BudgetExceeded('expanded object budget exceeded')
                budget.check()

    @contextmanager
    def _operation(self, tenant: str, profile: str | None, endpoint: str):
        endpoint = parse_link(endpoint).repository_url
        # Admission is repeated even when all requested objects are already cached.
        auth = credential_environment(self.settings, profile, self.profile_grants(tenant), repository_url=endpoint)
        # The object cache is keyed by tenant, profile and repository, and one
        # lease covers it at a time. Objects fetched with one tenant's token are
        # never served to another, and two acquisitions cannot write the same
        # bare repository at once.
        tenant_key = hashlib.sha256(tenant.encode()).hexdigest()
        key = hashlib.sha256(('\0'.join((tenant, profile or '', endpoint))).encode()).hexdigest()
        root = self.settings.cache_root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with AcquisitionLease(root / 'leases', tenant_key, key, self.limits):
            cache = root / key
            if cache.is_symlink():
                raise AcquisitionError('unsafe cache path')
            cache.mkdir(exist_ok=True, mode=0o700)
            env = isolated_environment(cache)
            env.update(auth)
            budget = Budget(self.limits, cancelled=self.cancelled)
            try:
                budget.watch_cache(cache)
                if not (cache / 'HEAD').exists():
                    self._git(['init', '--bare', '--quiet', '--template='], env, cache, budget)
                yield cache, env, budget
            except BudgetExceeded:
                # This leased cache contains no retained source blobs or reviews.
                shutil.rmtree(cache)
                raise
            finally:
                logger.info('Source acquisition finished',
                            extra={'expanded_bytes': budget.expanded, 'acquired_bytes': budget.acquired})

    def evict_cache(self, tenant: str, profile: str | None, endpoint: str) -> None:
        """Evict disposable Git data only while holding the cache's exclusive lease."""
        endpoint = parse_link(endpoint).repository_url
        credential_environment(self.settings, profile, self.profile_grants(tenant), repository_url=endpoint)
        tenant_key = hashlib.sha256(tenant.encode()).hexdigest()
        key = hashlib.sha256(('\0'.join((tenant, profile or '', endpoint))).encode()).hexdigest()
        with AcquisitionLease(self.settings.cache_root / 'leases', tenant_key, key, self.limits):
            cache = self.settings.cache_root / key
            if cache.is_symlink():
                raise AcquisitionError('unsafe cache path')
            if cache.exists():
                shutil.rmtree(cache)

    def _git(self, args, env, cache, budget, *, stdin=None, stdout_cap=None, allow_failure=False):
        # The environment already closes config, prompts, SSH and protocols;
        # these pins close what a repository or a redirect could still open:
        # no credential helper that might read a stored token, no hook path a
        # fetched config could point at, HTTPS only even when told to follow
        # elsewhere, every object checked as it arrives, no submodule reaching
        # another host, and no maintenance so the only writes are ours.
        fixed = ['-c', 'credential.helper=', '-c', 'core.hooksPath=/dev/null',
                 '-c', 'protocol.allow=never', '-c', 'protocol.https.allow=always',
                 '-c', 'gc.auto=0', '-c', 'maintenance.auto=false',
                 '-c', 'fetch.fsckObjects=true', '-c', 'transfer.fsckObjects=true', '-c', 'http.followRedirects=false',
                 '-c', 'fetch.recurseSubmodules=false', '-c', 'submodule.recurse=false']
        code, result = self.transport.run([*fixed, *args], env, cache, budget, stdin=stdin, stdout_cap=stdout_cap)
        if code and not allow_failure:
            raise AcquisitionError('git acquisition failed; repository, ref or pinned object unavailable')
        return (code, result) if allow_failure else result

    def _resolve(self, request, link, cache, env, budget, *, advertisement=None):
        raw = (advertisement if advertisement is not None
               else self._git(['ls-remote', '--symref', link.repository_url], env, cache, budget))
        refs = {}
        head = None
        lines = raw.decode('utf-8', errors='strict').splitlines()
        if len(lines) > self.limits.refs + 2:
            raise BudgetExceeded('ref advertisement budget exceeded')
        for line in lines:
            oid, sep, name = line.partition('\t')
            if not sep:
                raise AcquisitionError('invalid ref advertisement')
            if oid.startswith('ref: ') and name == 'HEAD':
                head = oid[5:]
            elif name != 'HEAD':
                if name in refs:
                    raise AcquisitionError('duplicate ref advertisement')
                refs[name] = oid
        if len(refs) > self.limits.refs:
            raise BudgetExceeded('ref advertisement budget exceeded')
        selected = select_ref(link, refs, head, ref_kind=request.ref_kind,
                              ref_value=request.ref_name, package_path=request.package_path)
        resolved = ResolvedRef(canonical_url=link.repository_url, kind=selected.kind,
                               name=selected.ref, commit=selected.object_id, package_path=selected.path)
        return resolved, selected.path

    def resolve(self, request: RefRequest) -> ResolvedRef:
        link = parse_link(request.url)
        with self._operation(request.tenant_id, request.credential_profile_id, link.repository_url) as (cache,
                env, budget):
            resolved, _ = self._resolve(request, link, cache, env, budget)
            # An advertisement alone does not establish a tag's object type.
            self._ensure_commit(resolved, cache, env, budget)
            return resolved

    def _ensure_commit(self, resolved, cache, env, budget):
        code, _ = self._git(['cat-file', '-e', resolved.commit + '^{commit}'], env, cache, budget, allow_failure=True)
        if code:
            self._git(['fetch', '--quiet', '--no-tags', '--no-write-fetch-head', '--depth=1',
                       '--filter=blob:none', resolved.canonical_url, resolved.commit], env, cache, budget)
        kind = self._git(['cat-file', '-t', resolved.commit], env, cache, budget).strip()
        if kind != b'commit':
            raise AcquisitionError('selected object is not a commit')

    def _tree(self, resolved, cache, env, budget):
        self._ensure_commit(resolved, cache, env, budget)
        raw = self._git(['ls-tree', '-rz', '--full-tree', resolved.commit], env, cache, budget)
        budget.expand(len(raw))
        entries = raw.split(b'\0')
        if entries[-1] != b'':
            raise AcquisitionError('incomplete tree inventory')
        entries.pop()
        if len(entries) > self.limits.tree_entries:
            raise BudgetExceeded('tree entry budget exceeded')
        tree = {}
        folded = set()
        for index, entry in enumerate(entries):
            if index % 1024 == 0:
                budget.check()
            info, sep, path = entry.partition(b'\t')
            if not sep:
                raise AcquisitionError('invalid tree inventory')
            try:
                name = path.decode('utf-8')
                mode, kind, oid = info.decode('ascii').split(' ')
            except (ValueError, UnicodeError):
                raise AcquisitionError('unsupported tree entry') from None
            validate_path(name, allow_empty=False)
            if len(path) > self.limits.path_bytes or len(name.split('/')) > self.limits.nesting:
                raise BudgetExceeded('tree path budget exceeded')
            normalized = unicodedata.normalize('NFC', name).casefold()
            if normalized in folded:
                raise AcquisitionError('colliding package paths')
            folded.add(normalized)
            if not re.fullmatch(r'[0-9a-f]{40}', oid):
                raise AcquisitionError('invalid tree object')
            tree[name] = (mode, kind, oid)
        return tree

    def _package(self, tenant, resolved, path, tree, cache, env, budget, baseline=None):
        result = self._packages(tenant, resolved, [path], tree, cache, env, budget, baseline)[path]
        if isinstance(result, AcquisitionError):
            raise result
        return result

    def _packages(self, tenant, resolved, paths, tree, cache, env, budget, baseline=None):
        """Acquire packages through one shared blob inventory, fetch and read.

        Package refusals are returned per path; budget exhaustion still ends the operation.
        """
        names = sorted(tree)
        results, selections = {}, {}
        for path in paths:
            budget.check()
            try:
                selections[path] = self._select(path, tree, names, budget)
            except AcquisitionError as exc:
                results[path] = exc
        bodies, pending, verified = {}, dict(selections), {}
        while pending:
            try:
                bodies.update(self._blobs(resolved, list(pending.values()), cache, env, budget))
                verified.update(pending)
                break
            except AcquisitionError as exc:
                # A failure reaches only the packages that reference its objects; the rest retry
                # together, and suspects without an exact cause are each acquired on their own.
                reasons, suspects = getattr(exc, 'reasons', {}), getattr(exc, 'suspects', frozenset())
                alone = len(pending) == 1
                affected = [path for path, selected in pending.items()
                            if any(entry[2] in suspects for entry in selected.values())] or list(pending)
                for path in affected:
                    selected = pending.pop(path)
                    failed = sorted(reasons.keys() & {entry[2] for entry in selected.values()})
                    if alone or failed:
                        results[path] = AcquisitionError(reasons[failed[0]]) if failed else exc
                        continue
                    try:
                        bodies.update(self._blobs(resolved, [selected], cache, env, budget))
                        verified[path] = selected
                    except AcquisitionError as isolated:
                        results[path] = AcquisitionError(str(isolated))
        # Retained bytes live in custody, per tenant, apart from the disposable
        # Git cache: evicting or losing the cache must not lose a baseline.
        blob_store = (self.blob_store
                      or FileBlobStore((self.settings.custody_root
                                        or self.settings.cache_root.parent / 'source-custody')
                                       / hashlib.sha256(tenant.encode()).hexdigest()))
        for path, selected in verified.items():
            budget.check()
            try:
                results[path] = self._assemble(resolved, path, selected, bodies, blob_store, cache, env,
                                               budget, baseline)
            except AcquisitionError as exc:
                results[path] = exc
        return {path: results[path] for path in paths}

    def _select(self, path, tree, names, budget):
        validate_path(path)
        prefix = path + '/' if path else ''
        if prefix + 'SKILL.md' not in tree:
            raise AcquisitionError('skill package not found')
        # Sorted names place every entry under the prefix in one contiguous range.
        start = bisect.bisect_left(names, prefix)
        stop = start
        while stop < len(names) and names[stop].startswith(prefix):
            stop += 1
        within = names[start:stop]
        nested = {name[len(prefix):-len('/SKILL.md')] for name in within
                  if name.endswith('/SKILL.md') and name != prefix + 'SKILL.md'}
        selected = {}
        for index, name in enumerate(within):
            if index % 1024 == 0:
                budget.check()
            relative = name[len(prefix):]
            components = relative.split('/')
            if any('/'.join(components[:i]) in nested for i in range(1, len(components))):
                continue
            selected[relative] = tree[name]
        if len(selected) > self.limits.files:
            raise BudgetExceeded('package file count exceeded')
        for name, (mode, kind, _) in selected.items():
            if kind != 'blob' or mode not in ('100644', '100755'):
                raise AcquisitionError('unsupported package entry: ' + name)
        return selected

    def _blobs(self, resolved, selections, cache, env, budget):
        oids = sorted({entry[2] for selected in selections for entry in selected.values()})
        if not oids:
            return {}
        inventory = ['cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)']
        code, checks = self._git(inventory, env, cache, budget,
                                stdin=('\n'.join(oids) + '\n').encode('ascii'), allow_failure=True)
        # Lazy fetching is off (GIT_NO_LAZY_FETCH) so that reading an object can
        # never turn into a network call the budget does not see. The price is
        # that the inventory may report objects missing, and Git 2.39.5 aborts
        # the whole inventory on a missing promisor blob rather than reporting
        # it; either way the partial answer is discarded and the selected ids
        # are fetched once, explicitly and bounded, before asking again.
        missing = oids if code else [line.split()[0].decode('ascii') for line in checks.splitlines()
                                     if line.endswith(b' missing')]
        if missing:
            # One fetch for every selected package's objects, never an implicit fetch per blob or package.
            try:
                self._git(['fetch', '--quiet', '--no-tags', '--no-write-fetch-head', '--stdin', resolved.canonical_url],
                          env, cache, budget, stdin=('\n'.join(missing) + '\n').encode('ascii'))
            except AcquisitionError as exc:
                raise _BlobFailure(str(exc), suspects=missing) from None
            checks = self._git(inventory, env, cache, budget, stdin=('\n'.join(oids) + '\n').encode('ascii'))
        sizes, reasons = {}, {}
        # Attribution: a line that is not a blob inventory may still name an
        # object we asked for, exactly once; that object is charged, and the
        # packages referencing it fail while the others go on. Anything else
        # in the output means the inventory itself cannot be trusted.
        for line in checks.splitlines():
            fields = line.split()
            if len(fields) != 3 or fields[1] != b'blob' or not fields[2].isdigit():
                named = fields[0].decode('ascii', errors='replace') if fields else None
                if named not in oids or named in sizes or named in reasons:
                    raise AcquisitionError('required blob unavailable')
                reasons[named] = 'required blob unavailable'
                continue
            oid, _, size = fields
            size = int(size)
            if size > self.limits.file_bytes:
                raise BudgetExceeded('package file byte budget exceeded')
            sizes[oid.decode('ascii')] = size
        if not set(sizes) <= set(oids):
            raise AcquisitionError('incomplete blob inventory')
        reasons.update((oid, 'incomplete blob inventory') for oid in oids if oid not in sizes and oid not in reasons)
        if reasons:
            raise _BlobFailure(reasons[min(reasons)], reasons)
        for selected in selections:
            if sum(sizes[entry[2]] for entry in selected.values()) > self.limits.package_bytes:
                raise BudgetExceeded('package content budget exceeded')
        bodies, chunk, chunk_bytes = {}, [], 0
        for oid in oids:
            # Batch output is the exact header, body and newline for each object, within the stdout budget.
            need = len(f'{oid} blob {sizes[oid]}\n') + sizes[oid] + 1
            if chunk and chunk_bytes + need > self.limits.stdout_bytes:
                bodies.update(self._read_blobs(chunk, sizes, chunk_bytes, cache, env, budget))
                chunk, chunk_bytes = [], 0
            chunk.append(oid)
            chunk_bytes += need
        bodies.update(self._read_blobs(chunk, sizes, chunk_bytes, cache, env, budget))
        return bodies

    def _read_blobs(self, oids, sizes, expected, cache, env, budget):
        budget.expand(sum(sizes[oid] for oid in oids))
        raw = self._git(['cat-file', '--batch'], env, cache, budget,
                        stdin=('\n'.join(oids) + '\n').encode('ascii'), stdout_cap=expected)
        bodies, offset = {}, 0
        for oid in oids:
            header = f'{oid} blob {sizes[oid]}\n'.encode('ascii')
            if not raw.startswith(header, offset):
                reason = ('blob size changed' if raw.startswith(f'{oid} blob '.encode('ascii'), offset)
                          else 'required blob unavailable')
                raise _BlobFailure(reason, {oid: reason})
            offset += len(header)
            bodies[oid] = raw[offset:offset + sizes[oid]]
            offset += sizes[oid]
            if raw[offset:offset + 1] != b'\n':
                raise _BlobFailure('blob size changed', {oid: 'blob size changed'})
            offset += 1
        if offset != len(raw):
            raise _BlobFailure('blob size changed', suspects=oids)
        return bodies

    def _assemble(self, resolved, path, selected, bodies, blob_store, cache, env, budget, baseline):
        for oid in sorted({entry[2] for entry in selected.values()}):
            if bodies[oid].startswith(b'version https://git-lfs.github.com/spec/v1'):
                raise AcquisitionError('Git LFS pointer is unsupported')
        manifest = tuple(ManifestEntry(path=name, blob_ref=blob_store.put(bodies[oid]),
                         digest=hashlib.sha256(bodies[oid]).hexdigest(), size=len(bodies[oid]), mode=int(mode, 8))
                         for name, (mode, _, oid) in selected.items())
        skill_body = bodies[selected['SKILL.md'][2]]
        try:
            text = skill_body.decode('utf-8')
        except UnicodeError:
            raise AcquisitionError('SKILL.md must be UTF-8') from None
        frontmatter = re.match(r'\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)', text, re.DOTALL)
        raw_frontmatter = frontmatter.group(1) if frontmatter else None
        evidence = (Evidence(kind='known', value=raw_frontmatter, policy_version='source-acquisition-v1',
                             source_digest=hashlib.sha256(skill_body).hexdigest())
                    if raw_frontmatter is not None else Evidence(kind='absent', policy_version='source-acquisition-v1'))
        history = self._history(baseline, resolved, cache, env, budget)
        return AcquiredPackage(resolved_ref=resolved, package_path=path, manifest=manifest,
                               raw_frontmatter=evidence, history_evidence=history)

    def _history(self, baseline, resolved, cache, env, budget):
        if baseline is None:
            return 'initial'
        if baseline == resolved.commit:
            return 'proven_ancestor'
        # A missing parent or a shallow boundary is uncertainty, never non-ancestry.
        # The walk deepens the history once, by a bounded depth, and gives up
        # as 'unproven' if a parent is still missing after that; 'rewritten' is
        # only said when the whole reachable history was walked without finding
        # the baseline. The plan treats both as reasons for a reviewer.
        seen = set()
        pending = [resolved.commit]
        completed = False
        while pending and len(seen) < self.limits.history_commits:
            oid = pending.pop()
            if oid == baseline:
                return 'proven_ancestor'
            if oid in seen:
                continue
            seen.add(oid)
            code, body = self._git(['cat-file', 'commit', oid], env, cache, budget, allow_failure=True)
            if code:
                if completed:
                    return 'unproven'
                completed = True
                code, _ = self._git(['fetch', '--quiet', '--no-tags', '--no-write-fetch-head',
                    f'--depth={self.limits.history_commits}', '--filter=blob:none',
                    resolved.canonical_url, resolved.commit], env, cache, budget, allow_failure=True)
                if code:
                    return 'unproven'
                seen.remove(oid)
                pending.append(oid)
                continue
            budget.expand(len(body))
            for line in body.split(b'\n\n', 1)[0].splitlines():
                if line.startswith(b'parent '):
                    parent = line[7:].decode('ascii')
                    if not re.fullmatch(r'[0-9a-f]{40}', parent):
                        raise AcquisitionError('invalid history object')
                    pending.append(parent)
        return 'unproven' if pending else 'rewritten'

    def read(self, request: PackageRequest) -> AcquiredPackage:
        endpoint = parse_link(request.resolved_ref.canonical_url).repository_url
        if endpoint != request.resolved_ref.canonical_url:
            raise AcquisitionError('noncanonical resolved repository')
        with self._operation(request.tenant_id, request.credential_profile_id, endpoint) as (cache, env, budget):
            tree = self._tree(request.resolved_ref, cache, env, budget)
            return self._package(request.tenant_id, request.resolved_ref, request.package_path, tree,
                                 cache, env, budget, request.baseline_commit)

    def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        link = parse_link(request.ref.url)
        validate_path(request.discovery_root)
        with self._operation(request.ref.tenant_id, request.ref.credential_profile_id,
                             link.repository_url) as (cache, env, budget):
            resolved, selected_path = self._resolve(request.ref, link, cache, env, budget)
            tree = self._tree(resolved, cache, env, budget)
            root = selected_path or request.discovery_root
            prefix = root + '/' if root else ''
            paths = sorted(name[:-len('/SKILL.md')] if name != 'SKILL.md' else ''
                           for name in tree if (name == 'SKILL.md' or name.endswith('/SKILL.md'))
                           and name.startswith(prefix))
            packages = []
            retained = []
            # Every candidate shares one blob inventory, fetch and read; validity stays per package.
            acquisitions = self._packages(request.ref.tenant_id, resolved, paths, tree, cache, env, budget)
            for path in paths:
                budget.check()
                try:
                    acquired = acquisitions[path]
                    if isinstance(acquired, AcquisitionError):
                        raise acquired
                    text = acquired.raw_frontmatter.value
                    try:
                        metadata, keys = (metadata_from_frontmatter(text, cache, budget)
                                          if isinstance(text, str) else ({}, ()))
                    except ValueError:
                        raise AcquisitionError('invalid package frontmatter') from None
                    retained.append(acquired)
                    packages.append(DiscoveredPackage(path=path, valid=True, upstream_name=metadata.get('name'),
                        description=metadata.get('description'), file_count=len(acquired.manifest),
                        total_bytes=sum(entry.size for entry in acquired.manifest),
                        unsupported_metadata=tuple(sorted(set(keys) - {'name', 'description', 'license', 'tags'}))))
                except AcquisitionError as exc:
                    packages.append(DiscoveredPackage(path=path, valid=False, reasons=(str(exc),)))
            return DiscoveryResult(discovery_id=str(uuid4()), tenant_id=request.ref.tenant_id,
                actor_id=request.actor_id, resolved_ref=resolved,
                credential_profile_id=request.ref.credential_profile_id,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30), packages=tuple(packages),
                acquired_packages=tuple(retained),
                preselected_path=selected_path
                if (link.explicit_package or request.ref.package_path is not None)
                and any(p.path == selected_path and p.valid for p in packages) else None)


class GitBatch:
    def __init__(self, reader, tenant_id, profile, endpoint, cache, env, budget):
        self.reader, self.tenant_id, self.profile = reader, tenant_id, profile
        self.endpoint = parse_link(endpoint).repository_url
        self.cache, self.env, self.budget = cache, env, budget
        self.refs, self.trees, self.packages = {}, {}, {}
        self.advertisement = None
        self.closed = False

    def _scope(self, tenant_id, profile, endpoint):
        # A session holds one tenant's credential and one repository's cache;
        # a request for anything else is refused rather than answered from
        # objects fetched under another tenant's token.
        if (self.closed or tenant_id != self.tenant_id or profile != self.profile
                or parse_link(endpoint).repository_url.casefold() != self.endpoint.casefold()):
            raise AcquisitionError('batch acquisition scope changed')
        self.budget.check()

    def resolve(self, request):
        self._scope(request.tenant_id, request.credential_profile_id, request.url)
        key = exact_digest(request.model_dump(mode='json'))
        if key not in self.refs:
            if self.advertisement is None:
                self.advertisement = self.reader._git(['ls-remote', '--symref', self.endpoint], self.env,
                                                      self.cache, self.budget)
            resolved, _ = self.reader._resolve(request, parse_link(request.url), self.cache, self.env, self.budget,
                                              advertisement=self.advertisement)
            self.reader._ensure_commit(resolved, self.cache, self.env, self.budget)
            self.refs[key] = resolved
        return self.refs[key]

    def read(self, request):
        self._scope(request.tenant_id, request.credential_profile_id, request.resolved_ref.canonical_url)
        key = exact_digest(request.model_dump(mode='json'))
        if key not in self.packages:
            commit = request.resolved_ref.commit
            if commit not in self.trees:
                self.trees[commit] = self.reader._tree(request.resolved_ref, self.cache, self.env, self.budget)
            self.packages[key] = self.reader._package(request.tenant_id, request.resolved_ref, request.package_path,
                self.trees[commit], self.cache, self.env, self.budget, request.baseline_commit)
        return self.packages[key]
