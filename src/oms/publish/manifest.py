"""Complete publication manifests for exact package echo detection."""
import json
import os
from pathlib import Path
from collections.abc import Mapping

from oms.domain.models import Publication

PUBLICATION_POLICY_VERSION = "package-1"


def supports_file_modes() -> bool:
    return os.name != "nt"


def file_mode(path: Path) -> int | None:
    if not supports_file_modes():
        return None
    return 0o100755 if path.stat().st_mode & 0o111 else 0o100644


def intended_mode(mode: int | None) -> int | None:
    return (mode or 0o100644) if supports_file_modes() else None


# Ownership recorded before file modes were. Its bytes are the only proof, and
# an unknown mode is not evidence of a local edit (spec §7.6). `None` remains
# unverified mode evidence, which never proves ownership.
LEGACY_MODE = "legacy"


def mode_matches(path: Path, expected: int | str | None) -> bool:
    return (not supports_file_modes() or expected == LEGACY_MODE
            or (expected is not None and file_mode(path) == expected))


def package_manifest(records: Mapping[str, tuple[str, int | None, int]], prefix: str) -> str:
    rows = [{"path": path[len(prefix):], "digest": digest, "size": size, "mode": mode}
            for path, (digest, mode, size) in sorted(records.items()) if path.startswith(prefix)]
    return json.dumps(rows, sort_keys=True, separators=(",", ":"))


def is_package_echo(publication: Publication, skill) -> bool:
    return is_manifest_echo(publication, skill_id=skill.id, manifest=getattr(skill, "package_manifest", ()),
        complete=getattr(skill, "manifest_complete", False), source_digest=getattr(skill, "source_digest", None))


def is_manifest_echo(publication: Publication, *, skill_id: str, manifest,
                     complete: bool, source_digest: str | None) -> bool:
    if (not complete or publication.manifest_json is None
            or publication.skill_id != skill_id
            or publication.policy_version != PUBLICATION_POLICY_VERSION
            or publication.content_hash != source_digest):
        return False
    try:
        expected = json.loads(publication.manifest_json)
    except (TypeError, json.JSONDecodeError):
        return False
    required = {"path", "digest", "size", "mode"}
    if not isinstance(expected, list) or any(not isinstance(row, dict) or set(row) != required for row in expected):
        return False
    incoming = [{"path": entry.path, "digest": entry.digest, "size": entry.size, "mode": entry.mode}
                for entry in manifest]
    if any(row["mode"] is None for row in (*expected, *incoming)):
        return False
    return sorted(expected, key=lambda row: row["path"]) == sorted(incoming, key=lambda row: row["path"])
