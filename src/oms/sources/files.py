"""Supporting files: whole-file comparison, verified reads from custody and path checks.

Files are compared as whole bytes plus mode, never edited, and a shared blob says
nothing about who owns an occurrence of it at a path.
"""
from hashlib import sha256
from typing import Literal

from oms.ports.blob_store import BlobNotFound, BlobStore
from oms.sources.errors import SourceConflict, SourceNotFound
from oms.sources.compare import UnitDecision, compare_unit, same_value
from oms.sources.models import Evidence, ManifestEntry

FileSide = Literal["base", "local", "upstream", "discovery"]


def require_path(path: str) -> None:
    if (not path or path.startswith("/") or "\\" in path or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise SourceNotFound("file_not_found")


def verified_bytes(blobs: BlobStore, entry: ManifestEntry) -> bytes:
    try:
        body = blobs.get(entry.blob_ref)
    except BlobNotFound as exc:
        raise SourceConflict("record_unavailable", "file custody unavailable") from exc
    if len(body) != entry.size or sha256(body).hexdigest() != entry.digest:
        raise SourceConflict("record_unavailable", "file custody mismatch")
    return body


def file_evidence(entry: ManifestEntry | None, policy_version: str) -> Evidence:
    if entry is None:
        return Evidence(kind='absent', policy_version=policy_version)
    return Evidence(kind='known', policy_version=policy_version, source_digest=entry.digest,
                    value={'path': entry.path, 'digest': entry.digest, 'size': entry.size, 'mode': entry.mode})


def compare_file(base: Evidence, local: Evidence, incoming: Evidence, *,
                 independently_owned: bool = False) -> UnitDecision:
    result = compare_unit(base, local, incoming, independently_owned=independently_owned)
    if result.action in ('keep_local', 'already_matches'):
        return result
    if any(value.kind == 'known' and isinstance(value.value, dict) and value.value.get('mode') is None
           for value in (base, local, incoming)):
        return UnitDecision('review', tuple(dict.fromkeys(('unknown_file_mode', *result.reasons))))
    return result


def script_change(path: str, base: Evidence, local: Evidence, incoming: Evidence) -> bool:
    if same_value(base, incoming):
        return False
    if path.startswith('scripts/'):
        return True
    return any(evidence.kind == 'known' and isinstance(evidence.value, dict)
               and evidence.value.get('mode') == 0o100755 for evidence in (base, local, incoming))
