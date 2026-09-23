"""release/bump_version.py sets one version across every public artefact."""
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "release" / "bump_version.py"
JSON_FILES = ("frontend/community/package.json", "frontend/community/package-lock.json",
              "frontend/packages/client/package.json", "frontend/packages/ui-core/package.json")


def _make_tree(root: Path) -> None:
    (root / "release").mkdir(parents=True)
    (root / "release/bump_version.py").write_bytes(SCRIPT.read_bytes())
    (root / "pyproject.toml").write_text('[project]\nname = "oms-core"\nversion = "1.1.0"\n')
    (root / "src/oms/schema").mkdir(parents=True)
    (root / "src/oms/schema/metadata.py").write_text('CORE_VERSION = "1.1.0"\nSCHEMA_VERSION = 1\n')
    (root / "frontend/community").mkdir(parents=True)
    (root / "frontend/community/next.config.ts").write_text(
        'const nextConfig = {\n  generateBuildId: async () => process.env.OMS_BUILD_ID || "oms-community-1.1.0",\n};\n')
    (root / "uv.lock").write_text(
        '[[package]]\nname = "devlop"\nversion = "1.1.0"\nsource = { registry = "https://pypi.org/simple" }\n\n'
        '[[package]]\nname = "oms-core"\nversion = "1.1.0"\nsource = { editable = "." }\ndependencies = [\n]\n')
    for name in JSON_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"name": "x", "version": "1.1.0"}
        if name.endswith("package-lock.json"):
            data["packages"] = {"": {"version": "1.1.0"},
                                "node_modules/@inogen/oms-client": {"version": "1.1.0"},
                                "node_modules/@inogen/oms-ui-core": {"version": "1.1.0"},
                                "node_modules/react": {"version": "19.2.4"}}
        path.write_text(json.dumps(data, indent=2) + "\n")


def _run(root: Path, version: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(root / "release/bump_version.py"), version],
                          capture_output=True, text=True, cwd=root)


def test_sets_one_version_everywhere(tmp_path):
    _make_tree(tmp_path)
    result = _run(tmp_path, "1.2.0")
    assert result.returncode == 0, result.stderr
    assert 'version = "1.2.0"' in (tmp_path / "pyproject.toml").read_text()
    assert 'CORE_VERSION = "1.2.0"' in (tmp_path / "src/oms/schema/metadata.py").read_text()
    for name in JSON_FILES:
        assert json.loads((tmp_path / name).read_text())["version"] == "1.2.0", name
    lock = json.loads((tmp_path / "frontend/community/package-lock.json").read_text())["packages"]
    assert lock[""]["version"] == "1.2.0"
    assert lock["node_modules/@inogen/oms-client"]["version"] == "1.2.0"
    assert lock["node_modules/@inogen/oms-ui-core"]["version"] == "1.2.0"
    assert lock["node_modules/react"]["version"] == "19.2.4"
    assert 'process.env.OMS_BUILD_ID || "oms-community-1.2.0"' in (tmp_path / "frontend/community/next.config.ts").read_text()
    uv_lock = (tmp_path / "uv.lock").read_text()
    assert 'name = "oms-core"\nversion = "1.2.0"' in uv_lock
    assert 'name = "devlop"\nversion = "1.1.0"' in uv_lock


def test_refuses_a_version_that_is_not_x_y_z(tmp_path):
    _make_tree(tmp_path)
    result = _run(tmp_path, "1.2")
    assert result.returncode != 0
    assert "usage" in result.stderr
    assert 'version = "1.1.0"' in (tmp_path / "pyproject.toml").read_text()
