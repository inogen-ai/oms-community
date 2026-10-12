from pathlib import Path

import pytest

from oms.import_skills.parser import parse_directory


def package(root: Path, path: str, name: str) -> Path:
    folder = root / path
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Help with receipts\n---\n"
        "## Rules\n\n- Keep receipts.\n", encoding="utf-8")
    return folder


def test_selected_parent_excludes_nested_packages_and_organisation_constraints(tmp_path):
    parent = package(tmp_path, "skills/expenses", "expenses")
    package(tmp_path, "skills/expenses/nested", "child")
    package(tmp_path, "skills/unselected", "other")
    (parent / "CLAUDE.md").write_text("- Never expose passwords.\n")
    (parent / "receipt.txt").write_text("Retain the original receipt.\n")
    parsed = parse_directory(tmp_path, selected_roots=("skills/expenses",),
                             extract_constraints=False)
    assert parsed.constraints == []
    assert [skill.id for skill in parsed.skills] == ["expenses"]
    assert {file.path for file in parsed.skills[0].artefacts} == {"CLAUDE.md", "receipt.txt"}


def test_selected_root_package_records_executable_mode(tmp_path):
    root = package(tmp_path, "", "root-package")
    script = root / "check"
    script.write_bytes(b"#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    parsed = parse_directory(tmp_path, selected_roots=("",), extract_constraints=False)
    assert parsed.skills[0].source_ref == "SKILL.md"
    assert parsed.skills[0].artefacts[0].mode == 0o100755


@pytest.mark.parametrize("selection", [("../outside",), ("/outside",),
                                        ("skills/missing",), ("", "")])
def test_invalid_explicit_selection_is_refused(tmp_path, selection):
    package(tmp_path, "", "root-package")
    with pytest.raises(ValueError):
        parse_directory(tmp_path, selected_roots=selection, extract_constraints=False)


def test_empty_explicit_selection_does_not_import_all_packages(tmp_path):
    package(tmp_path, "", "root-package")
    assert parse_directory(tmp_path, selected_roots=(), extract_constraints=False).skills == []


def test_primary_bytes_and_complete_inventory_are_exact(tmp_path):
    from hashlib import sha256
    raw = b"---\r\nname: receipts\r\nlicense: ' MIT '\r\n---\r\n## Rules\r\n- Keep receipts.\r\n"
    primary = tmp_path / "SKILL.md"
    primary.write_bytes(raw)
    primary.chmod(0o755)
    (tmp_path / "CLAUDE.md").write_bytes(b"Keep as a package file\n")
    overflow = tmp_path / "references" / "edge-cases.md"
    overflow.parent.mkdir()
    overflow.write_bytes(b"- Keep rare receipts.\n")
    parsed = parse_directory(tmp_path, selected_roots=("",), extract_constraints=False).skills[0]
    assert parsed.source_digest == sha256(raw).hexdigest()
    assert parsed.source_mode == 0o100755
    assert parsed.declared_license == " MIT "
    assert parsed.manifest_complete
    manifest = {entry.path: entry for entry in parsed.package_manifest}
    assert set(manifest) == {"SKILL.md", "CLAUDE.md", "references/edge-cases.md"}
    assert manifest["SKILL.md"].digest == sha256(raw).hexdigest()
    assert manifest["SKILL.md"].size == len(raw)


def test_text_only_parse_does_not_claim_file_inventory_or_mode():
    from hashlib import sha256
    from oms.import_skills.parser import parse_skill_file
    text = "---\nname: receipts\nlicense: ' MIT '\n---\n## Rules\n- Keep receipts.\n"
    parsed = parse_skill_file(text, source_ref="SKILL.md")
    assert parsed.source_digest == sha256(text.encode()).hexdigest()
    assert parsed.source_mode is None
    assert not parsed.manifest_complete
    assert parsed.package_manifest == []
    assert parsed.declared_license == " MIT "
