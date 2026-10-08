from dataclasses import replace
import os

import pytest

from oms.adapters.blobs.file_blob_store import FileBlobStore
from oms.domain.identity import SkillRef
from oms.sources.errors import SourceConflict
from oms.sources.limits import AcquisitionLimits
from oms.sources.local_package import retain_local_packages
from oms.sources.models import OriginRef
from oms.sources.projection import ProjectionBuilder


def package(root, name="expenses"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Keep records\n---\n## Rules\n- Keep receipts.\n")


def test_local_projection_keeps_exact_bytes_without_repository_identity(tmp_path):
    root = tmp_path / "input"
    package(root)
    (root / "README.md").write_bytes(b"Exact\r\nbytes\r\n")
    blobs = FileBlobStore(tmp_path / "custody")
    retained, = retain_local_packages(root, blobs)
    assert retained.package_path == ""
    assert "resolved_ref" not in retained.model_dump()
    assert blobs.get(next(row.blob_ref for row in retained.manifest if row.path == "README.md")) == b"Exact\r\nbytes\r\n"
    origin = OriginRef(skill=SkillRef("acme", "expenses"), origin_id="local-expenses", kind="local", generation=1)
    snapshot = ProjectionBuilder(blobs, tmp_path / "parser").build(retained, origin, snapshot_id="local-snapshot")
    assert snapshot.revision == retained.revision
    assert snapshot.raw_frontmatter.kind == "known"
    assert snapshot.ref.origin.kind == "local"


def test_parent_manifest_excludes_nested_packages_and_tool_debris(tmp_path):
    root = tmp_path / "input"
    package(root)
    package(root / "nested", "nested")
    package(root / ".git" / "ignored", "ignored")
    retained = retain_local_packages(root, FileBlobStore(tmp_path / "custody"))
    assert [item.package_path for item in retained] == ["", "nested"]
    assert [entry.path for entry in retained[0].manifest] == ["SKILL.md"]


@pytest.mark.skipif(os.name == "nt", reason="Native Windows has no executable mode proof")
def test_executable_mode_changes_local_revision(tmp_path):
    root = tmp_path / "input"
    package(root)
    executable = root / "check.sh"
    executable.write_bytes(b"exit 0\n")
    executable.chmod(0o644)
    blobs = FileBlobStore(tmp_path / "custody")
    before, = retain_local_packages(root, blobs)
    executable.chmod(0o755)
    after, = retain_local_packages(root, blobs)
    assert before.revision != after.revision
    assert next(entry.mode for entry in after.manifest if entry.path == "check.sh") == 0o100755


def test_local_inventory_refuses_symlinks_collisions_and_limits(tmp_path):
    root = tmp_path / "input"
    package(root)
    blobs = FileBlobStore(tmp_path / "custody")
    (root / "outside").symlink_to(tmp_path / "secret")
    with pytest.raises(SourceConflict, match="unsupported local entry"):
        retain_local_packages(root, blobs)
    (root / "outside").unlink()
    (root / "README.md").write_bytes(b"one")
    (root / "readme.md").write_bytes(b"two")
    with pytest.raises(SourceConflict, match="path collision"):
        retain_local_packages(root, blobs)
    with pytest.raises(SourceConflict, match="file limit"):
        retain_local_packages(root, blobs, limits=replace(AcquisitionLimits(), file_bytes=2))
