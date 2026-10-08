"""The MCP tools validate their arguments once, with the models the HTTP door uses.

The SDK can check each call against the advertised input schema before any
handler runs. `build_server` turns that check off, because its answer for the
same value differed from the HTTP door's: it named no field, repeated the
whole value back, and counted whitespace the models strip, so a padded value
within its limit over HTTP was refused over MCP. These tests pin the uniform
behaviour. A refused argument is a typed `invalid_payload` error that names
the field and never repeats the value; a value that fits once stripped is
accepted on both transports; each read tool refuses a wrongly typed argument
with its typed error; a valid call answers exactly as before; and the
advertised schemas keep every limit.
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient
from mcp.server import Server
import mcp.types as types
import pytest

from oms.community.app import CoreServices, build_memory
from oms.domain.models import Edge, Rule, Skill
from oms.domain.types import EdgeType
from oms.ingestion.schema import (
    LEARNING_EVIDENCE_MAX, PROJECT_NAME_MAX, REUSE_CASE_MAX, SESSION_SUMMARY_MAX,
)
from oms.mcp_server.server import build_server
from oms.web.api import create_app


LIMITS = {"session_summary": SESSION_SUMMARY_MAX, "project_name": PROJECT_NAME_MAX,
          "reuse_case": REUSE_CASE_MAX, "learning_evidence": LEARNING_EVIDENCE_MAX}
RULE = "In this repo, build the wheel before tagging."
# Distinctive, so a test can tell whether any part of a refused value came
# back in the error.
MARKER = "zq7-"


def _over(limit: int) -> str:
    """A value one character over `limit`, built from the marker."""
    return (MARKER * limit)[: limit + 1]


def _thin(transaction_id: str, **context: str) -> dict:
    """A session learning as the thin body: `log_correction` or `POST /api/ingest`."""
    return {"transaction_id": transaction_id, "signal_type": "self_reflection",
            "learning": RULE, **context}


def _envelope(transaction_id: str, **context: str) -> dict:
    """A session learning as the full envelope `log_signal` takes."""
    return {"transaction_id": transaction_id, "timestamp": "2026-09-29T10:00:00Z",
            "source_agent_id": "agent", "source_runtime": "mcp", "tenant_id": "acme",
            "signal_type": "self_reflection",
            "execution_context": {"user_input": "", "agent_raw_output": "",
                                  "learning": RULE, **context}}


@pytest.fixture
def services(tmp_path: Path) -> CoreServices:
    """One Community workspace holding a skill, so every read tool has something to find."""
    built = build_memory(tmp_path / "workspace", tenant="acme")
    built.store.upsert_skill(Skill(id="checks", name="Checks", description="Review the publication.",
                                   domain="engineering", tenant_id="acme"))
    built.store.upsert_rule(Rule(id="check-rule", body="Check the result before publishing.",
                                 tenant_id="acme"))
    built.store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id="check-rule", to_id="checks"), tenant_id="acme")
    return built


@pytest.fixture
def server(services: CoreServices) -> Server:
    return build_server(services.coordinator, resolve_principal=services.principal_resolver.resolve,
                        catalogue=services.catalogue)


async def _call(server: Server, name: str, arguments: dict) -> types.CallToolResult:
    """One tool call through the SDK's own request handler, as a client makes it."""
    handler = server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=name, arguments=arguments))
    return (await handler(request)).root


@pytest.mark.parametrize("field", LIMITS)
@pytest.mark.parametrize("tool", ["log_correction", "log_signal"])
async def test_an_over_limit_context_field_is_a_typed_error_that_names_it(
        server: Server, services: CoreServices, tool: str, field: str) -> None:
    transaction_id = f"over-{tool}-{field}"
    build = _thin if tool == "log_correction" else _envelope

    result = await _call(server, tool, build(transaction_id, **{field: _over(LIMITS[field])}))

    assert result.isError is True
    (block,) = result.content
    error = json.loads(block.text)
    assert set(error) == {"error", "code"} and error["code"] == "invalid_payload"
    location = field if tool == "log_correction" else f"execution_context.{field}"
    assert error["error"].startswith(f"invalid payload: {location}: ")
    assert f"at most {LIMITS[field]} characters" in error["error"]
    # Not the value, and not a fragment of it.
    assert MARKER not in block.text
    assert services.store.get_transaction(transaction_id) is None


async def test_a_value_that_fits_once_stripped_is_accepted_on_both_transports(
        server: Server, services: CoreServices) -> None:
    """Whitespace around a value is not counted, over MCP exactly as over HTTP."""
    padded = {field: f"  {'x' * limit}\n\t " for field, limit in LIMITS.items()}
    kept = {field: "x" * limit for field, limit in LIMITS.items()}
    with TestClient(create_app(services), base_url="http://127.0.0.1:4317") as client:
        answer = client.post("/api/ingest", json=_thin("padded-http", **padded))
    assert answer.status_code == 202, answer.text

    for tool, arguments in (("log_correction", _thin("padded-thin", **padded)),
                            ("log_signal", _envelope("padded-envelope", **padded))):
        result = await _call(server, tool, arguments)
        assert result.isError is False, result.content[0].text
        assert json.loads(result.content[0].text)["status"] == "accepted"

    for transaction_id in ("padded-http", "padded-thin", "padded-envelope"):
        stored = services.store.get_transaction(transaction_id)
        assert {field: getattr(stored, field) for field in LIMITS} == kept, transaction_id


async def test_a_malformed_transaction_id_is_refused_without_repeating_it(
        server: Server, services: CoreServices) -> None:
    """The thin body leaves the id's form to the envelope it builds, and that
    refusal is typed and names the field like every other."""
    result = await _call(server, "log_correction", {"correction": "Keep receipts for expense claims.",
                                                    "transaction_id": f"{MARKER} has spaces"})

    assert result.isError is True
    error = json.loads(result.content[0].text)
    assert error["code"] == "invalid_payload"
    assert error["error"].startswith("invalid payload: transaction_id: ")
    assert MARKER not in result.content[0].text
    assert services.repository.transactions("acme") == []


@pytest.mark.parametrize(("tool", "arguments", "field"), [
    pytest.param("list_skills", {"domain_hint": 5}, "domain_hint", id="list-number"),
    pytest.param("list_skills", {"domain_hint": ["engineering"]}, "domain_hint", id="list-array"),
    pytest.param("list_skills", {"domain_hint": None}, "domain_hint", id="list-null"),
    pytest.param("query_skill", {"name": 5}, "name", id="skill-number"),
    pytest.param("query_skill", {"name": {"id": "checks"}}, "name", id="skill-object"),
    pytest.param("query_skill_resource", {"skill": "checks", "resource": 5}, "resource",
                 id="resource-number"),
    pytest.param("query_skill_resource", {"skill": ["checks"], "resource": "references.md"},
                 "skill", id="resource-skill-array"),
])
async def test_each_read_tool_refuses_a_wrongly_typed_argument_with_its_typed_error(
        server: Server, tool: str, arguments: dict, field: str) -> None:
    result = await _call(server, tool, arguments)

    assert result.isError is True
    error = json.loads(result.content[0].text)
    assert set(error) == {"error", "code"} and error["code"] == "invalid_payload"
    assert error["error"].startswith(f"invalid payload: {field} ")


async def test_a_valid_call_answers_as_before(server: Server) -> None:
    corrected = await _call(server, "log_correction", {
        "correction": "Keep receipts for expense claims.", "transaction_id": "valid-thin"})
    learned = await _call(server, "log_signal",
                          _envelope("valid-envelope", session_summary="Ship the release."))
    for result, transaction_id in ((corrected, "valid-thin"), (learned, "valid-envelope")):
        assert result.isError is False
        assert json.loads(result.content[0].text) == {
            "transaction_id": transaction_id, "status": "accepted",
            "state": "awaiting_manual_review"}

    listed = await _call(server, "list_skills", {"domain_hint": "engineering"})
    assert listed.isError is False
    assert [skill["id"] for skill in json.loads(listed.content[0].text)["skills"]] == ["checks"]

    fetched = await _call(server, "query_skill", {"name": "Checks"})
    assert fetched.isError is False
    assert json.loads(fetched.content[0].text)["id"] == "checks"
    assert "Check the result before publishing." in fetched.content[1].text


async def test_the_advertised_schemas_keep_every_limit(server: Server) -> None:
    """Clients still see the limits, although the SDK no longer enforces them."""
    handler = server.request_handlers[types.ListToolsRequest]
    listed = (await handler(types.ListToolsRequest(method="tools/list"))).root.tools
    schemas = {tool.name: tool.inputSchema for tool in listed}
    envelope = schemas["log_signal"]["$defs"]["ExecutionContext"]["properties"]
    for field, limit in LIMITS.items():
        for properties in (schemas["log_correction"]["properties"], envelope):
            assert [branch["maxLength"] for branch in properties[field]["anyOf"]
                    if "maxLength" in branch] == [limit]
    assert schemas["list_skills"]["properties"]["domain_hint"]["type"] == "string"
    assert schemas["query_skill"]["required"] == ["name"]
    assert schemas["query_skill_resource"]["required"] == ["skill", "resource"]


async def test_an_unauthenticated_call_is_refused_before_its_arguments_are_read(
        services: CoreServices) -> None:
    """Validation runs in the handlers, after authorisation, so a caller who
    may not call learns nothing about which of its arguments would pass."""
    anonymous = build_server(services.coordinator, catalogue=services.catalogue)

    result = await _call(anonymous, "log_correction",
                         _thin("anonymous", session_summary=_over(SESSION_SUMMARY_MAX)))

    assert result.isError is True
    assert json.loads(result.content[0].text)["code"] == "unauthorised"


@pytest.mark.parametrize("tool", ["log_correction", "log_signal"])
async def test_context_beside_an_envelope_is_refused_by_name(
        server: Server, services: CoreServices, tool: str) -> None:
    """With `execution_context` present the call is the full envelope, whose
    context lives inside it. Copies at the top level were dropped without a
    word, so the agent heard "accepted" and the reviewer read that no context
    was provided."""
    arguments = {**_envelope(f"mixed-{tool}"), "session_summary": "Ship the release.",
                 "reuse_case": "The next release build."}

    result = await _call(server, tool, arguments)

    assert result.isError is True
    error = json.loads(result.content[0].text)
    assert error == {"code": "invalid_payload", "error": (
        "invalid payload: session_summary, reuse_case: send these inside "
        "execution_context when the call carries one")}
    assert services.store.get_transaction(f"mixed-{tool}") is None
