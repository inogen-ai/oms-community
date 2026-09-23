"""Source identity, import isolation and rejection of pre-fix mixed records."""
from copy import deepcopy
from dataclasses import asdict, replace
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from oms.community.app import build_community, build_memory
from oms.domain.models import Edge, ReviewItem, Rule, Skill
from oms.domain.types import EdgeType, RuleStatus, Verdict
from oms.import_skills.identity import ImportIdentityConflict
from oms.import_skills.importer import ImportReport
from oms.import_skills.parser import parse_directory
from oms.settings.core import CoreSettings
from oms.skills.upload import UploadRefused
from tests.community.test_critic_workflow import critic_neo4j_driver  # noqa: F401


@pytest.fixture(params=["memory", pytest.param("neo4j", marks=pytest.mark.integration)])
def services(request, tmp_path):
    if request.param == "memory":
        return build_memory(tmp_path / "data", tenant="acme")
    driver = request.getfixturevalue("critic_neo4j_driver")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    return build_community(CoreSettings(data_dir=tmp_path / "data", tenant_id="acme"), driver)


def files(scope, name="run", *, rule=None, body=None):
    rule = rule or f"Keep the {scope} result."
    body = body or f"Show the {scope} output."
    return {f"{scope}/skills/{name}/SKILL.md": (
        f"---\nname: {name}\ndescription: Use for {scope}.\n---\n\n"
        f"# {scope} {name}\n\n## Rules\n\n- {rule}\n\n"
        f"## Output format\n\n{body}\n").encode()}


def stage(services, entries):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return services.uploads.stage(buffer.getvalue(), "acme")


def apply(services, entries):
    staged = stage(services, entries)
    return services.uploads.apply(staged.id, "acme")


def index(services):
    return {s.import_source_ref: s for s in services.store.skills_for_tenant("acme")
            }


def snapshot(services):
    return deepcopy({"skills": [asdict(s) for s in services.store.skills_for_tenant("acme")],
        "rules": {s.id: [asdict(r) for r in services.store.rules_for_skill(s.id)]
                  for s in services.store.skills_for_tenant("acme")},
        "reviews": [asdict(r) for r in services.queue.pending("acme")]})


def test_first_import_and_separate_imports_have_the_same_source_ids(services):
    entries = files("agenthub") | files("autoresearch-agent")
    preview = stage(services, entries)
    expected = {d.source_ref: d.skill_id for d in preview.skills}
    assert len(set(expected.values())) == 2
    services.uploads.discard(preview.id, "acme")
    apply(services, files("agenthub"))
    apply(services, files("autoresearch-agent"))
    assert {src: s.id for src, s in index(services).items()} == expected
    report = apply(services, entries)
    assert report.rules_created == 0 and report.rules_flagged_removed == 0
    assert not services.queue.pending("acme")
    for skill in index(services).values():
        assert len(services.store.rules_for_skill(skill.id)) == 1
        assert services.store.rules_for_skill(skill.id)[0].corroboration_count == 1


def test_slug_similar_paths_have_independent_transactions_and_reviews(services):
    apply(services, files("a-b") | files("a_b"))
    skills = index(services)
    transactions = [services.store.lineage(services.store.rules_for_skill(s.id)[0].id)[0]
                    for s in skills.values()]
    assert len({t.id for t in transactions}) == 2
    assert len({t.source_ref for t in transactions}) == 2
    apply(services, files("a-b", rule="Keep the new result."))
    reviews = services.queue.pending("acme")
    assert len(reviews) == 1 and reviews[0].kind == "removal"
    assert "a-b/skills/run/SKILL.md" in reviews[0].reason


def test_namespaced_import_does_not_adopt_an_unbound_manual_skill(services):
    services.store.upsert_skill(Skill(id="run", name="My local run", description="Mine",
                                      domain="general", tenant_id="acme"))
    apply(services, files("agenthub"))
    assert services.store.get_skill("run").name == "My local run"
    assert services.store.get_skill("run").import_source_ref is None
    assert len(services.store.skills_for_tenant("acme")) == 2


def test_package_local_forward_routing_reaches_the_qualified_skill(services):
    entries = files("agenthub") | files("agenthub", "status") | files("autoresearch-agent", "status")
    entries["agenthub/skills/run/SKILL.md"] += b"\n## Related Skills\n\n- **status**: Check progress.\n"
    apply(services, entries)
    skills = index(services)
    source = skills["agenthub/skills/run/SKILL.md"].id
    target = skills["agenthub/skills/status/SKILL.md"].id
    neighbours = services.store.graph_neighbours(source, "acme", limit=100)
    assert any(edge["type"] == "ROUTES_TO" and edge["target"] == target for edge in neighbours["edges"])


def test_failed_import_keeps_previous_payload_evidence(services, monkeypatch):
    apply(services, files("agenthub") | files("autoresearch-agent"))
    before = snapshot(services)
    rule = services.store.rules_for_skill(index(services)["agenthub/skills/run/SKILL.md"].id)[0]
    txn = services.store.lineage(rule.id)[0]
    payload = services.payloads.get(txn.id)
    assert payload is not None
    real = type(services.importer)._import_skill

    def fail(self, skill, *args, **kwargs):
        if skill.source_ref.startswith("autoresearch-agent/"):
            raise RuntimeError("simulated import failure")
        return real(self, skill, *args, **kwargs)

    monkeypatch.setattr(type(services.importer), "_import_skill", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        apply(services, files("agenthub", rule="Keep a different result.") | files("autoresearch-agent"))
    assert snapshot(services) == before
    assert services.payloads.get(txn.id) == payload


def legacy(services, tmp_path, *, ruleless=False):
    """Seed exactly the old sequential name-only import, bypassing preflight."""
    entries = files("agenthub") | files("autoresearch-agent")
    if ruleless:
        entries = {p: b.replace(b"## Rules", b"## Overview") for p, b in entries.items()}
    root = tmp_path / "original"
    for path, body in entries.items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
    vocab = services.importer._vocabulary("acme")
    for parsed in parse_directory(root, vocabulary=vocab).skills:
        services.importer._import_skill(parsed, "acme", vocab, ImportReport())
    old = services.store.get_skill("run")
    services.store.upsert_skill(replace(old, import_source_ref=None, import_name=None))
    if not ruleless:
        first = next(r for r in services.store.rules_for_skill("run") if "agenthub" in r.body)
        services.queue.enqueue_once(ReviewItem(id="false-removal", kind="removal", subject_id=first.id,
            other_id=None, verdict=Verdict.AMBIGUOUS, tenant_id="acme",
            reason="rule absent from re-imported autoresearch-agent/skills/run/SKILL.md"))
    return root, entries


@pytest.mark.parametrize("ruleless", [False, True])
def test_legacy_mixed_records_are_refused_before_any_writes(services, tmp_path, ruleless):
    root, entries = legacy(services, tmp_path, ruleless=ruleless)
    before = snapshot(services)
    # Even constraints and unrelated earlier skills must not be partly applied.
    entries.update(files("aaa", "new-skill"))
    entries["CLAUDE.md"] = b"# Constraints\n\n* Keep an audit log.\n"
    with pytest.raises(UploadRefused, match="clean workspace"):
        stage(services, entries)
    with pytest.raises(ImportIdentityConflict, match="clean workspace"):
        services.importer.import_directory(root, "acme")
    assert snapshot(services) == before
    assert not services.store.all_constraints("acme")


def test_ambiguous_routing_names_are_refused_before_any_writes(services):
    entries = files("pkg", "start")
    entries["pkg/skills/start/SKILL.md"] += b"\n## Related Skills\n\n- **run**: Begin work.\n"
    for directory in ("a", "b"):
        entries[f"pkg/skills/{directory}/SKILL.md"] = next(iter(files(directory).values()))
    with pytest.raises(UploadRefused, match="matches more than one skill"):
        stage(services, entries)
    assert not services.store.skills_for_tenant("acme")


def test_two_layout_aliases_cannot_create_two_owners_for_one_source(services):
    first = next(iter(files("a", "first").values()))
    second = next(iter(files("b", "second").values()))
    with pytest.raises(UploadRefused, match="Import each source only once"):
        stage(services, {"run/SKILL.md": first, "skills/run/SKILL.md": second})
    assert not services.store.skills_for_tenant("acme")
