"""Installed-core compatibility for enabled source composition and local intake."""
from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest

from oms.community.app import _compose, build_community
from oms.domain.identity import SkillRef
from oms.settings.core import CoreSettings
from oms.sources.mutation import source_repository
from oms.web.api import create_app
from tests.community.test_critic_workflow import critic_neo4j_driver  # noqa: F401


@pytest.fixture(params=["memory", pytest.param("neo4j", marks=pytest.mark.integration)])
def source_application(request, tmp_path):
    settings = CoreSettings(data_dir=tmp_path, tenant_id="acme", github_skill_sources=True)
    if request.param == "neo4j":
        driver = request.getfixturevalue("critic_neo4j_driver")
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()
        services = build_community(settings, driver)
    else:
        from oms.adapters.memory.review_queue import InMemoryReviewQueue
        from oms.adapters.memory.settings import InMemorySettingsStore
        from oms.adapters.memory.store import InMemoryGraphStore
        from oms.community.repository import MemoryWorkflowRepository
        store, queue = InMemoryGraphStore(), InMemoryReviewQueue()
        services = _compose(store, queue, MemoryWorkflowRepository(store, queue), InMemorySettingsStore(), settings)
    with TestClient(create_app(services, settings=settings), base_url="http://localhost:4317") as client:
        yield services, client


def test_enabled_source_intake_commits_one_receipt_history_and_generation(source_application, monkeypatch):
    services, client = source_application
    def unavailable(*args, **kwargs):
        raise AssertionError("Source intake called ordinary import processing")
    monkeypatch.setattr(services.importer, "prepare_directory", unavailable)
    capabilities = client.get("/api/capabilities").json()
    assert capabilities["github_skill_sources"] is True
    assert capabilities["schema_version"] == 2
    assert capabilities["api_contract_version"] == "1.2"
    workspace = client.get("/api/skill-source-workspace").json()["workspace_id"]
    content = BytesIO()
    with ZipFile(content, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: Expenses\ndescription: Record submitted expenses\n---\n\n## Guidance\n\nKeep original receipts.\n")
        archive.writestr("references/receipt.txt", b"Receipt reference\n")
    staged = client.post("/api/import", files={"file": ("expenses.zip", content.getvalue(), "application/zip")})
    assert staged.status_code == 200, staged.text
    assert services.store.get_skill("expenses", tenant_id="acme") is None
    body = {"upload_id": staged.json()["upload_id"], "selections": [
        {"package_path": "", "local_name": "Expenses", "domain": "finance"}]}
    headers = {"Idempotency-Key": "local-installed-import", "X-Source-Workspace": workspace}
    first = client.post("/api/skill-local-imports", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "complete" and first.json()["committed"] is True
    skill = SkillRef("acme", "expenses")
    sources = source_repository(services.store)
    before = sources.get_generations(skill)
    versions = services.store.skill_versions("acme", "expenses")
    assert before.content > 0 and len(versions) == 1
    assert versions[0].files_json and versions[0].source_operation_id
    assert sources.get_local_stream(skill).baseline is not None
    assert sources.get_binding(skill) is None
    repeated = client.post("/api/skill-local-imports", json=body, headers=headers)
    assert repeated.json() == first.json()
    assert sources.get_generations(skill) == before
    assert services.store.skill_versions("acme", "expenses") == versions
    assert client.get("/api/skill-sources?tenant_id=another").status_code == 400


def test_source_mutation_refuses_an_unverified_workspace_before_reserving(source_application):
    services, client = source_application
    response = client.post("/api/skill-source-discoveries", json={"url": "https://github.com/example/skills"},
        headers={"Idempotency-Key": "wrong-workspace", "X-Source-Workspace": "different-workspace"})
    assert response.status_code == 409
    assert response.json()["code"] == "workspace_changed"
    lookup = client.get("/api/skill-source-operations", params={"request_key": "wrong-workspace"})
    assert lookup.status_code == 404


@pytest.mark.integration
def test_source_identity_upgrade_requires_explicit_maintenance_and_preserves_visible_ids(critic_neo4j_driver):
    from oms.adapters.neo4j.store import Neo4jGraphStore
    from oms.domain.models import Skill
    from oms.schema.metadata import SchemaCompatibilityError, SchemaManager
    driver = critic_neo4j_driver
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
        session.run("CREATE (:OMSMetadata {id:'core',edition:'community',managed_edition:'community',"
            "core_version:1,schema_version:1,private_version:0,minimum_core_version:'1.4.0'})").consume()
        session.run("CREATE (:Skill:Entity {id:'expenses',tenant_id:'acme',name:'Expenses',"
            "description:'Original custody',domain:'finance'})").consume()
    manager = SchemaManager(driver)
    with pytest.raises(SchemaCompatibilityError, match="maintenance"):
        manager.initialise()
    with pytest.raises(SchemaCompatibilityError, match="stopped"):
        manager.migrate_source_identity(old_writers_stopped=False)
    manager.migrate_source_identity(old_writers_stopped=True)
    store = Neo4jGraphStore(driver)
    store.ensure_schema()
    assert store.get_skill("expenses", tenant_id="acme").description == "Original custody"
    store.upsert_skill(Skill(id="expenses", tenant_id="other", name="Other expenses", description="Independent custody", domain="finance"))
    assert store.get_skill("expenses", tenant_id="acme").description == "Original custody"
    assert store.get_skill("expenses", tenant_id="other").description == "Independent custody"
    with driver.session() as session:
        rows = list(session.run("MATCH (s:Skill {id:'expenses'}) RETURN s.storage_key AS key"))
        metadata = dict(session.run("MATCH (m:OMSMetadata {id:'core'}) RETURN m").single()["m"])
    assert {row["key"] for row in rows} == {SkillRef(tenant, "expenses").storage_key for tenant in ("acme", "other")}
    with pytest.raises(SchemaCompatibilityError):
        manager._check(metadata, "community", 1)
