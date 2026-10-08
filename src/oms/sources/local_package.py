"""Take a local package into custody under the acquisition budgets, whichever way it arrived."""
from hashlib import sha256
import os
from pathlib import Path
import stat
import unicodedata

from oms.import_skills.parser import is_os_debris
from oms.ports.blob_store import BlobStore
from oms.sources.errors import SourceConflict
from oms.sources.limits import AcquisitionLimits
from oms.sources.models import Evidence, LocalPackage, ManifestEntry
from oms.sources.primitives import exact_digest


def retain_local_packages(root: Path, blobs: BlobStore, *,
                          limits: AcquisitionLimits | None = None) -> tuple[LocalPackage, ...]:
    bounds = limits or AcquisitionLimits()
    selected_file = root if root.is_file() else None
    if root.is_symlink() or selected_file is not None and root.name != "SKILL.md":
        raise SourceConflict("invalid_request", "invalid local package root")
    base = root.parent if selected_file else root
    if not base.is_dir():
        raise SourceConflict("invalid_package_selection", "local package not found")
    files = []
    total, entries = 0, 0
    for parent, directories, names in os.walk(base, followlinks=False):
        directory = Path(parent)
        directories[:] = [name for name in directories if not is_os_debris(name)]
        for name in [*directories, *names]:
            if is_os_debris(name):
                continue
            entries += 1
            path = directory / name
            relative = path.relative_to(base).as_posix()
            if (entries > bounds.tree_entries or len(relative.encode()) > bounds.path_bytes
                or len(path.relative_to(base).parts) > bounds.nesting):
                raise SourceConflict("limit_exceeded", "local inventory limit")
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode) or not (stat.S_ISDIR(details.st_mode) or stat.S_ISREG(details.st_mode)):
                raise SourceConflict("invalid_request", "unsupported local entry")
            if stat.S_ISREG(details.st_mode):
                if details.st_size > bounds.file_bytes:
                    raise SourceConflict("limit_exceeded", "local file limit")
                total += details.st_size
                if total > bounds.expanded_bytes:
                    raise SourceConflict("limit_exceeded", "local inventory limit")
                files.append((path, details))
    roots = sorted(path.parent for path, _ in files if path.name == "SKILL.md")
    if selected_file is not None:
        roots = [base]
    if not roots or len(roots) > 100:
        raise SourceConflict("invalid_package_selection", "local package selection required")
    result = []
    all_roots = {path.parent for path, _ in files if path.name == "SKILL.md"}
    for package_root in roots:
        nested = {path for path in all_roots if package_root in path.parents}
        selected = [(path, details) for path, details in files if package_root in path.parents
                    and not any(child == path.parent or child in path.parents for child in nested)]
        if len(selected) > bounds.files or sum(details.st_size for _, details in selected) > bounds.package_bytes:
            raise SourceConflict("limit_exceeded", "local package limit")
        manifest, canonical = [], set()
        for path, before in sorted(selected):
            relative = path.relative_to(package_root).as_posix()
            key = unicodedata.normalize("NFC", relative).casefold()
            if key in canonical:
                raise SourceConflict("invalid_request", "local path collision")
            canonical.add(key)
            # O_NOFOLLOW prevents a file replaced with a symlink after inventory
            # from becoming a read outside the explicitly selected package.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                actual = os.fstat(stream.fileno())
                if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino):
                    raise SourceConflict("source_changed", "local package changed")
                body = stream.read(bounds.file_bytes + 1)
            after = path.lstat()
            if (len(body) != before.st_size or (after.st_size, after.st_mtime_ns, after.st_mode) !=
                    (before.st_size, before.st_mtime_ns, before.st_mode)):
                raise SourceConflict("source_changed", "local package changed")
            mode = None if os.name == "nt" else (0o100755 if before.st_mode & 0o111 else 0o100644)
            manifest.append(ManifestEntry(path=relative, blob_ref=blobs.put(body), digest=sha256(body).hexdigest(),
                                          size=len(body), mode=mode))
        revision = exact_digest([(entry.path, entry.digest, entry.size, entry.mode) for entry in manifest])
        result.append(LocalPackage(revision=revision, package_path="" if package_root == base
                                   else package_root.relative_to(base).as_posix(),
            manifest=tuple(manifest), raw_frontmatter=Evidence(kind="unknown", policy_version="1")))
    return tuple(result)
