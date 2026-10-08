from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

from oms.sources.api import source_routes
from oms.sources.reads import SourceReads


@pytest.fixture
def source_api(source_world):
    world = source_world
    app = FastAPI()
    state = {"context": world.context, "authenticated": True}

    def resolve(request, action):
        if not state["authenticated"]:
            raise HTTPException(401, "authentication required")
        return state["context"]
    reads = SourceReads(world.service, world.blobs)
    app.include_router(source_routes(world.service, reads, resolve).router)
    return TestClient(app), world, state


def installation():
    return {"discovery_id": "discovery", "selections": [{"package_path": "", "local_name": "expenses", "domain": "finance"}]}


def test_history_pages_all_retained_versions_with_equal_timestamps(source_api):
    from datetime import timedelta
    from oms.domain.models import SkillVersion
    from oms.domain.types import SkillVersionCause
    client, world, _ = source_api
    world.install()
    expected = {row.id for row in world.store.skill_versions("acme", "expenses")}
    at = world.store.latest_skill_version("acme", "expenses").at + timedelta(seconds=1)
    for number in range(125):
        version = SkillVersion(id=f"version-{number:03}", skill_id="expenses", tenant_id="acme",
            at=at, revision=str(number), cause=SkillVersionCause.CONSOLE_EDIT)
        world.store.append_skill_version(version)
        expected.add(version.id)
    world.store.append_skill_version(SkillVersion(id="other-tenant", skill_id="expenses", tenant_id="other",
        at=world.now, revision="other", cause=SkillVersionCause.CONSOLE_EDIT))
    ids, cursor = [], None
    for _ in range(9):
        response = client.get("/api/skills/expenses/source-history", params={
            "limit": 17, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        rows = response.json()
        ids.extend(row["id"] for row in rows["items"])
        cursor = rows["next_cursor"]
        if cursor is None:
            break
    assert len(ids) == len(expected)
    assert set(ids) == expected
    assert cursor is None
    assert ids[:2] == ["version-124", "version-123"]
    invalid = client.get("/api/skills/expenses/source-history", params={"cursor": "invalid"})
    assert invalid.status_code == 422


def test_lost_first_response_replays_exact_outcome_without_duplicate_content(source_api):
    client, world, state = source_api
    first = client.post("/api/skill-source-installations", json=installation(), headers={"Idempotency-Key": "http-install"})
    assert first.status_code == 200, first.text
    assert first.json()["committed"] is True
    assert first.json()["outcomes"][0]["skill_id"] == "expenses"
    before = world.snapshot()
    retry = client.post("/api/skill-source-installations", json=installation(), headers={"Idempotency-Key": "http-install"})
    assert retry.json() == first.json()
    assert world.snapshot() == before
    reads = world.reader.reads
    for _ in range(2):
        polled = client.get("/api/skill-source-operations/" + first.json()["operation_id"])
        assert polled.json() == first.json()
    assert world.reader.reads == reads
    receipt = client.get("/api/skill-source-operations", params={"request_key": "http-install"})
    assert receipt.json() == first.json()
    assert world.reader.reads == reads
    changed = installation()
    changed["selections"][0]["domain"] = "engineering"
    refused = client.post("/api/skill-source-installations", json=changed, headers={"Idempotency-Key": "http-install"})
    assert refused.status_code == 409
    assert refused.json()["operation_id"] == first.json()["operation_id"]


def test_http_apply_and_stale_retry_keep_explicit_operation_outcomes(source_api):
    client, world, _ = source_api
    candidate = world.open_update()
    update = candidate.update
    body = {"skill_id": "expenses", "fingerprint": update.plan.fingerprint.model_dump(mode="json")}
    world.edit_description("Newer editor change")
    response = client.post(f"/api/skill-updates/{update.update_id}/apply", json=body,
                           headers={"Idempotency-Key": "http-apply"})
    assert response.status_code == 409, response.text
    assert world.description() == "Newer editor change"
    operation = client.get("/api/skill-source-operations/" + response.json()["operation_id"])
    assert operation.json()["state"] == "failed"
    assert operation.json()["committed"] is False


def test_card_detail_supplies_bound_choices_and_marks_newer_local_content_stale(source_api):
    client, world, _ = source_api
    candidate = world.open_update()
    route = "/api/skill-updates/" + candidate.update.update_id
    response = client.get(route)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["stale"] is False
    assert detail["resolved_check"]["state"] == "passed"
    assert set(detail["part_fingerprints"]) == {row.part_id for row in candidate.update.plan.changes}
    assert all(len(value) == 64 for value in detail["part_fingerprints"].values())
    assert set(detail["ownership"].values()) <= {"source", "shared", "local", "unknown"}
    world.edit_description("Unreviewed newer local change")
    stale = client.get(route).json()
    assert stale["stale"] is True
    assert stale["resolved_check"]["state"] == "unavailable"
    assert set(stale["ownership"].values()) == {"unknown"}


def test_source_reads_filter_scope_before_pagination_and_hide_foreign_ids(source_api):
    client, world, state = source_api
    candidate = world.open_update()
    source = world.sources.get_binding(world.skill).source_id
    assert len(client.get("/api/skill-sources?limit=1").json()["items"]) == 1
    admit = world.policy.admit

    def deny_selected(action, context, skills):
        admit(action, context, skills)
        if action == "read" and skills:
            raise PermissionError("scope revoked")
    world.policy.admit = deny_selected
    listing = client.get("/api/skill-sources?limit=1").json()
    assert listing["items"] == [] and listing["next_cursor"] is None
    assert client.get("/api/skill-sources/" + source).status_code == 404
    assert client.get("/api/skill-updates/" + candidate.update.update_id).status_code == 404
    assert client.get("/api/skills/expenses/source-history").status_code == 404
    world.policy.admit = admit
    state["context"] = world.context.model_copy(update={"tenant_id": "other"})
    assert client.get("/api/skill-updates/" + candidate.update.update_id).status_code == 404
    assert client.get("/api/skill-source-discoveries/discovery").status_code == 404


def test_bound_file_routes_preserve_bytes_and_refuse_arbitrary_or_changed_local_state(source_api):
    client, world, _ = source_api
    update = world.open_update().update
    route = f"/api/skill-updates/{update.update_id}/files"
    response = client.get(route, params={"side": "upstream", "path": "SKILL.md", "preview": "false"})
    assert response.status_code == 200, response.text
    assert response.content == world.blobs.get(world.incoming.manifest[0].blob_ref)
    assert response.headers["content-type"] == "application/octet-stream"
    assert client.get(route, params={"side": "base", "path": "../secret"}).status_code == 404
    assert client.get(route, params={"side": "upstream", "path": "missing"}).status_code == 404
    world.edit_description("Concurrent editor")
    assert client.get(route, params={"side": "local", "path": "SKILL.md"}).status_code == 409


def test_http_rejects_authority_fields_and_preserves_authentication_status(source_api):
    client, world, state = source_api
    body = installation() | {"tenant_id": "other", "actor_id": "admin"}
    assert client.post("/api/skill-source-installations", json=body,
                       headers={"Idempotency-Key": "invalid"}).status_code == 422
    assert client.post("/api/skill-source-installations", json=installation()).status_code == 422
    assert client.get("/api/skill-sources?limit=101").status_code == 422
    state["authenticated"] = False
    assert client.get("/api/skill-sources").status_code == 401


def test_check_retry_delay_is_preserved_in_json_and_header(source_api):
    client, world, _ = source_api
    world.install()
    binding = world.sources.get_binding(world.skill)
    source = world.sources.get_source(binding.source_id, tenant_id="acme")
    body = {"expected_source_generation": source.generation, "skill_ids": ["expenses"]}
    route = f"/api/skill-sources/{source.source_id}/check"
    first = client.post(route, json=body, headers={"Idempotency-Key": "http-check"})
    assert first.status_code == 200, first.text
    second = client.post(route, json=body, headers={"Idempotency-Key": "http-retry"})
    assert second.status_code == 429, second.text
    assert second.json()["retry_after_seconds"] == int(second.headers["retry-after"]) == 60


def test_recreated_visible_skill_id_does_not_expose_retired_source_card_or_binding(source_api):
    from oms.skills.service import SkillAdminService
    client, world, _ = source_api
    candidate = world.open_update()
    admin = SkillAdminService(store=world.store, repository=world.repository)
    admin.delete_skill("expenses", "acme", actor="reviewer")
    admin.create_skill("acme", name="expenses", description="New unrelated skill", domain="engineering", actor="reviewer")
    assert client.get("/api/skill-updates/" + candidate.update.update_id).status_code == 404
    assert client.get("/api/skills/expenses/source-binding").json()["binding"] is None


def test_workspace_identity_is_verified_stable_and_separate_from_tenant_label(source_api):
    client, world, state = source_api
    response = client.get("/api/skill-source-workspace")
    assert response.status_code == 200
    first = response.json()["workspace_id"]
    assert first != world.context.tenant_id
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/skill-source-workspace").json()["workspace_id"] == first
    state["context"] = world.context.model_copy(update={"tenant_id": "other"})
    assert client.get("/api/skill-source-workspace").json()["workspace_id"] != first


def test_old_workspace_guard_refuses_reads_and_mutations_before_reserving_work(source_api):
    client, world, _ = source_api
    response = client.post("/api/skill-source-installations", json=installation(),
        headers={"Idempotency-Key": "old-workspace-install", "X-Source-Workspace": "retired-workspace"})
    assert response.status_code == 409 and response.json()["code"] == "workspace_changed"
    assert world.store.get_skill("expenses", tenant_id="acme") is None
    assert world.sources.operation_for_key("old-workspace-install", tenant_id="acme", actor_id="reviewer") is None
    assert client.get("/api/skill-sources", headers={"X-Source-Workspace": "retired-workspace"}).status_code == 409
    assert client.get("/api/skill-source-workspace", headers={"X-Source-Workspace": "retired-workspace"}).status_code == 200


def test_throttled_check_has_no_phantom_receipt_and_same_key_can_retry_after_delay(source_api):
    from datetime import timedelta
    client, world, _ = source_api
    world.install()
    source_id = world.sources.get_binding(world.skill).source_id
    path = f'/api/skill-sources/{source_id}/check'
    body = {'expected_source_generation': 0, 'skill_ids': ['expenses']}
    body['expected_source_generation'] = world.sources.get_source(source_id, tenant_id='acme').generation
    first = client.post(path, json=body, headers={'Idempotency-Key': 'first-check'})
    assert first.status_code == 200, first.text
    refused = client.post(path, json=body, headers={'Idempotency-Key': 'retry-check'})
    assert refused.status_code == 429, refused.text
    assert refused.json()['code'] == 'check_throttled'
    assert refused.json()['retry_after_seconds'] == 60
    assert refused.json()['operation_id'] is None
    assert world.sources.operation_for_key('retry-check', tenant_id='acme', actor_id='reviewer') is None
    world.now += timedelta(seconds=61)
    retried = client.post(path, json=body, headers={'Idempotency-Key': 'retry-check'})
    assert retried.status_code == 200, retried.text
    assert client.get('/api/skill-source-operations/' + retried.json()['operation_id']).json() == retried.json()


def test_error_receipt_lookup_uncertainty_preserves_recovery_identity(source_api, monkeypatch):
    client, world, _ = source_api

    def unavailable(*args, **kwargs):
        raise OSError('storage unavailable')
    monkeypatch.setattr(world.service, 'install', unavailable)
    monkeypatch.setattr(world.service.repository, 'metadata', unavailable)
    response = client.post('/api/skill-source-installations', json=installation(), headers={'Idempotency-Key': 'uncertain'})
    assert response.status_code == 500
    assert response.json()['operation_id'].startswith('source-operation-')


def test_revoked_operation_admission_does_not_erase_existing_error_receipt(source_api, monkeypatch):
    from oms.sources.errors import SourceNotFound
    client, world, _ = source_api
    first = client.post('/api/skill-source-installations', json=installation(), headers={'Idempotency-Key': 'existing'})
    assert first.status_code == 200

    def hidden(*args, **kwargs):
        raise SourceNotFound('operation_not_found')

    def refused(*args, **kwargs):
        raise PermissionError('revoked')
    monkeypatch.setattr(world.service, 'get_operation', hidden)
    monkeypatch.setattr(world.service, 'install', refused)
    response = client.post('/api/skill-source-installations', json=installation(), headers={'Idempotency-Key': 'existing'})
    assert response.status_code == 403
    assert response.json()['operation_id'] == first.json()['operation_id']
