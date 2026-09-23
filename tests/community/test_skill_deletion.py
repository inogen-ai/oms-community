"""Independent acceptance checks for deleting a Community skill.

Both adapters run the same HTTP lifecycle checks. Database integration uses
only the disposable Neo4j fixture, never a developer's running workspace.
"""
from dataclasses import asdict
from pathlib import Path

import pytest

from oms.domain.models import ContentBlock, Edge, Example, ReviewItem, Section
from oms.domain.types import EdgeType, ExampleKind, Mutability, SectionKind, Verdict
from oms.publish.catalogue import SkillNotFound
from tests.community.test_todo_critic import (
    INTRO, NEW_INTRO, ORIGIN, _detail, _document, _files, _import, _part, _save,
    _seed_rule, _seed_skill,
    critic_neo4j_driver, workspace,  # noqa: F401: shared parametrised fixtures
)


def _delete(workspace, skill_id):
    response = workspace.client.delete(f"/api/skills/{skill_id}", headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == skill_id
    return response.json()


def _prose_section(workspace, skill_id):
    store = workspace.services.store
    return next(section for section in store.sections_for_skill(skill_id)
                if store.blocks_for_section(section.id))


def _section_review(workspace, item_id, section, *, tenant="acme"):
    block = workspace.services.store.blocks_for_section(section.id)[0]
    item = ReviewItem(id=item_id, kind="block_revision", subject_id=block.id,
        other_id=section.id, verdict=Verdict.AMBIGUOUS, tenant_id=tenant,
        reason="Review the changed source wording.", proposed_body="Keep the reviewed source wording.")
    workspace.services.queue.enqueue_once(item)
    return item


def _publish(workspace):
    response = workspace.client.post("/api/publish", headers=ORIGIN)
    assert response.status_code == 200, response.text
    output = Path(response.json()["output"])
    assert output.resolve().is_relative_to(workspace.root.resolve())
    return output


def _rule_examples(store, rule_id):
    # The existing Neo4j adapter reconstructs Example.created_at at read time;
    # compare its durable content and custody rather than that transient field.
    return {example.id: {key: value for key, value in asdict(example).items()
                         if key != "created_at"}
            for example in store.examples_for_rule(rule_id)}


def test_deletion_removes_owned_structure_and_all_skill_read_surfaces(workspace):
    skill_id = _import(workspace)
    store = workspace.services.store
    detail = _detail(workspace, skill_id)
    sections = store.sections_for_skill(skill_id)
    assert sections and detail["artefacts"] and detail["versions"]
    blocks = [block.id for section in sections for block in store.blocks_for_section(section.id)]
    store.upsert_example(Example(id="owned-section-example", body="An example owned by this section.",
        kind=ExampleKind.POSITIVE, tenant_id="acme", parent_section_id=sections[0].id))
    store.upsert_example(Example(id="owned-skill-example", body="An example owned by this skill.",
        kind=ExampleKind.POSITIVE, tenant_id="acme", parent_skill_id=skill_id))
    assert store.examples_for_skill(skill_id)
    report = _delete(workspace, skill_id)
    assert report["name"] == detail["name"]
    assert report["rules_detached"] == len(detail["rules"])
    assert store.get_skill(skill_id) is None
    assert store.sections_for_skill(skill_id) == []
    assert store.artefacts_for_skill(skill_id) == []
    assert store.examples_for_skill(skill_id) == []
    assert store.skill_versions("acme", skill_id) == []
    for section in sections:
        assert store.get_section(section.id) is None
        assert store.blocks_for_section(section.id) == []
    for block_id in blocks:
        assert store.get_content_block(block_id) is None
    for version in detail["versions"]:
        assert store.get_skill_version(version["id"]) is None

    version = detail["versions"][0]["id"]
    for path in [f"/api/skills/{skill_id}", f"/api/skills/{skill_id}/document",
                 f"/api/skills/{skill_id}/versions", f"/api/skills/{skill_id}/files?path=references/policy.md",
                 f"/api/skills/{skill_id}/versions/{version}/compare",
                 f"/api/publish/preview?skill_id={skill_id}",
                 f"/api/graph/nodes/{skill_id}", f"/api/graph/nodes/{skill_id}/neighbours"]:
        response = workspace.client.get(path)
        assert response.status_code == 404, (path, response.text)
    assert workspace.client.get("/api/skills").json() == []
    assert workspace.client.get("/api/skill-changes").json() == []
    assert skill_id not in {node["id"] for node in workspace.client.get("/api/graph").json()["nodes"]}
    assert workspace.services.catalogue.list_skills("acme") == []
    with pytest.raises(SkillNotFound):
        workspace.services.catalogue.query_skill("acme", skill_id)
    with pytest.raises(SkillNotFound):
        workspace.services.catalogue.query_skill_resource("acme", skill_id, "references/policy.md")
    assert workspace.client.delete(f"/api/skills/{skill_id}", headers=ORIGIN).status_code == 404


def test_shared_rules_evidence_and_other_skills_are_preserved(workspace):
    skill_id = _import(workspace)
    services, store = workspace.services, workspace.services.store
    keeper = _seed_skill(workspace, "keeper")
    shared = store.rules_for_skill(skill_id)[0]
    store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id=shared.id, to_id=keeper.id))
    sole = _seed_rule(workspace, "sole-rule", skill_id)
    store.upsert_example(Example(id="shared-rule-example", body="Keep the original receipt.",
        kind=ExampleKind.POSITIVE, tenant_id="acme", parent_rule_id=shared.id))
    artefact, _ = store.artefacts_for_skill(skill_id)[0]
    store.upsert_artefact(artefact, keeper.id, "references/shared-resource.md")
    keeper_before = _detail(workspace, keeper.id)
    rule_before, sole_before = asdict(shared), asdict(sole)
    lineage_before = {txn.id: asdict(txn) for txn in store.lineage(shared.id)}
    payloads_before = {txn_id: services.payloads.get(txn_id) for txn_id in lineage_before}
    examples_before = _rule_examples(store, shared.id)

    report = _delete(workspace, skill_id)

    assert report["rules_detached"] == 2
    assert asdict(store.get_rule(shared.id)) == rule_before
    assert asdict(store.get_rule(sole.id)) == sole_before
    assert [skill.id for skill in store.skills_for_rule(shared.id)] == [keeper.id]
    assert store.skills_for_rule(sole.id) == []
    assert {txn.id: asdict(txn) for txn in store.lineage(shared.id)} == lineage_before
    assert {txn_id: services.payloads.get(txn_id) for txn_id in lineage_before} == payloads_before
    assert _rule_examples(store, shared.id) == examples_before
    # The keeper's wording is unchanged; its shared rule now truthfully lists
    # only its surviving membership instead of naming the deleted skill.
    for rule in keeper_before["rules"]:
        rule["skill_ids"] = [sid for sid in rule["skill_ids"] if sid != skill_id]
    assert _detail(workspace, keeper.id) == keeper_before
    assert store.artefacts_for_skill(keeper.id)[0][0].content_ref == artefact.content_ref
    assert services.blobs.get(artefact.content_ref)
    assert {rule["id"] for rule in workspace.client.get("/api/rules").json()} == {shared.id, sole.id}


def test_only_reviews_for_deleted_sections_are_resolved(workspace):
    skill_id = _import(workspace)
    store, queue = workspace.services.store, workspace.services.queue
    section = _prose_section(workspace, skill_id)
    doomed = _section_review(workspace, "doomed-source-review", section)
    keeper = _seed_skill(workspace, "keeper")
    other_section = Section(id="keeper-section", skill_id=keeper.id, kind=SectionKind.PROSE,
        heading="Overview", order=1, mutability=Mutability.AUTHORIAL_PASSTHROUGH, tenant_id="acme")
    store.upsert_section(other_section)
    block = ContentBlock(id="keeper-block", content_ref="sha256-keeper", kind=SectionKind.PROSE,
        tenant_id="acme", body="Keep the other skill's source.", source_ref="skills/keeper/SKILL.md")
    store.upsert_content_block(block)
    store.attach_block(block, other_section.id)
    kept = _section_review(workspace, "kept-source-review", other_section)
    foreign = _section_review(workspace, "foreign-review", section, tenant="other")
    rule = store.rules_for_skill(skill_id)[0]
    rule_item = ReviewItem(id="kept-rule-review", kind="removal", subject_id=rule.id,
        other_id=None, verdict=Verdict.AMBIGUOUS, tenant_id="acme", reason="Review this retained rule.")
    queue.enqueue_once(rule_item)
    pending_before = {item.id: asdict(item) for item in (kept, foreign, rule_item)}
    correction = workspace.client.post("/api/ingest", headers=ORIGIN, json={
        "transaction_id": "retained-correction", "correction": "Check expense approvals before submission.",
        "skill_hint": skill_id})
    assert correction.status_code == 202, correction.text

    _delete(workspace, skill_id)

    resolved = queue.get(doomed.id)
    assert resolved.resolved and resolved.resolution
    assert resolved.decided_by == "local-operator" and resolved.decided_at is not None
    for item_id, before in pending_before.items():
        assert asdict(queue.get(item_id)) == before
    assert doomed.id not in {item["id"] for item in workspace.client.get("/api/import-review").json()}
    assert "retained-correction" in {item["txn_id"] for item in workspace.client.get("/api/review").json()}
    # A stale browser cannot apply the discarded source review after deletion.
    for action in ("accept", "reject"):
        response = workspace.client.post(f"/api/import-review/{doomed.id}/decision", headers=ORIGIN,
            json={"action": action})
        assert response.status_code == 409, response.text
    assert store.get_skill(skill_id) is None
    assert store.get_section(section.id) is None


def test_delete_rolls_back_graph_queue_and_publications_on_failure(workspace, monkeypatch):
    skill_id = _import(workspace)
    store, queue = workspace.services.store, workspace.services.queue
    _publish(workspace)
    item = _section_review(workspace, "rollback-source-review", _prose_section(workspace, skill_id))
    before = _detail(workspace, skill_id)
    document_before = _document(workspace, skill_id)
    review_before = asdict(queue.get(item.id))
    publications_before = {p.source_ref: asdict(p) for p in store.publications_for_skill("acme", skill_id)}
    assert publications_before
    real_resolve = type(queue).resolve

    def fail_after_resolving(self, item_id, resolution, decided_by=None):
        real_resolve(self, item_id, resolution, decided_by=decided_by)
        raise RuntimeError("simulated failure after review resolution")

    # Patch the adapter class: Neo4j's atomic operation binds a fresh queue
    # instance to its transaction, while memory uses the original instance.
    monkeypatch.setattr(type(queue), "resolve", fail_after_resolving)
    with pytest.raises(RuntimeError, match="simulated failure"):
        workspace.client.delete(f"/api/skills/{skill_id}", headers=ORIGIN)

    assert _detail(workspace, skill_id) == before
    assert _document(workspace, skill_id) == document_before
    assert asdict(queue.get(item.id)) == review_before
    assert {p.source_ref: asdict(p) for p in store.publications_for_skill("acme", skill_id)} == publications_before


def test_recreating_and_reimporting_cannot_inherit_deleted_document_history(workspace):
    files = _files(name="expense-review")
    skill_id = _import(workspace, files=files)
    document = _document(workspace, skill_id)
    response = _save(workspace, skill_id, document, [(_part(document, INTRO)["anchor"], NEW_INTRO)])
    assert response.status_code == 200, response.text
    detail = _detail(workspace, skill_id)
    rule_id = detail["rules"][0]["id"]
    response = workspace.client.patch(f"/api/rules/{rule_id}", headers=ORIGIN,
        json={"body": "Retain the reviewed receipt wording.", "action": "retract"})
    assert response.status_code == 200, response.text
    retained_rule = asdict(workspace.services.store.get_rule(rule_id))
    old_versions = {version["id"] for version in _detail(workspace, skill_id)["versions"]}
    _delete(workspace, skill_id)

    created = workspace.client.post("/api/skills", headers=ORIGIN, json={
        "name": "expense-review", "description": "A new empty guide.", "domain": "general"})
    assert created.status_code == 201, created.text
    assert created.json()["id"] == skill_id
    fresh = _detail(workspace, skill_id)
    assert fresh["rules"] == [] and fresh["sections"] == [] and fresh["artefacts"] == []
    assert len(fresh["versions"]) == 1
    assert {version["id"] for version in fresh["versions"]}.isdisjoint(old_versions)
    assert INTRO not in fresh["body"] and NEW_INTRO not in fresh["body"]
    _delete(workspace, skill_id)

    assert _import(workspace, files=files) == skill_id
    imported = _detail(workspace, skill_id)
    assert INTRO in imported["body"] and NEW_INTRO not in imported["body"]
    assert {version["id"] for version in imported["versions"]}.isdisjoint(old_versions)
    assert asdict(workspace.services.store.get_rule(rule_id)) == retained_rule
    assert [skill.id for skill in workspace.services.store.skills_for_rule(rule_id)] == [skill_id]
    assert workspace.services.queue.pending("acme") == []


def test_next_publish_prunes_the_last_deleted_skill_without_removing_unowned_files(workspace):
    skill_id = _import(workspace)
    output = _publish(workspace)
    published = output / "skills" / skill_id
    assert (output / ".oms-ownership.json").is_file()
    assert (published / "SKILL.md").is_file()
    unowned = output / "skills" / "hand-written" / "SKILL.md"
    unowned.parent.mkdir()
    unowned.write_text("This directory belongs to the operator.\n", encoding="utf-8")

    _delete(workspace, skill_id)

    assert (published / "SKILL.md").is_file(), "deletion alone must not write to publication destinations"
    assert workspace.services.store.publications_for_skill("acme", skill_id) == []
    assert _publish(workspace) == output
    assert not published.exists()
    assert unowned.read_text() == "This directory belongs to the operator.\n"
    for filename in ("AGENTS.md", "CLAUDE.md"):
        assert "Expense Review" not in (output / filename).read_text()
        assert f"skills/{skill_id}" not in (output / filename).read_text()
    assert workspace.client.get("/api/skills").json() == []


def test_delete_refuses_foreign_tenants_and_browser_origins_without_writes(workspace):
    skill_id = _import(workspace)
    foreign = _seed_skill(workspace, "other-workspace-skill", tenant="other")
    local_before = _detail(workspace, skill_id)
    foreign_before = asdict(workspace.services.store.get_skill(foreign.id))
    for target in (foreign.id, "missing-skill"):
        assert workspace.client.delete(f"/api/skills/{target}", headers=ORIGIN).status_code == 404
    for headers in [{"Origin": "https://untrusted.example"}, {"Origin": "null"},
                    {"Sec-Fetch-Site": "cross-site"}]:
        response = workspace.client.delete(f"/api/skills/{skill_id}", headers=headers)
        assert response.status_code == 403, response.text
    for parameter in ("tenant", "tenant_id"):
        response = workspace.client.delete(f"/api/skills/{skill_id}?{parameter}=other", headers=ORIGIN)
        assert response.status_code == 400, response.text
    preflight = workspace.client.options(f"/api/skills/{skill_id}", headers={
        **ORIGIN, "Access-Control-Request-Method": "DELETE"})
    assert preflight.status_code == 200 and "DELETE" in preflight.headers["Access-Control-Allow-Methods"]
    assert _detail(workspace, skill_id) == local_before
    assert asdict(workspace.services.store.get_skill(foreign.id)) == foreign_before
