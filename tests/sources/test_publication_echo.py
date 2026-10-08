from pathlib import Path

from oms.community.app import build_memory
from oms.import_skills.parser import parse_directory


def published_case(tmp_path):
    source = tmp_path / "incoming" / "skills" / "expenses"
    (source / "references").mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: expenses\ndescription: Keep receipts\n---\n# Expenses\n\n## Rules\n\n- Keep all receipts.\n")
    (source / "references" / "check.txt").write_bytes(b"first")
    services = build_memory(tmp_path / "data", tenant="acme")
    services.importer.import_directory(tmp_path / "incoming", "acme")
    output = tmp_path / "published"
    services.publisher.publish("acme", output)
    return services, output


def test_whole_package_echo_requires_unchanged_supporting_bytes_and_modes(tmp_path):
    services, output = published_case(tmp_path)
    parsed = parse_directory(output).skills[0]
    assert services.importer._is_unchanged_against_ledger(parsed, "acme")
    path = output / parsed.source_ref
    (path.parent / "references" / "check.txt").write_bytes(b"changed")
    changed = parse_directory(output).skills[0]
    assert not services.importer._is_unchanged_against_ledger(changed, "acme")
    (path.parent / "references" / "check.txt").write_bytes(b"first")
    (path.parent / "references" / "check.txt").chmod(0o755)
    executable = parse_directory(output).skills[0]
    assert not services.importer._is_unchanged_against_ledger(executable, "acme")


def test_source_file_mode_is_published_and_roundtrips(tmp_path):
    services, output = published_case(tmp_path)
    skill = services.store.skills_for_tenant("acme")[0]
    artefact, relative = services.store.artefacts_for_skill(skill.id, tenant_id="acme")[0]
    artefact.mode = 0o100755
    services.store.upsert_artefact(artefact, skill.id, relative, tenant_id="acme")
    services.publisher.publish("acme", output)
    target = output / "skills" / skill.id / relative
    assert target.stat().st_mode & 0o111
    assert services.store.get_publication("acme", f"skills/{skill.id}/{relative}").mode == 0o100755


def test_publication_description_is_derived_without_changing_stored_skill(mutation_case):
    from oms.domain.models import Edge, Rule
    from oms.domain.types import EdgeType
    from oms.publish.publisher import Publisher
    store = mutation_case.store
    skill = store.get_skill("expenses", tenant_id="acme")
    skill.description = ""
    store.upsert_skill(skill)
    rule = Rule(id="description-guidance", body="Keep original receipts for expense claims.", tenant_id="acme")
    store.upsert_rule(rule)
    store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id=rule.id, to_id=skill.id), tenant_id="acme")
    generation = mutation_case.capture_generation()
    publisher = Publisher(store)
    projected, rendered = publisher.publishable_skills("acme")[0]
    assert "original receipts" in projected.description
    assert projected.description in rendered.skill_md
    assert "original receipts" in publisher.render_root("acme")
    assert store.get_skill("expenses", tenant_id="acme").description == ""
    assert mutation_case.capture_generation() == generation
