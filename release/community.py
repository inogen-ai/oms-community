"""Assemble a reviewed public tree and build its exact release candidate.

No command pushes a Git repository, uploads a package or promotes a release.
The manifest command is an explicit review step; export never learns new files
from an untrusted candidate or copies a source repository's Git directory.
"""
from __future__ import annotations

import argparse
import ast
import configparser
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from release.artefacts import Artefact, Candidate, ReleaseError, verify_candidate
from release.public_boundary import (
    PRIVATE_DISTRIBUTIONS, PROTECTED_MODULES,
    _constant_string, _inspect_source, inspect_public_archive,
)


ROOTS = (
    ("community", ""),
    ("frontend/community", "frontend/community"),
    ("frontend/packages/client", "frontend/packages/client"),
    ("frontend/packages/ui-core", "frontend/packages/ui-core"),
)
EXTRA_FILES = {
    "release/__init__.py": "release/__init__.py",
    "release/artefacts.py": "release/artefacts.py",
    "release/public_boundary.py": "release/public_boundary.py",
    "release/cli.py": "release/cli.py",
    "release/community.py": "release/community.py",
    "tests/__init__.py": "tests/__init__.py",
    "tests/community/test_critic_workflow.py": "tests/community/test_critic_workflow.py",
    "tests/community/test_critic_catalogue.py": "tests/community/test_critic_catalogue.py",
    "tests/community/test_critic_api.py": "tests/community/test_critic_api.py",
    "tests/community/test_critic_import_review.py": "tests/community/test_critic_import_review.py",
    "tests/community/test_import_identity.py": "tests/community/test_import_identity.py",
    "tests/community/test_repository_views.py": "tests/community/test_repository_views.py",
    "tests/community/test_recent_skill_changes.py": "tests/community/test_recent_skill_changes.py",
    "tests/community/test_matching.py": "tests/community/test_matching.py",
    "tests/community/test_manual_amendment.py": "tests/community/test_manual_amendment.py",
    "tests/community/test_todo_critic.py": "tests/community/test_todo_critic.py",
    "tests/community/test_skill_deletion.py": "tests/community/test_skill_deletion.py",
    "tests/community/test_git_access.py": "tests/community/test_git_access.py",
    "tests/community/test_publication_destinations.py": "tests/community/test_publication_destinations.py",
    "tests/community/test_contribution_endpoint.py": "tests/community/test_contribution_endpoint.py",
    "tests/editions/test_critic_schema.py": "tests/schema/test_critic_schema.py",
    "tests/repo/test_community_launcher.py": "tests/launcher/test_local.py",
}
IGNORED_DIRECTORIES = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".next", "out", "dist", "build", "test-results", "playwright-report",
}
COMMERCIAL_ROUTES = (
    "/api/people", "/api/teams", "/api/domains", "/api/mutes", "/api/enrol",
    "/api/portal", "/api/me/", "/api/redactions", "/api/pipeline",
    "/api/activity", "/api/usage", "/api/cypher", "/api/queries",
    "/api/reflect", "/api/admin", "/api/overlay", "/api/skill-uploads",
    "/api/settings/models",
)
# Tests under tests/community ship with the public repository through
# EXTRA_FILES. A test file listed here is deliberately private; anything
# in neither set fails tests/repo/test_export_test_manifest.py, so a new
# public test cannot silently stay behind.
PRIVATE_ONLY_TESTS: frozenset[str] = frozenset()
_SECRETS = (
    re.compile(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\r\n]+[A-Za-z0-9+/=]{20}"),
    re.compile(rb"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{24,}"),
    re.compile(rb"\bsk_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(rb"\bsk-(?:proj|ant)-[A-Za-z0-9_-]{24,}"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(rb"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(rb"(?m)^\s*OMS_ACTIVATION_CREDENTIAL\s*=\s*[^\s#]{16,}"),
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or ".git" in path.parts
            or "\\" in value or ":" in value or path.as_posix() != value
            or any(part.startswith(".env") for part in path.parts)):
        raise ReleaseError(f"unsafe public path: {value}")
    return path


def _read_regular(root: Path, relative: str) -> tuple[bytes, int]:
    path = _relative(relative)
    descriptors = []
    try:
        descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in path.parts[:-1]:
            descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=descriptors[-1]))
        descriptor = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=descriptors[-1])
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ReleaseError(f"public input is not a regular file: {relative}")
            limit = 50 * 1024 * 1024
            if before.st_size > limit:
                raise ReleaseError(f"public source file exceeds inspection limit: {relative}")
            data = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_mode) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_mode):
            raise ReleaseError(f"public input changed during export: {relative}")
        if len(data) > limit:
            raise ReleaseError(f"public source file exceeds inspection limit: {relative}")
        return data, 0o755 if before.st_mode & 0o111 else 0o644
    except OSError as exc:
        raise ReleaseError(f"cannot safely read public input: {relative}") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _is_private(value: str) -> bool:
    return any(value == prefix or value.startswith(prefix + ".")
               for prefix in PROTECTED_MODULES)


def _runtime_file(name: str) -> bool:
    return (name.startswith("src/")
        or bool(re.match(r"frontend/community/(?:app|components|lib|scripts)/", name))
        or bool(re.match(r"frontend/packages/[^/]+/src/", name)))


def scan_file(name: str, data: bytes, *, runtime: bool = True) -> None:
    """Inspect code, dependency metadata and likely credentials without values in errors."""
    path = _relative(name)
    if any(prefix.replace(".", "/") in path.as_posix() for prefix in PROTECTED_MODULES):
        raise ReleaseError(f"protected implementation path: {name}")
    if any(pattern.search(data) for pattern in _SECRETS):
        raise ReleaseError(f"possible credential in public file: {name}")
    if runtime:
        _inspect_source(name, data)
        if name.endswith((".js", ".jsx", ".mjs", ".ts", ".tsx", ".map")):
            text = data.decode("utf-8")
            if any(route in text for route in COMMERCIAL_ROUTES):
                raise ReleaseError(f"protected route in public frontend: {name}")
    elif name.endswith((".py", ".pyi")):
        # Release guards and their mutation tests can name prohibited modules
        # as data. They still cannot import or execute those implementations.
        tree = ast.parse(data, filename=name)
        for node in ast.walk(tree):
            values = []
            if isinstance(node, ast.Import):
                values = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                values = [node.module or "", *[(node.module or "") + "." + alias.name for alias in node.names]]
            elif isinstance(node, ast.Call) and node.args:
                # Names in guards are harmless data; names passed to dynamic
                # import hooks are executable dependencies.
                function = node.func
                if ((isinstance(function, ast.Name) and function.id in {"__import__", "import_module"})
                        or isinstance(function, ast.Attribute) and function.attr == "import_module"):
                    value = _constant_string(node.args[0])
                    values = [value] if value is not None else []
            if any(_is_private(value) for value in values):
                raise ReleaseError(f"private import in public tooling: {name}")
    if path.name == "pyproject.toml":
        project = tomllib.loads(data.decode())["project"]
        requirements = list(project.get("dependencies", []))
        for group in project.get("optional-dependencies", {}).values():
            requirements.extend(group)
        for requirement in requirements:
            if canonicalize_name(Requirement(requirement).name) in PRIVATE_DISTRIBUTIONS:
                raise ReleaseError(f"private dependency in public project: {name}")
        for entry in project.get("scripts", {}).values():
            if _is_private(entry.split(":")[0]):
                raise ReleaseError(f"private entry point in public project: {name}")
    if path.name == "package.json":
        package = json.loads(data)
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            if any("enterprise" in dep or "commercial" in dep
                   for dep in package.get(key, {})):
                raise ReleaseError(f"private frontend dependency: {name}")


def discover_manifest(repository: Path) -> dict:
    """Prepare the exact file list for review; this does not export anything."""
    files = dict(EXTRA_FILES)
    for source_root, destination_root in ROOTS:
        root = repository / source_root
        if not root.is_dir() or root.is_symlink():
            raise ReleaseError(f"missing public source root: {source_root}")
        for directory, dirs, names in os.walk(root, followlinks=False):
            for name in dirs:
                if (Path(directory) / name).is_symlink():
                    raise ReleaseError("symbolic link in public source tree")
            dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRECTORIES
                             and not name.endswith(".egg-info"))
            for name in sorted(names):
                if (name.startswith(".env") or name.endswith((".pyc", ".pyo", ".tsbuildinfo"))
                        or name == ".DS_Store"):
                    continue
                source = Path(directory) / name
                local = source.relative_to(root).as_posix()
                destination = str(PurePosixPath(destination_root) / local)
                files[source.relative_to(repository).as_posix()] = destination
    if len(set(files.values())) != len(files):
        raise ReleaseError("two sources own the same public export file")
    for source, destination in files.items():
        data, _ = _read_regular(repository, source)
        scan_file(destination, data, runtime=_runtime_file(destination))
    return {"schema_version": 1, "files": dict(sorted(files.items()))}


def _load_manifest(path: Path) -> dict[str, str]:
    value = json.loads(path.read_text())
    if set(value) != {"schema_version", "files"} or value["schema_version"] != 1:
        raise ReleaseError("invalid export manifest schema")
    files = value["files"]
    if not isinstance(files, dict) or not files or not all(
            isinstance(source, str) and isinstance(destination, str)
            for source, destination in files.items()):
        raise ReleaseError("an exact reviewed export file list is required")
    if len(set(files.values())) != len(files):
        raise ReleaseError("duplicate public destination")
    for source, destination in files.items():
        _relative(source)
        _relative(destination)
        allowed = (source in EXTRA_FILES and EXTRA_FILES[source] == destination) or any(
            source.startswith(prefix + "/") and destination == str(
                PurePosixPath(target) / source.removeprefix(prefix + "/"))
            for prefix, target in ROOTS)
        if not allowed:
            raise ReleaseError(f"source is outside the public ownership roots: {source}")
    return files


def _git(root: Path, *arguments: str, data: bytes | None = None) -> bytes:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.check_output(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *arguments],
        input=data, env=env, stderr=subprocess.PIPE)


def verify_repository(root: Path, *, fresh: bool = False) -> dict:
    """Inspect all reachable history and the committed current file manifest."""
    if not (root / ".git").is_dir() or (root / ".git").is_symlink():
        raise ReleaseError("verification requires an independent Git repository")
    for directory, directories, names in os.walk(root / ".git", followlinks=False):
        for name in [*directories, *names]:
            path = Path(directory) / name
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise ReleaseError("public Git internals contain a link or special file")
    metadata = json.loads((root / "PUBLIC_MANIFEST.json").read_text())
    if set(metadata) != {"schema_version", "files"} or metadata["schema_version"] != 1:
        raise ReleaseError("invalid public file manifest")
    files = metadata["files"]
    commits = _git(root, "rev-list", "--all").decode().splitlines()
    roots = _git(root, "rev-list", "--max-parents=0", "--all").decode().splitlines()
    if len(roots) != 1 or (fresh and len(commits) != 1):
        raise ReleaseError("public export must contain exactly one root commit")
    refs = _git(root, "for-each-ref", "--format=%(refname)").decode().splitlines()
    if fresh and refs != ["refs/heads/main"]:
        raise ReleaseError("public export contains unexpected references")
    for path in ("objects/info/alternates", "info/grafts", "shallow"):
        if (root / ".git" / path).exists():
            raise ReleaseError("public export borrows or rewrites repository history")
    reachable = {line.split()[0] for line in _git(root, "rev-list", "--objects", "--all").decode().splitlines()}
    all_objects = set(_git(root, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").decode().splitlines())
    if reachable != all_objects:
        raise ReleaseError("public export contains unreachable historical objects")
    inspected = set()
    for commit in commits:
        for record in filter(None, _git(root, "ls-tree", "-rz", "--full-tree", commit).split(b"\0")):
            header, raw_name = record.split(b"\t", 1)
            mode, kind, oid = header.decode().split()
            name = raw_name.decode()
            if mode not in ("100644", "100755") or kind != "blob":
                raise ReleaseError("historical public tree contains a non-regular file")
            _relative(name)
            if (oid, name) not in inspected:
                scan_file(name, _git(root, "cat-file", "blob", oid), runtime=_runtime_file(name))
                inspected.add((oid, name))
    tree = _git(root, "ls-tree", "-rz", "--full-tree", "HEAD").split(b"\0")
    names = set()
    for record in filter(None, tree):
        header, raw_name = record.split(b"\t", 1)
        mode, kind, oid = header.decode().split()
        name = raw_name.decode()
        if mode not in ("100644", "100755") or kind != "blob":
            raise ReleaseError("public repository contains a link, submodule or special file")
        _relative(name)
        names.add(name)
        content = _git(root, "cat-file", "blob", oid)
        if name == "PUBLIC_MANIFEST.json":
            if (root / name).read_bytes() != content:
                raise ReleaseError("public file manifest differs from its committed bytes")
        else:
            if name not in files or _sha(content) != files[name]["sha256"]:
                raise ReleaseError(f"public Git object differs from the reviewed export: {name}")
            scan_file(name, content, runtime=_runtime_file(name))
            current, current_mode = _read_regular(root, name)
            if (current != content or current_mode != (int(mode, 8) & 0o777)
                    or current_mode != files[name]["mode"]):
                raise ReleaseError(f"public working tree differs from its root commit: {name}")
    if names != set(files) | {"PUBLIC_MANIFEST.json"}:
        raise ReleaseError("public root commit does not match its exact file manifest")
    return {"root_commit": roots[0], "shared_commit": _git(root, "rev-parse", "HEAD").decode().strip(),
            "files": len(files), "objects": len(all_objects)}


def verify_fresh_repository(root: Path) -> dict:
    return verify_repository(root, fresh=True)


def refresh_public_manifest(root: Path) -> None:
    """Explicitly record reviewed tracked changes in the canonical public repo."""
    records = {}
    for raw in filter(None, _git(root, "ls-files", "-z").split(b"\0")):
        name = raw.decode()
        if name == "PUBLIC_MANIFEST.json":
            continue
        content, mode = _read_regular(root, name)
        scan_file(name, content, runtime=_runtime_file(name))
        records[name] = {"sha256": _sha(content), "mode": mode}
    if not records:
        raise ReleaseError("no tracked public files")
    (root / "PUBLIC_MANIFEST.json").write_text(json.dumps(
        {"schema_version": 1, "files": records}, indent=2, sort_keys=True) + "\n")


def export_repository(repository: Path, manifest_path: Path, destination: Path) -> dict:
    files = _load_manifest(manifest_path)
    current = discover_manifest(repository)["files"]
    if files != current:
        raise ReleaseError("public source file list changed; review and update the export manifest")
    if destination.exists():
        report = verify_fresh_repository(destination)
        recorded = json.loads((destination / "PUBLIC_MANIFEST.json").read_text())["files"]
        for source, name in files.items():
            data, mode = _read_regular(repository, source)
            if recorded.get(name) != {"sha256": _sha(data), "mode": mode}:
                raise ReleaseError("existing export has different bytes; choose a new output directory")
        return report
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".public-export-", dir=destination.parent))
    try:
        recorded = {}
        for source, name in sorted(files.items()):
            data, mode = _read_regular(repository, source)
            scan_file(name, data, runtime=_runtime_file(name))
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
            recorded[name] = {"sha256": _sha(data), "mode": mode}
        (stage / "PUBLIC_MANIFEST.json").write_text(json.dumps(
            {"schema_version": 1, "files": recorded}, indent=2, sort_keys=True) + "\n")
        _git(stage, "init", "--initial-branch=main", "--template=")
        _git(stage, "add", "--all")
        _git(stage, "-c", "user.name=OMS public export", "-c",
             "user.email=public-export@invalid.example", "commit", "--no-gpg-sign",
             "-m", "Initial reviewed Community source")
        report = verify_fresh_repository(stage)
        os.rename(stage, destination)
        return report
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _archive_files(path: Path) -> dict[str, bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = {info.filename for info in archive.infolist() if not info.is_dir()}
        if any(name.endswith((".pth", ".egg-link", ".pyc", ".pyo"))
               or PurePosixPath(name).name.startswith(("sitecustomize.", "usercustomize."))
               for name in names):
            raise ReleaseError("public wheel contains an import hook or editable source")
    else:
        with tarfile.open(path) as archive:
            names = {member.name for member in archive if member.isfile()}
    # Validate paths, duplicates, special files and declared sizes before
    # materialising any member. This is structural validation, not approval:
    # the reviewed source comparison below is what approves file ownership.
    inspect_public_archive(path, names)
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {info.filename: archive.read(info) for info in archive.infolist() if not info.is_dir()}
    with tarfile.open(path) as archive:
        return {member.name: archive.extractfile(member).read() for member in archive if member.isfile()}


def inspect_candidate_archives(project: Path, directory: Path) -> tuple[dict, list[Artefact]]:
    """Tie installed package files to reviewed source before recording allowlists."""
    public = json.loads((project / "PUBLIC_MANIFEST.json").read_text())["files"]
    package = tomllib.loads((project / "pyproject.toml").read_text())["project"]
    version = package["version"]
    dist = package["name"].replace("-", "_") + "-" + version
    allowlists, artefacts = {}, []
    for path in sorted(directory.iterdir()):
        if path.name == "candidate.json":
            continue
        files = _archive_files(path)
        if path.suffix == ".whl":
            metadata = {f"{dist}.dist-info/{name}" for name in
                        ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt")}
            for name in ("LICENSE", "NOTICE"):
                archived = f"{dist}.dist-info/licenses/{name}"
                if archived in files:
                    if name not in public or _sha(files[archived]) != public[name]["sha256"]:
                        raise ReleaseError("wheel licence differs from reviewed source")
                    metadata.add(archived)
            expected = {name.removeprefix("src/") for name in public if name.startswith("src/")}
            if set(files) - metadata != expected:
                raise ReleaseError("wheel package files differ from the reviewed public source")
            for name in expected:
                if _sha(files[name]) != public["src/" + name]["sha256"]:
                    raise ReleaseError(f"wheel changed reviewed source bytes: {name}")
            package_name = package["name"]
        elif path.name == dist + ".tar.gz":
            package_name = package["name"]
            expected = {dist + "/" + name for name in public if name.startswith("src/")}
            if not expected.issubset(files):
                raise ReleaseError("source distribution omits reviewed package source")
            generated_metadata = {f"src/{package['name'].replace('-', '_')}.egg-info/{leaf}" for leaf in
                                  ("PKG-INFO", "SOURCES.txt", "dependency_links.txt", "entry_points.txt",
                                   "requires.txt", "top_level.txt")}
            for name, data in files.items():
                if not name.startswith(dist + "/"):
                    raise ReleaseError("source distribution has an unexpected root")
                relative = name.removeprefix(dist + "/")
                generated = relative in {"PKG-INFO", "setup.cfg"} | generated_metadata
                if relative == "setup.cfg":
                    if relative in public:
                        if _sha(data) != public[relative]["sha256"]:
                            raise ReleaseError("source distribution changed reviewed build configuration")
                    else:
                        config = configparser.ConfigParser(interpolation=None, strict=True)
                        try:
                            config.read_string(data.decode())
                            ordinary = (not config.defaults() and config.sections() == ["egg_info"]
                                and dict(config["egg_info"]) == {"tag_build": "", "tag_date": "0"})
                        except (configparser.Error, UnicodeError):
                            ordinary = False
                        if not ordinary:
                            raise ReleaseError("source distribution contains unreviewed generated build configuration")
                if not generated and (relative not in public or _sha(data) != public[relative]["sha256"]):
                    raise ReleaseError(f"source distribution contains unreviewed bytes: {relative}")
        elif path.name.startswith("inogen-oms-") and path.name.endswith(".tgz"):
            metadata = json.loads(files["package/package.json"])
            package_name = metadata["name"]
            leaf = {"@inogen/oms-client": "client", "@inogen/oms-ui-core": "ui-core"}.get(package_name)
            if leaf is None or metadata["version"] != version:
                raise ReleaseError("unexpected public frontend package identity")
            for name, data in files.items():
                source = f"frontend/packages/{leaf}/" + name.removeprefix("package/")
                if not name.startswith("package/") or source not in public or _sha(data) != public[source]["sha256"]:
                    raise ReleaseError("frontend package contains unreviewed source")
        elif path.name == f"oms_community_ui-{version}.tar.gz":
            package_name = "@inogen/oms-community"
            built = project / "frontend/community/out"
            expected = {"site/" + path.relative_to(built).as_posix(): path
                        for path in built.rglob("*") if path.is_file()}
            if set(files) != set(expected) or any(files[name] != path.read_bytes() for name, path in expected.items()):
                raise ReleaseError("static archive differs from the inspected frontend build")
        else:
            raise ReleaseError(f"unexpected candidate artefact: {path.name}")
        inspect_public_archive(path, set(files))
        for name, data in files.items():
            scan_file(name, data)
        allowlists[path.name] = sorted(files)
        artefacts.append(Artefact(filename=path.name, package=package_name, version=version,
                                 sha256=_sha(path.read_bytes()), size=path.stat().st_size))
    return allowlists, artefacts


def _pack_site(root: Path, destination: Path) -> None:
    if not (root / "index.html").is_file():
        raise ReleaseError("the static Community frontend was not built")
    with destination.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path in sorted(root.rglob("*")):
                if path.is_dir() and not path.is_symlink():
                    continue
                name = path.relative_to(root).as_posix()
                data, mode = _read_regular(root, name)
                info = tarfile.TarInfo("site/" + name)
                info.size, info.mode, info.mtime = len(data), mode, 0
                archive.addfile(info, io.BytesIO(data))


def check_installs(project: Path, directory: Path, *, wheelhouse: Path | None = None) -> dict:
    """Install wheel and sdist independently, away from checkout import paths."""
    package = tomllib.loads((project / "pyproject.toml").read_text())["project"]
    name = package["name"].replace("-", "_")
    archives = [*directory.glob("*.whl"), directory / f"{name}-{package['version']}.tar.gz"]
    results = {}
    locked = tomllib.loads((project / "uv.lock").read_text())["package"]
    private = [item["name"] for item in locked if canonicalize_name(item["name"]) in PRIVATE_DISTRIBUTIONS]
    if private:
        raise ReleaseError("public lock contains a private dependency")
    for archive in archives:
        with tempfile.TemporaryDirectory(prefix="oms-public-install-") as scratch:
            root = Path(scratch)
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root),
                   "PYTHONNOUSERSITE": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
            subprocess.run([sys.executable, "-I", "-m", "venv", str(root / "venv")], check=True, env=env)
            python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            constraints = root / "constraints.txt"
            constraints.write_text("\n".join(f"{item['name']}=={item['version']}" for item in locked
                                            if "registry" in item.get("source", {})) + "\n")
            requirements = root / "runtime-requirements.txt"
            subprocess.run(["uv", "export", "--project", str(project.resolve()), "--frozen", "--no-dev",
                            "--no-emit-project", "--format", "requirements-txt", "--output-file", str(requirements)],
                           cwd=root, env={**env, "UV_CACHE_DIR": str(root / "uv-cache")},
                           check=True, stdout=subprocess.DEVNULL)
            args = [str(python), "-I", "-m", "pip", "install"]
            if wheelhouse is not None:
                args += ["--no-index", "--find-links", str(wheelhouse.resolve())]
            subprocess.run([*args, "--only-binary=:all:", "--require-hashes", "--requirement", str(requirements)],
                           check=True, cwd=root, env=env)
            subprocess.run([*args, "--no-deps", "--constraint", str(constraints), str(archive.resolve())],
                           check=True, cwd=root, env=env)
            recorded = json.loads((project / "PUBLIC_MANIFEST.json").read_text())["files"]
            expected = {name.removeprefix("src/"): info["sha256"] for name, info in recorded.items()
                        if name.startswith("src/")}
            verify_bytes = ("import hashlib, importlib.metadata, pathlib\n"
                            "dist = importlib.metadata.distribution('oms-core')\n"
                            f"expected = {expected!r}\n"
                            "for name, digest in expected.items():\n"
                            "    path = pathlib.Path(dist.locate_file(name))\n"
                            "    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:\n"
                            "        raise RuntimeError('installed source differs from reviewed bytes: ' + name)\n")
            subprocess.run([str(python), "-I", "-c", verify_bytes], check=True, cwd=root, env=env)
            probe = """import importlib, importlib.util, pathlib, pkgutil, sys, oms
def require_local(module):
    if not pathlib.Path(module.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve()):
        raise RuntimeError('module escaped installed wheel: ' + module.__name__)
require_local(oms)
for name in ('oms_commercial', 'oms_enterprise', 'openai', 'anthropic', 'fastembed', 'voyageai'):
    if importlib.util.find_spec(name) is not None:
        raise RuntimeError('private package installed: ' + name)
for item in pkgutil.walk_packages(oms.__path__, oms.__name__ + '.'):
    module = importlib.import_module(item.name)
    if getattr(module, '__file__', None):
        require_local(module)
"""
            subprocess.run([str(python), "-I", "-c", probe], check=True, cwd=root, env=env)
            cli = python.with_name("oms.exe" if os.name == "nt" else "oms")
            subprocess.run([str(cli), "--help"], check=True, cwd=root, env=env)
            subprocess.run([str(cli), "serve", "--help"], check=True, cwd=root, env=env)
            inventory = """import importlib.metadata as m, json
print(json.dumps(sorted([{'name': d.metadata['Name'], 'version': d.version,
'license': d.metadata.get('License-Expression') or d.metadata.get('License') or
    '; '.join(x for x in d.metadata.get_all('Classifier', []) if x.startswith('License ::')) or 'UNKNOWN',
'requires': d.requires or []} for d in m.distributions()], key=lambda x: x['name'].lower())))"""
            results[archive.name] = json.loads(subprocess.check_output(
                [str(python), "-I", "-c", inventory], cwd=root, env=env))
            if any(canonicalize_name(item["name"]) in PRIVATE_DISTRIBUTIONS for item in results[archive.name]):
                raise ReleaseError("clean public installation resolved a private dependency")
    inventories = [{(item["name"], item["version"]) for item in inventory} for inventory in results.values()]
    if any(inventory != inventories[0] for inventory in inventories[1:]):
        raise ReleaseError("wheel and sdist resolved different installed dependency versions")
    return results


def build_candidate(project: Path, destination: Path, *, frontend: bool = True,
                    install: bool = True, wheelhouse: Path | None = None) -> dict:
    provenance = verify_repository(project)
    if destination.exists():
        raise ReleaseError("candidate output already exists; never overwrite candidate bytes")
    destination.parent.mkdir(parents=True, exist_ok=True)
    package = tomllib.loads((project / "pyproject.toml").read_text())["project"]
    schema = {}
    for node in ast.parse((project / "src/oms/schema/metadata.py").read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    schema[target.id] = node.value.value
    if (schema.get("CORE_VERSION") != package["version"]
            or type(schema.get("SCHEMA_VERSION")) is not int or schema["SCHEMA_VERSION"] < 1):
        raise ReleaseError("public package version and schema metadata must agree")
    stage = Path(tempfile.mkdtemp(prefix=".community-candidate-", dir=destination.parent))
    source = Path(tempfile.mkdtemp(prefix="oms-public-build-"))
    try:
        # Build only the verified committed tree, excluding local ignored
        # environment files, stale build output and untracked install hooks.
        for name in [*json.loads((project / "PUBLIC_MANIFEST.json").read_text())["files"], "PUBLIC_MANIFEST.json"]:
            content, mode = _read_regular(project, name)
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(mode)
        project = source
        subprocess.run(["uv", "build", "--project", str(project.resolve()), "--wheel", "--sdist",
                        "--python", sys.executable, "--no-python-downloads", "--no-create-gitignore",
                        "--out-dir", str(stage.resolve())], check=True,
                       env={"PATH": os.environ.get("PATH", ""), "HOME": str(source),
                            "UV_CACHE_DIR": str(source / ".uv-cache")})
        if frontend:
            app = project / "frontend/community"
            if json.loads((app / "package.json").read_text()).get("version") != package["version"]:
                raise ReleaseError("Community application and core package versions differ")
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(source), "NEXT_TELEMETRY_DISABLED": "1"}
            subprocess.run(["npm", "ci"], cwd=app, env=env, check=True)
            subprocess.run(["npm", "run", "build"], cwd=app, env=env, check=True)
            for leaf in ("client", "ui-core"):
                subprocess.run(["npm", "pack", "--pack-destination", str(stage.resolve())],
                               cwd=project / "frontend/packages" / leaf, check=True)
            _pack_site(app / "out", stage / f"oms_community_ui-{package['version']}.tar.gz")
        allowlists, artefacts = inspect_candidate_archives(project, stage)
        candidate = Candidate(schema_version=1, version=package["version"],
                              shared_commit=provenance["shared_commit"], shared_schema=schema["SCHEMA_VERSION"],
                              artefacts=artefacts)
        (stage / "candidate.json").write_bytes(candidate.canonical())
        verify_candidate(candidate, stage)
        installs = check_installs(project, stage, wheelhouse=wheelhouse) if install else {}
        components = []
        for item in next(iter(installs.values()), []):
            components.append({"type": "library", "name": item["name"], "version": item["version"],
                               "purl": f"pkg:pypi/{canonicalize_name(item['name'])}@{item['version']}",
                               "licenses": [{"license": {"name": item["license"]}}]})
        if frontend:
            lock = json.loads((project / "frontend/community/package-lock.json").read_text())
            for path, item in lock["packages"].items():
                if not path or item.get("link") or "version" not in item:
                    continue
                name = item.get("name") or path.split("node_modules/")[-1]
                if "commercial" in name or "enterprise" in name:
                    raise ReleaseError("public frontend lock resolved a private dependency")
                installed_metadata = project / "frontend/community" / path / "package.json"
                licence = item.get("license")
                if installed_metadata.is_file():
                    licence = json.loads(installed_metadata.read_text()).get("license", licence)
                components.append({"type": "library", "name": name, "version": item["version"],
                                   "purl": f"pkg:npm/{name.replace('@', '%40')}@{item['version']}",
                                   "licenses": [{"license": {"name": licence or "UNKNOWN"}}]})
        report = {"candidate_sha256": candidate.digest(), **provenance,
                  "build_environment": {
                      "python": platform.python_version(), "platform": platform.platform(),
                      "uv": subprocess.check_output(["uv", "--version"], text=True).strip(),
                      **({"node": subprocess.check_output(["node", "--version"], text=True).strip(),
                          "npm": subprocess.check_output(["npm", "--version"], text=True).strip()}
                         if frontend else {}),
                  },
                  "archive_allowlists": allowlists,
                  "clean_install_checked": install, "frontend_built": frontend,
                  "installed_dependency_inventories": installs,
                  "sbom": {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
                           "components": components},
                  "license_review_required": sorted({item["name"] for item in components
                      if item["licenses"][0]["license"]["name"] == "UNKNOWN"}),
                  "artefacts": [item.model_dump() for item in artefacts]}
        os.rename(stage, destination)
        return report
    finally:
        shutil.rmtree(source)
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    manifest = sub.add_parser("manifest", help="prepare an exact file list for review")
    manifest.add_argument("--repository", type=Path, default=Path("."))
    manifest.add_argument("--out", type=Path, default=Path("release/community-files.json"))
    export = sub.add_parser("export", help="create a fresh local public Git repository")
    export.add_argument("--repository", type=Path, default=Path("."))
    export.add_argument("--manifest", type=Path, default=Path("release/community-files.json"))
    export.add_argument("--out", type=Path, default=Path("dist/public-export"))
    verify = sub.add_parser("verify", help="check all objects in a fresh public repository")
    verify.add_argument("project", type=Path)
    verify.add_argument("--fresh", action="store_true", help="require exactly one initial root commit")
    refresh = sub.add_parser("refresh", help="record tracked public changes before committing them")
    refresh.add_argument("project", type=Path, default=Path("."), nargs="?")
    build = sub.add_parser("build", help="build and inspect immutable public candidate bytes")
    build.add_argument("--project", type=Path, default=Path("."))
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--report", type=Path, required=True)
    build.add_argument("--no-frontend", action="store_true")
    build.add_argument("--no-install-check", action="store_true")
    build.add_argument("--wheelhouse", type=Path)
    args = parser.parse_args()
    if args.command == "manifest":
        report = discover_manifest(args.repository)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"Prepared {len(report['files'])} exact paths for review: {args.out}")
    elif args.command == "export":
        print(json.dumps(export_repository(args.repository, args.manifest, args.out), sort_keys=True))
    elif args.command == "verify":
        print(json.dumps(verify_repository(args.project, fresh=args.fresh), sort_keys=True))
    elif args.command == "refresh":
        refresh_public_manifest(args.project)
    else:
        report = build_candidate(args.project, args.out, frontend=not args.no_frontend,
                                 install=not args.no_install_check, wheelhouse=args.wheelhouse)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(report["candidate_sha256"])


if __name__ == "__main__":
    main()
