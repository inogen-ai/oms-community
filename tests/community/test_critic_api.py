"""Adversarial API checks against the installed Community wheel.

No repository source directory is added to Python's import path here.
"""
from dataclasses import dataclass, replace
from importlib.metadata import distribution
import inspect
from io import BytesIO
import json
from pathlib import Path
import stat
from zipfile import ZipFile, ZipInfo

from fastapi.testclient import TestClient
import pytest

from oms.domain.models import Rule, Skill


API_URL = "http://127.0.0.1:4317"
UI_ORIGIN = "http://127.0.0.1:4318"


@dataclass
class ApiWorld:
    services: object
    settings: object
    client: TestClient
    root: Path


def _construct(root):
    from oms.community.app import build_memory
    from oms.settings.core import CoreSettings
    from oms.web.api import create_app

    services = build_memory(root, tenant="acme")
    settings = CoreSettings(data_dir=root, tenant_id="acme")
    return services, settings, create_app(services, settings=settings)


@pytest.fixture
def api(tmp_path):
    services, settings, app = _construct(tmp_path / "data")
    with TestClient(app, base_url=API_URL) as client:
        yield ApiWorld(services, settings, client, tmp_path)


def _zip_package(*, extra=None, marker=None):
    script = (f"#!/bin/sh\ntouch '{marker}'\n" if marker else "#!/bin/sh\nprintf 'checked\\n'\n").encode()
    files = {
        "expense-review/SKILL.md": b"---\nname: Expense Review\ndescription: Review expense claims carefully.\n---\n\n# Expense Review\n\nKeep clear records for every expense claim.\n\n## Instructions\n\n* Keep original receipts with each claim.\n\n## References\n\nSee [the policy](references/policy.md).\n",
        "expense-review/references/policy.md": b"# Expense policy\n\nRetain the original receipt bytes.\n",
        "expense-review/scripts/check.sh": script,
    }
    files.update(extra or {})
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue(), files


def _import(client, archive):
    return client.post("/api/import", files={"file": ("skills.zip", archive, "application/zip")},
                       headers={"Origin": UI_ORIGIN})


def _contribute(client, transaction_id="api-correction", **extra):
    return client.post("/api/ingest", json={
        "transaction_id": transaction_id, "correction": "Verify expense totals before submitting a claim.",
        **extra,
    }, headers={"Origin": UI_ORIGIN})


def test_api_imports_are_from_the_installed_core_distribution():
    from oms.community.app import build_memory
    actual = Path(inspect.getfile(build_memory)).resolve()
    installed = Path(distribution("oms-core").locate_file("oms/community/app.py")).resolve()
    assert actual == installed
    assert "site-packages" in actual.parts or "dist-packages" in actual.parts


def test_http_import_contribute_review_history_and_publish_preserve_custody(api):
    marker = api.root / "script-must-not-run"
    archive, original_files = _zip_package(marker=marker)
    imported = _import(api.client, archive)
    assert imported.status_code == 200, imported.text
    assert not marker.exists()
    skills = api.client.get("/api/skills").json()
    assert len(skills) == 1
    skill_id = skills[0]["id"]

    accepted = _contribute(api.client, "api-approved", skill_hint=skill_id, source_ref="session:api-critic")
    assert accepted.status_code in (200, 202), accepted.text
    # The whole receipt. `status` is what the bundled helper's command line
    # reads to tell an accepted contribution from a failed one.
    assert accepted.json() == {"transaction_id": "api-approved", "status": "accepted",
                               "state": "awaiting_manual_review"}
    inbox = api.client.get("/api/review").json()
    item, = [row for row in inbox if row["txn_id"] == "api-approved"]
    assert item["source_ref"] == "session:api-critic"
    assert item["signal_type"] == "explicit_correction"
    original_detail = api.client.get(f"/api/skills/{skill_id}").json()

    wording = "Check every expense total against its original receipt before submitting the claim."
    decision = {"action": "create", "body": wording, "skill_ids": [skill_id]}
    approved = api.client.post("/api/review/api-approved/decision", json=decision,
                               headers={"Origin": UI_ORIGIN})
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "applied"
    repeated = api.client.post("/api/review/api-approved/decision", json=decision,
                               headers={"Origin": UI_ORIGIN})
    assert repeated.status_code == 200 and repeated.json() == approved.json()
    assert not any(row["txn_id"] == "api-approved" for row in api.client.get("/api/review").json())

    detail = api.client.get(f"/api/skills/{skill_id}").json()
    assert any(rule["body"] == wording for rule in detail["rules"])
    assert detail["versions"] and detail["versions"] != original_detail.get("versions")
    assert [txn.id for txn in api.services.store.lineage("rule-api-approved")] == ["api-approved"]
    preview = api.client.get("/api/publish/preview", params={"skill_id": skill_id})
    assert preview.status_code == 200 and wording in preview.json()["body"]

    configured = api.client.get("/api/settings").json()
    output = Path(configured["publish_root"])
    assert output.resolve().is_relative_to(api.root.resolve()), "test publication must remain in its temporary workspace"
    published = api.client.post("/api/publish", headers={"Origin": UI_ORIGIN})
    assert published.status_code == 200, published.text
    assert Path(published.json()["output"]).resolve() == output.resolve()
    rendered = list(output.rglob("SKILL.md"))
    assert any(wording in path.read_text(encoding="utf-8") for path in rendered)
    for relative in ("references/policy.md", "scripts/check.sh"):
        copies = list(output.rglob(Path(relative).name))
        assert any(path.read_bytes() == original_files[f"expense-review/{relative}"] for path in copies)
    assert not marker.exists()


PRIVATE_CAPABILITIES = (
    "semantic_compilation", "multi_user_identity", "team_scoping", "personal_mutes",
    "contributor_portal", "redaction_vault", "model_settings", "scheduled_publish",
    "managed_publish", "graph_query_console", "advanced_review", "usage_analytics",
)


def test_capabilities_match_community_routes_and_do_not_disclose_private_settings(api):
    response = api.client.get("/api/capabilities")
    assert response.status_code == 200
    capabilities = response.json()
    assert capabilities["edition"] == "community"
    assert capabilities["api_contract_version"] in ("1.0", "1.1")
    assert capabilities["schema_version"] == 1
    assert capabilities["manual_learning"] is True
    assert all(capabilities[name] is False for name in PRIVATE_CAPABILITIES)
    assert set(capabilities) == {"edition", "api_contract_version", "schema_version", "manual_learning", *PRIVATE_CAPABILITIES}
    schema = api.client.get("/openapi.json").json()
    assert "/api/review" in schema["paths"]
    assert "/api/review/{transaction_id}/decision" in schema["paths"]
    forbidden = ("license", "licence", "model", "token", "enrol", "team", "mute", "people", "vault",
                 "consistency", "compile", "analytics", "proposed-edit", "cypher", "queries",
                 "reflect", "redaction", "pipeline", "usage", "activity", "admin", "overlay")
    assert not any(any(word in path.lower() for word in forbidden) for path in schema["paths"])


@pytest.mark.parametrize("key,value", [("manual_learning", False), ("semantic_compilation", True), ("edition", "enterprise")])
def test_advertised_capability_disagreement_refuses_startup(api, key, value):
    from oms.web.api import create_app
    advertised = api.client.get("/api/capabilities").json()
    advertised[key] = value
    with pytest.raises(ValueError):
        create_app(api.services, settings=api.settings, advertised_capabilities=advertised)


def test_protected_route_cannot_hide_behind_a_false_capability(api):
    from fastapi import APIRouter
    from oms.web.api import create_app
    from oms.web.capabilities import RouteBundle
    router = APIRouter()

    @router.post("/api/settings/models")
    def unadvertised_model_settings():
        return {"should_never_be_mounted": True}

    bundle = RouteBundle("undeclared-protected-route", router)
    with pytest.raises(ValueError):
        create_app(api.services, settings=api.settings, route_bundles=(bundle,))


def test_protected_capability_cannot_be_enabled_in_a_community_composition(api):
    from fastapi import APIRouter
    from oms.web.api import create_app
    from oms.web.capabilities import RouteBundle
    router = APIRouter()

    @router.post("/api/compile")
    def paid_compilation():
        return {"should_never_be_mounted": True}

    bundle = RouteBundle("semantic", router, capabilities=frozenset({"semantic_compilation"}))
    with pytest.raises(ValueError):
        create_app(api.services, settings=api.settings, route_bundles=(bundle,))


@pytest.mark.parametrize("path", [
    "/api/settings/models", "/api/settings/test-model", "/api/people", "/api/teams",
    "/api/mutes", "/api/redaction-vault", "/api/consistency", "/api/compile",
    "/api/licence", "/api/license", "/api/graph/query", "/api/usage", "/api/contributor",
])
def test_private_http_routes_are_absent(api, path):
    response = api.client.post(path, json={}, headers={"Origin": UI_ORIGIN})
    assert response.status_code == 404


@pytest.mark.parametrize("origin", [
    "null", "https://attacker.invalid", "http://127.0.0.1.attacker.invalid:4318",
    "http://localhost.attacker.invalid:4318", "http://127.0.0.1:43180",
    "http://127.0.0.1:4318/", "https://127.0.0.1:4318", "http://127.0.0.1:4318,null",
])
def test_untrusted_browser_origins_cannot_mutate_the_local_workspace(api, origin):
    response = api.client.post("/api/ingest", json={"transaction_id": "untrusted-origin", "correction": "Keep receipts."},
                               headers={"Origin": origin})
    assert response.status_code == 403
    assert api.services.store.get_transaction("untrusted-origin") is None


@pytest.mark.parametrize("origin", ["http://127.0.0.1:4318", "http://localhost:4318", "http://127.0.0.1:4317"])
def test_exact_configured_browser_origins_can_contribute(api, origin):
    response = api.client.post("/api/ingest", json={"transaction_id": "allowed-origin", "correction": "Keep receipts."},
                               headers={"Origin": origin})
    assert response.status_code in (200, 202), response.text


def test_no_origin_command_line_contribution_is_allowed(api):
    response = api.client.post("/api/ingest", json={"transaction_id": "cli-input", "correction": "Keep receipts."})
    assert response.status_code in (200, 202), response.text
    assert api.services.store.get_transaction("cli-input") is not None


def test_no_origin_http_mcp_uses_the_same_manual_disposition(api):
    response = api.client.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "log_correction", "arguments": {
                "transaction_id": "mcp-to-manual", "correction": "Keep receipts with expense claims.",
            },
        },
    }, follow_redirects=False)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError"), result
    acknowledgement = json.loads(result["content"][0]["text"])
    assert acknowledgement["transaction_id"] == "mcp-to-manual"
    row, = [item for item in api.client.get("/api/review").json() if item["txn_id"] == "mcp-to-manual"]
    assert row["state"] == "awaiting_manual_review"
    assert api.services.store.get_transaction("mcp-to-manual").source_runtime.value == "mcp"


def test_http_mcp_cannot_bypass_browser_origin_protection(api):
    response = api.client.post("/mcp", headers={"Accept": "application/json, text/event-stream", "Origin": "null"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "log_correction", "arguments": {
                "transaction_id": "mcp-origin-bypass", "correction": "Keep receipts with expense claims.",
            },
        },
    })
    assert response.status_code == 403
    assert api.services.store.get_transaction("mcp-origin-bypass") is None


@pytest.mark.parametrize("origin", [None, UI_ORIGIN])
def test_cross_site_browser_mutation_is_refused(api, origin):
    headers = {"Sec-Fetch-Site": "cross-site"}
    if origin:
        headers["Origin"] = origin
    response = api.client.post("/api/ingest", json={"transaction_id": "cross-site", "correction": "Keep receipts."}, headers=headers)
    assert response.status_code == 403
    assert api.services.store.get_transaction("cross-site") is None


@pytest.mark.parametrize("method,path", [("GET", "/api/settings"), ("POST", "/api/ingest")])
@pytest.mark.parametrize("host", ["attacker.invalid", "127.0.0.1.attacker.invalid:4317", "localhost.attacker.invalid"])
def test_untrusted_host_is_not_rescued_by_a_trusted_origin_or_forwarded_host(api, method, path, host):
    response = api.client.request(method, path,
        headers={"Host": host, "Origin": UI_ORIGIN, "X-Forwarded-Host": "127.0.0.1:4317"},
        **({"json": {"correction": "Keep receipts."}} if method == "POST" else {}))
    assert response.status_code in (400, 403)


@pytest.mark.parametrize("host", ["[", "[::1", "attacker@127.0.0.1:4317", "127.0.0.1:4317/other"])
def test_malformed_host_is_a_client_refusal_and_not_a_server_error(api, host):
    response = api.client.get("/api/health", headers={"Host": host})
    assert response.status_code in (400, 403)


def test_default_settings_keep_local_binding_and_original_workspace_id():
    from oms.settings.core import CoreSettings
    settings = CoreSettings().validate()
    assert settings.host == "127.0.0.1" and settings.port == 4317
    assert settings.tenant_id == "acme"
    assert "*" not in settings.allowed_hosts and "*" not in settings.allowed_origins


@pytest.mark.parametrize("overrides", [
    {"host": "0.0.0.0"}, {"host": "::"},
    {"allowed_origins": ("https://ui.example",)}, {"allowed_origins": ("*",)},
    {"allowed_hosts": ("*",)}, {"allowed_hosts": ("public.example",)},
])
def test_exposing_local_identity_requires_explicit_network_acknowledgement(overrides):
    from oms.settings.core import CoreSettings
    with pytest.raises(ValueError):
        CoreSettings(**overrides).validate()


def test_dev_environment_flags_do_not_disable_browser_protection(tmp_path, monkeypatch):
    monkeypatch.setenv("OMS_DEV_NO_AUTH", "1")
    monkeypatch.setenv("OMS_ENV", "dev")
    services, _settings, app = _construct(tmp_path / "data")
    with TestClient(app, base_url=API_URL) as client:
        response = client.post("/api/ingest", json={"transaction_id": "dev-bypass", "correction": "Keep receipts."},
                               headers={"Origin": "https://attacker.invalid"})
    assert response.status_code == 403
    assert services.store.get_transaction("dev-bypass") is None


@pytest.mark.parametrize("path", ["/api/skills", "/api/review", "/api/rules", "/api/constraints", "/api/graph", "/api/settings"])
def test_other_workspace_query_is_explicitly_refused(api, path):
    response = api.client.get(path, params={"tenant_id": "other"})
    assert response.status_code in (400, 403, 422)


def test_other_workspace_contribution_is_refused_without_persistence(api):
    response = _contribute(api.client, "foreign-tenant", tenant_id="other")
    assert response.status_code in (400, 403, 422)
    assert api.services.store.get_transaction("foreign-tenant") is None


def test_http_safety_hold_does_not_publish_before_separate_release_and_approval(api):
    created = api.client.post("/api/skills", json={"name": "Expenses", "domain": "finance"}, headers={"Origin": UI_ORIGIN})
    assert created.status_code in (200, 201), created.text
    skill_id = created.json()["id"]
    held = _contribute(api.client, "api-held", correction="Never share records with alice@example.com.")
    assert held.status_code in (200, 202), held.text
    assert held.json()["state"] == "held_safety"
    review = api.client.get("/api/review")
    assert "alice@example.com" not in review.text
    decision = {"action": "create", "body": "Protect all expense records.", "skill_ids": [skill_id]}
    refused = api.client.post("/api/review/api-held/decision", json=decision, headers={"Origin": UI_ORIGIN})
    assert refused.status_code in (400, 409, 422)
    assert api.services.store.get_rule("rule-api-held") is None
    released = api.client.post("/api/review/api-held/decision", json={**decision, "action": "release_safety"},
                               headers={"Origin": UI_ORIGIN})
    assert released.status_code == 200 and released.json()["state"] == "awaiting_manual_review"
    assert api.services.store.get_rule("rule-api-held") is None
    approved = api.client.post("/api/review/api-held/decision", json=decision, headers={"Origin": UI_ORIGIN})
    assert approved.status_code == 200 and approved.json()["state"] == "applied"


def test_http_rejection_remains_terminal_when_contribution_is_retried(api):
    assert _contribute(api.client, "api-rejected").status_code in (200, 202)
    decision = {"action": "reject", "body": "", "skill_ids": []}
    rejected = api.client.post("/api/review/api-rejected/decision", json=decision, headers={"Origin": UI_ORIGIN})
    assert rejected.status_code == 200 and rejected.json()["state"] == "rejected"
    repeated = _contribute(api.client, "api-rejected")
    assert repeated.status_code in (200, 202) and repeated.json()["state"] == "rejected"
    assert api.services.store.get_rule("rule-api-rejected") is None
    assert "api-rejected" not in api.client.get("/api/review").text


def test_foreign_skill_is_absent_from_detail_preview_graph_and_mutation(api):
    api.services.store.upsert_skill(Skill(id="private-skill", name="Private Skill", description="Foreign confidential description",
                                         domain="other", tenant_id="other"))
    api.services.store.upsert_rule(Rule(id="private-rule", body="Foreign confidential rule", tenant_id="other"))
    for path in ("/api/skills/private-skill", "/api/graph/nodes/private-skill", "/api/publish/preview?skill_id=private-skill"):
        response = api.client.get(path)
        assert response.status_code == 404
        assert "Foreign confidential" not in response.text
    response = api.client.patch("/api/skills/private-skill", json={"name": "Changed"}, headers={"Origin": UI_ORIGIN})
    assert response.status_code == 404
    assert api.services.store.get_skill("private-skill").name == "Private Skill"
    assert "private-skill" not in api.client.get("/api/skills").text
    assert "private-rule" not in api.client.get("/api/rules").text
    assert "Foreign confidential" not in api.client.get("/api/graph").text


@pytest.mark.parametrize("key", ["llm_model", "openai_api_key", "bundle_token", "redaction_key", "admin_token", "team_scoping", "tenant_id"])
def test_private_or_identity_settings_cannot_be_written_through_core_settings(api, key):
    response = api.client.patch("/api/settings", json={key: "must-not-be-stored"}, headers={"Origin": UI_ORIGIN})
    assert response.status_code in (400, 403, 422)
    assert "must-not-be-stored" not in api.client.get("/api/settings").text


@pytest.mark.parametrize("missing", [
    "store", "queue", "repository", "coordinator", "manual", "importer", "uploads", "skills",
    "history", "publisher", "catalogue", "settings_store", "principal_resolver", "notifier", "retention",
])
def test_missing_required_service_refuses_startup(tmp_path, missing):
    from oms.community.app import build_memory
    from oms.web.api import create_app
    services = build_memory(tmp_path / "data", tenant="acme")
    with pytest.raises((ValueError, TypeError)):
        broken = replace(services, **{missing: None})
        with TestClient(create_app(broken, settings=services.settings), base_url=API_URL):
            pass


@pytest.mark.parametrize("missing", ["coordinator", "manual", "publisher", "principal_resolver"])
def test_object_without_required_port_methods_refuses_startup(tmp_path, missing):
    from oms.community.app import build_memory
    from oms.web.api import create_app
    services = build_memory(tmp_path / "data", tenant="acme")
    with pytest.raises((ValueError, TypeError)):
        broken = replace(services, **{missing: object()})
        with TestClient(create_app(broken, settings=services.settings), base_url=API_URL):
            pass


def test_zip_traversal_is_rejected_without_writing_outside_import_root(api):
    archive, _files = _zip_package(extra={"../../must-not-escape.txt": b"Outside"})
    response = _import(api.client, archive)
    assert response.status_code in (400, 422)
    assert not list(api.root.rglob("must-not-escape.txt"))


def test_zip_symlinks_are_rejected(api):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        link = ZipInfo("expense-review/references/outside")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../../../outside")
    response = _import(api.client, buffer.getvalue())
    assert response.status_code in (400, 422)
