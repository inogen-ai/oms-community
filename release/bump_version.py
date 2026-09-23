"""Set one version number across every public OMS Community artefact.

Run from the public repository root:  python -m release.bump_version 1.2.0
"""
import json
import re
import sys
from pathlib import Path


def _set_json_version(path: Path, version: str) -> None:
    data = json.loads(path.read_text())
    data["version"] = version
    packages = data.get("packages")
    if packages is not None:
        packages[""]["version"] = version
        for key in ("node_modules/@inogen/oms-client", "node_modules/@inogen/oms-ui-core"):
            if key in packages:
                packages[key]["version"] = version
    path.write_text(json.dumps(data, indent=2) + "\n")


def _set_text(path: Path, pattern: str, value: str) -> None:
    updated, count = re.subn(pattern, value, path.read_text())
    if count != 1:
        raise SystemExit(f"expected one version line in {path}, found {count}")
    path.write_text(updated)


def main() -> None:
    version = sys.argv[1] if len(sys.argv) == 2 else ""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise SystemExit("usage: python -m release.bump_version X.Y.Z")
    root = Path(__file__).resolve().parents[1]
    pyproject = root / "pyproject.toml"
    pyproject.write_text(re.sub(r'(?m)^version = "[^"]+"$', f'version = "{version}"', pyproject.read_text()))
    metadata = root / "src/oms/schema/metadata.py"
    metadata.write_text(re.sub(r'(?m)^CORE_VERSION = "[^"]+"$', f'CORE_VERSION = "{version}"', metadata.read_text()))
    for name in ("frontend/community/package.json", "frontend/community/package-lock.json",
                 "frontend/packages/client/package.json", "frontend/packages/ui-core/package.json"):
        _set_json_version(root / name, version)
    _set_text(root / "frontend/community/next.config.ts",
              r'process\.env\.OMS_BUILD_ID \|\| "oms-community-[^"]+"',
              f'process.env.OMS_BUILD_ID || "oms-community-{version}"')
    _set_text(root / "uv.lock", r'(?m)^name = "oms-core"\nversion = "[^"]+"$',
              f'name = "oms-core"\nversion = "{version}"')
    print(f"set version {version}; now run: grep -rn '1\\.1\\.0' README.md docs frontend/*/README.md")


if __name__ == "__main__":
    main()
