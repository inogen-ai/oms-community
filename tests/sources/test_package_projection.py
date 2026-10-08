from dataclasses import replace
from hashlib import sha256

import pytest

from oms.adapters.blobs.file_blob_store import FileBlobStore
from oms.domain.identity import SkillRef
from oms.sources.models import AcquiredPackage, Evidence, ManifestEntry, OriginRef, ResolvedRef
from oms.sources.projection import ProjectionBuilder


def acquired(tmp_path, text, *, files=None):
    blobs = FileBlobStore(tmp_path)
    entries = {"SKILL.md": text.encode(), **(files or {})}
    manifest = tuple(ManifestEntry(path=path, blob_ref=blobs.put(body), digest=sha256(body).hexdigest(),
                                   size=len(body), mode=0o100644) for path, body in entries.items())
    package = AcquiredPackage(resolved_ref=ResolvedRef(canonical_url="https://github.com/example/skills",
        kind="branch", name="main", commit="a" * 40), package_path="skills/expenses",
        manifest=manifest, raw_frontmatter=Evidence(kind="known", value="retained raw", policy_version="acquisition"),
        history_evidence="initial")
    origin = OriginRef(skill=SkillRef("acme", "expenses"), origin_id="origin", kind="github", generation=1)
    return ProjectionBuilder(blobs, tmp_path / "parse"), package, origin


def test_projection_preserves_declared_licence_raw_source_and_file_mode(tmp_path):
    text = "---\nname: expenses\ndescription: File receipts\nlicense: ' MIT '\ncustom: retained\n---\n## Rules\n- Keep receipts.\n## Output format\nShow totals.\n"
    builder, package, origin = acquired(tmp_path, text, files={"CLAUDE.md": b"Do not import as constraints"})
    snapshot = builder.build(package, origin, snapshot_id="snapshot")
    fields = {part.part_id: part.evidence for part in snapshot.effective_projection}
    assert fields["field:license"].value == " MIT "
    assert "custom: retained" in snapshot.raw_frontmatter.value
    assert fields["file:CLAUDE.md"].value["mode"] == 0o100644
    assert not any(part.part_id.startswith("constraint:") for part in snapshot.effective_projection)
    assert snapshot.ref.origin == origin


def test_lossy_heading_collision_is_rejected_before_projection(tmp_path):
    builder, package, origin = acquired(tmp_path, "---\nname: expenses\n---\n## A & B\nFirst\n## A-B\nSecond\n")
    with pytest.raises(ValueError, match="heading"):
        builder.build(package, origin, snapshot_id="snapshot")


def test_rule_units_use_exact_text_instead_of_lossy_import_id(tmp_path):
    builder, package, origin = acquired(tmp_path, "---\nname: expenses\n---\n## Rules\n- Save café receipts.\n- Save cafe receipts!\n")
    snapshot = builder.build(package, origin, snapshot_id="snapshot")
    rules = [part for part in snapshot.effective_projection if part.kind == "rule"]
    assert len(rules) == 2 and rules[0].part_id != rules[1].part_id
    assert {part.evidence.value["body"] for part in rules} == {"Save café receipts.", "Save cafe receipts!"}


def test_missing_declared_licence_is_known_absence(tmp_path):
    builder, package, origin = acquired(tmp_path, "---\nname: expenses\n---\n## Rules\n- Keep receipts.\n")
    snapshot = builder.build(package, origin, snapshot_id="snapshot")
    assert next(part.evidence for part in snapshot.effective_projection if part.part_id == "field:license").kind == "absent"


def test_manifest_digest_mismatch_cannot_be_projected(tmp_path):
    builder, package, origin = acquired(tmp_path, "---\nname: expenses\n---\n## Rules\n- Keep receipts.\n")
    changed = package.model_copy(update={"manifest": (ManifestEntry.model_validate(package.manifest[0].model_dump() | {"digest": "b" * 64}),)})
    with pytest.raises(ValueError, match="digest"):
        builder.build(changed, origin, snapshot_id="snapshot")


@pytest.mark.parametrize("declaration", ["license: [MIT, Apache]\n", "license: null\n", "license: 42\n", "license: MIT\nlicense: Apache\n"])
def test_unsupported_or_ambiguous_licence_is_unknown(tmp_path, declaration):
    builder, package, origin = acquired(tmp_path, "---\nname: expenses\n" + declaration + "---\n## Rules\n- Keep receipts.\n")
    snapshot = builder.build(package, origin, snapshot_id="snapshot")
    evidence = next(part.evidence for part in snapshot.effective_projection if part.part_id == "field:license")
    assert evidence.kind == "unknown"
    assert declaration.rstrip() in snapshot.raw_frontmatter.value


def test_authoritative_mappings_preserve_graph_ids_and_effective_placement(tmp_path):
    from oms.sources.models import GraphMapping
    builder, package, origin = acquired(tmp_path, "---\nname: expenses\n---\n## Rules\n- Keep receipts.\n")
    initial = builder.build(package, origin, snapshot_id="initial")
    section = next(part for part in initial.effective_projection if part.kind == "section")
    rule = next(part for part in initial.effective_projection if part.kind == "rule")
    mappings = (GraphMapping(part_id=section.part_id, entity_ids=("existing-section",), owner_skills=(origin.skill,)),
                GraphMapping(part_id=rule.part_id, entity_ids=("existing-rule",), owner_skills=(origin.skill,), section_id="existing-section"))
    projected = builder.build(package, origin, snapshot_id="next", mappings=mappings)
    actual_rule = next(part for part in projected.effective_projection if part.kind == "rule")
    actual_mapping = next(mapping for mapping in projected.graph_mappings if mapping.part_id == rule.part_id)
    assert actual_mapping.entity_ids == ("existing-rule",)
    assert actual_mapping.section_id == actual_rule.evidence.value["section_id"] == "existing-section"
    assert next(part for part in projected.parsed_projection if part.kind == "rule").evidence.value["section_id"] == section.part_id


def test_normalized_file_path_collisions_are_refused(tmp_path):
    builder, package, origin = acquired(tmp_path, "---\nname: expenses\n---\n## Rules\n- Keep receipts.\n",
                                       files={"réf.md": b"one", "re\u0301f.md": b"two"})
    with pytest.raises(ValueError, match="path"):
        builder.build(package, origin, snapshot_id="snapshot")


def test_nested_packages_are_not_parent_files_and_reserved_bytes_remain_custody(tmp_path):
    builder, package, origin = acquired(tmp_path, "---\nname: expenses\n---\n## Rules\n- Keep receipts.\n",
        files={"nested/SKILL.md": b"child", "nested/private.txt": b"child", ".DS_Store": b"debris",
               "CLAUDE.md": b"retained instructions", "references/edge-cases.md": b"- Keep unusual receipts.\n"})
    projected = builder.build(package, origin, snapshot_id="snapshot")
    entries = {entry.path: entry for entry in projected.manifest}
    assert set(entries) == {"SKILL.md", "CLAUDE.md", "references/edge-cases.md"}
    assert not entries["CLAUDE.md"].published
    assert entries["CLAUDE.md"].exclusion_reason == "reserved_instruction_file"
    assert entries["references/edge-cases.md"].exclusion_reason == "generated_rule_overflow"
    assert any(part.kind == "rule" and part.evidence.value["reference_only"] for part in projected.effective_projection)
    assert not any(part.part_id == "file:references/edge-cases.md" for part in projected.effective_projection)


def test_document_mode_is_managed_from_manifest_not_frontmatter(tmp_path):
    builder, package, origin = acquired(tmp_path, "---\nname: expenses\ndocument_mode: 33261\n---\n## Rules\n- Keep receipts.\n")
    snapshot = builder.build(package, origin, snapshot_id="snapshot")
    mode = next(part for part in snapshot.effective_projection if part.part_id == "field:document_mode")
    assert mode.evidence.kind == "known" and mode.evidence.value == 0o100644
    package = package.model_copy(update={"manifest": tuple(entry.model_copy(update={"mode": None}) for entry in package.manifest)})
    snapshot = builder.build(package, origin, snapshot_id="unknown-mode")
    assert next(part.evidence for part in snapshot.effective_projection if part.part_id == "field:document_mode").kind == "unknown"
