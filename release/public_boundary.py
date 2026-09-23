"""Inspect built public archives against an explicit reviewed file allowlist.

The existing combined distribution deliberately fails this check. A public
build is not authorised until the separate Community extraction passes.
"""
import ast
from email.parser import BytesParser
import importlib.util
from pathlib import Path, PurePosixPath
import re
import tarfile
import zipfile
from packaging.requirements import Requirement, InvalidRequirement
from packaging.utils import canonicalize_name

from release.artefacts import ReleaseError

PROTECTED_MODULES = (
    "oms_commercial", "oms_enterprise", "oms.compiler", "oms.bench", "oms.eval",
    "oms.adapters.anthropic", "oms.adapters.openrouter", "oms.adapters.llm_base",
    "oms.adapters.reviser_base", "oms.adapters.claude_code", "oms.adapters.voyage",
    "oms.adapters.nomic", "oms.adapters.presidio",
    "oms.activity.service", "oms.activity.story", "oms.activity.copy",
    "oms.capture", "oms.identity", "oms.teams", "oms.mutes",
    "oms.licensing", "oms.vault", "oms.devmode", "oms.review",
    "oms.publish.managed", "oms.publish.overlay", "oms.skills.consistency",
    "oms.skills.upload_queue",
    "oms.ports.language_model", "oms.ports.reviser", "oms.ports.embedder",
    "oms.ports.identity", "oms.ports.teams", "oms.ports.mutes",
    "oms.adapters.fake.language_model", "oms.adapters.fake.embedder",
    "oms.adapters.piguard", "oms.adapters.promptarmor", "oms.adapters.promptguard",
    "oms.adapters.supermemory", "oms.adapters.vault",
    "oms.adapters.neo4j.identity", "oms.adapters.neo4j.teams", "oms.adapters.neo4j.mutes",
    "oms.adapters.neo4j.enrolment",
    "oms.adapters.memory.identity", "oms.adapters.memory.teams", "oms.adapters.memory.mutes",
    "oms.adapters.memory.enrolment",
)
PROTECTED_PATHS = tuple(module.replace(".", "/") for module in PROTECTED_MODULES) + (
    "vendor/", "evals/", ".git/", ".env",
)
PRIVATE_DISTRIBUTIONS = {"oms-commercial", "oms-enterprise", "openai", "anthropic", "voyageai",
                         "fastembed", "presidio-analyzer", "presidio-anonymizer"}
MAX_MEMBER_SIZE = 50 * 1024 * 1024
MAX_ARCHIVE_SIZE = 250 * 1024 * 1024


def _private_module(value: str) -> bool:
    return any(value == prefix or value.startswith(prefix + ".") for prefix in PROTECTED_MODULES)


def _module_name(path: str) -> tuple[str, str]:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    for i, part in enumerate(parts):
        if part in ("oms", "oms_commercial", "oms_enterprise"):
            parts = parts[i:]
            break
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    module = ".".join(parts)
    return module, module if is_package else module.rpartition(".")[0]


def _constant_string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _constant_string(node.left), _constant_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _inspect_source(name, data):
    if name.endswith((".py", ".pyi")):
        try:
            tree = ast.parse(data)
        except (SyntaxError, ValueError) as exc:
            raise ReleaseError(f"invalid Python source: {name}") from exc
        _, package = _module_name(name)
        values = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                values.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    try:
                        module = importlib.util.resolve_name("." * node.level + module, package)
                    except (ImportError, ValueError) as exc:
                        raise ReleaseError(f"unresolvable relative import: {name}") from exc
                values.append(module)
                values.extend(module + "." + alias.name for alias in node.names)
            value = _constant_string(node)
            if value is not None:
                values.append(value)
        if any(_private_module(value) for value in values):
            raise ReleaseError(f"reverse dependency in public source: {name}")
    elif name.endswith(("/METADATA", "/PKG-INFO")):
        metadata = BytesParser().parsebytes(data)
        for requirement in metadata.get_all("Requires-Dist", []):
            try:
                package = canonicalize_name(Requirement(requirement).name)
            except InvalidRequirement as exc:
                raise ReleaseError(f"invalid dependency in public metadata: {name}") from exc
            if package in PRIVATE_DISTRIBUTIONS:
                raise ReleaseError(f"private dependency in public metadata: {name}")
    elif name.endswith(("entry_points.txt", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".json", ".map")):
        text = data.decode("utf-8", errors="replace")
        needles = PROTECTED_MODULES + ("@oms/commercial", "@oms/enterprise", "oms-commercial", "oms-enterprise")
        if any(needle in text or needle.replace(".", "/") in text for needle in needles):
            raise ReleaseError(f"private dependency in public entry point or frontend: {name}")


def inspect_public_archive(path: Path, allowed_files: set[str]) -> list[str]:
    if not allowed_files:
        raise ReleaseError("a reviewed public archive allowlist is required")
    seen = set()
    all_names = set()
    total_size = 0

    def check_member(name, size, *, directory=False, symlink=False):
        nonlocal total_size
        normal = name.rstrip("/") if directory else name
        parts = PurePosixPath(normal).parts
        if (not normal or normal.startswith("/") or ":" in normal or ".." in parts or "\\" in name
                or normal in all_names or symlink or PurePosixPath(normal).as_posix() != normal):
            raise ReleaseError(f"unsafe archive member: {name}")
        all_names.add(normal)
        if any(protected in normal for protected in PROTECTED_PATHS):
            raise ReleaseError(f"protected content in public archive: {name}")
        if directory:
            if not any(file.startswith(normal + "/") for file in allowed_files):
                raise ReleaseError(f"unapproved archive directory: {name}")
            return
        if name not in allowed_files:
            raise ReleaseError(f"unapproved archive member: {name}")
        total_size += size
        if size > MAX_MEMBER_SIZE or total_size > MAX_ARCHIVE_SIZE:
            raise ReleaseError("public archive exceeds inspection size limits")
        seen.add(name)

    def inspect(name, data):
        if (b"PRIVATE KEY-----" in data or b"OMS_ACTIVATION_CREDENTIAL=" in data
                or re.search(rb'"OMS_ACTIVATION_CREDENTIAL"\s*:\s*"[^"\s]+', data)
                or re.search(rb'\bsk_(?:live|test)_[A-Za-z0-9]{16,}', data)):
            raise ReleaseError(f"credential or private key in archive: {name}")
        _inspect_source(name, data)

    if path.suffix == ".whl" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                kind = (item.external_attr >> 16) & 0o170000
                check_member(item.filename, item.file_size, directory=item.is_dir(),
                             symlink=kind not in (0, 0o100000, 0o040000))
                if not item.is_dir():
                    inspect(item.filename, archive.read(item))
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            for item in archive:
                check_member(item.name, item.size, directory=item.isdir(),
                             symlink=not (item.isfile() or item.isdir()))
                if item.isfile():
                    inspect(item.name, archive.extractfile(item).read())
    else:
        raise ReleaseError("unsupported public archive")
    if seen != allowed_files:
        raise ReleaseError("public archive is missing approved files")
    return sorted(seen)
