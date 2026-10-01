"""Bounded session context carried with a contribution.

An agent may say what it was working on, the future task a learning would
help, and what it observed. That context arrives through both front doors,
is scrubbed like every other agent-supplied text, and is kept with the
payload and on the transaction row. It is never served by the open graph
reads, and it is never invented when absent.
"""
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest
from pydantic import ValidationError

from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.auth import INGEST_WRITE
from oms.domain.models import Edge, Principal, Rule, Transaction
from oms.domain.types import EdgeType, SignalType, SourceRuntime
from oms.ingestion.contribution import ContributionRequest, correction_payload
from oms.ingestion.payload_store import FilePayloadStore
from oms.ingestion.sanitiser import RegexSanitiser
from oms.ingestion.schema import (
    LEARNING_EVIDENCE_MAX, PROJECT_NAME_MAX, REUSE_CASE_MAX, SESSION_SUMMARY_MAX,
    CorrectionPayload, ExecutionContext, clip_context_fields,
)
from oms.ingestion.service import IngestionService
from tests.community.test_critic_workflow import critic_neo4j_driver  # noqa: F401


WHEN = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)
PRINCIPAL = Principal(id="agent", tenant_id="acme", scopes=frozenset({INGEST_WRITE}))
LIMITS = {
    "session_summary": SESSION_SUMMARY_MAX,
    "project_name": PROJECT_NAME_MAX,
    "reuse_case": REUSE_CASE_MAX,
    "learning_evidence": LEARNING_EVIDENCE_MAX,
}
DESCRIPTIONS = {
    "session_summary": (
        "One or two factual sentences stating the objective of the current piece "
        "of work (not a completion report). Context only; up to 400 characters."),
    "project_name": (
        "Human-readable project name, mainly for work outside a repository. "
        "A name, never a path. Up to 160 characters."),
    "reuse_case": (
        "The future task this would help and what a future agent should do "
        "differently. Up to 400 characters."),
    "learning_evidence": (
        "What you observed that supports this lesson and its scope. Up to 600 "
        "characters. Never secrets, raw transcripts or local paths."),
}
# Personal data an agent might quote while describing its work.
SUMMARY_WITH_PHONE = "Ship the release; the owner is on +44 20 7946 0958."
EVIDENCE_WITH_EMAIL = "The failure report came from ops@example.com."


def _context(record) -> tuple:
    return tuple(getattr(record, name) for name in LIMITS)


def _thin(**context) -> ContributionRequest:
    return ContributionRequest(correction="Keep receipts for expense claims.", **context)


def _envelope(**context) -> CorrectionPayload:
    return CorrectionPayload.model_validate({
        "transaction_id": "context-envelope", "timestamp": WHEN.isoformat(),
        "source_agent_id": "agent", "source_runtime": "mcp", "tenant_id": "acme",
        "signal_type": "explicit_correction",
        "execution_context": {"user_input": "", "agent_raw_output": "",
                              "user_correction": "Keep receipts for expense claims.",
                              **context}})


def _transaction(**context) -> Transaction:
    return Transaction(
        id="context-1", signal_type=SignalType.SELF_REFLECTION,
        source_runtime=SourceRuntime.MCP, sanitised_payload_ref="context-1.json",
        timestamp=WHEN, tenant_id="acme", **context)


@pytest.fixture(params=["memory", pytest.param("neo4j", marks=pytest.mark.integration)])
def store(request):
    if request.param == "memory":
        return InMemoryGraphStore()
    from oms.adapters.neo4j.store import Neo4jGraphStore

    driver = request.getfixturevalue("critic_neo4j_driver")
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n").consume()
    graph = Neo4jGraphStore(driver)
    graph.ensure_schema()
    return graph


def test_the_thin_request_carries_the_four_fields_into_the_envelope():
    body = ContributionRequest(
        learning="In this repo, run X", signal_type="self_reflection",
        session_summary=" Separate the package. ", project_name="Release",
        reuse_case="Future release builds",
        learning_evidence="The editable install hid a missing file")
    context = correction_payload(body, PRINCIPAL, SourceRuntime.MCP).execution_context
    assert _context(context) == (
        "Separate the package.", "Release", "Future release builds",
        "The editable install hid a missing file")


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
def test_blank_context_is_absent_not_empty(blank):
    blanks = {name: blank for name in LIMITS}
    body = _thin(**blanks)
    envelope = correction_payload(body, PRINCIPAL, SourceRuntime.HTTP).execution_context
    for record in (body, envelope, _envelope(**blanks).execution_context):
        assert _context(record) == (None, None, None, None)


def test_each_limit_is_enforced_and_names_the_field():
    assert LIMITS == {"session_summary": 400, "project_name": 160,
                      "reuse_case": 400, "learning_evidence": 600}
    for name, limit in LIMITS.items():
        at_limit = "x" * limit
        # The limit counts what is kept: surrounding whitespace is stripped
        # before it is measured, so padding cannot push a value over.
        assert getattr(_thin(**{name: f"  {at_limit}\n"}), name) == at_limit
        assert getattr(_envelope(**{name: at_limit}).execution_context, name) == at_limit
        for build, location in ((_thin, (name,)),
                                (_envelope, ("execution_context", name))):
            with pytest.raises(ValidationError) as refused:
                build(**{name: at_limit + "x"})
            (error,) = refused.value.errors()
            assert (error["loc"], error["type"]) == (location, "string_too_long")
            assert name in str(refused.value)


def test_an_old_payload_without_the_fields_still_validates(tmp_path):
    context = ExecutionContext.model_validate({"user_input": "", "agent_raw_output": ""})
    assert _context(context) == (None, None, None, None)
    # A payload file written before these fields existed still reads back.
    (tmp_path / "before.json").write_text(json.dumps({
        "user_input": "Tidy the expenses page", "agent_raw_output": "Done",
        "user_correction": "Keep receipts for expense claims.", "learning": None}))
    stored = FilePayloadStore(tmp_path).get("before")
    assert stored.user_correction == "Keep receipts for expense claims."
    assert _context(stored) == (None, None, None, None)


def test_the_regex_sanitiser_scrubs_the_context_fields():
    original = ExecutionContext(
        user_input="Prepare the release", agent_raw_output="Released",
        learning="In this repo, build the wheel before tagging.",
        session_summary=SUMMARY_WITH_PHONE, project_name="Release",
        reuse_case="Future release builds", learning_evidence=EVIDENCE_WITH_EMAIL)
    result = RegexSanitiser().sanitise(original)
    assert _context(result.context) == (
        "Ship the release; the owner is on <PHONE_1>.", "Release",
        "Future release builds", "The failure report came from <EMAIL_1>.")
    # The learning is untouched, so the ingest hold has nothing to see and
    # nothing is retained for a reviewer to reveal.
    assert result.context.learning == original.learning
    assert result.redactions == ()


def test_context_originals_are_not_retained_and_do_not_renumber_the_learning():
    result = RegexSanitiser().sanitise(ExecutionContext(
        user_input="", agent_raw_output="",
        learning="Escalate release failures to lead@example.com.",
        learning_evidence="Reported by ops@example.com, then lead@example.com."))
    # The learning keeps the placeholder it had before context existed, and
    # one address keeps one placeholder across both fields.
    assert result.context.learning == "Escalate release failures to <EMAIL_1>."
    assert result.context.learning_evidence == "Reported by <EMAIL_2>, then <EMAIL_1>."
    assert [redaction.original for redaction in result.redactions] == ["lead@example.com"]


def test_ingest_projects_the_sanitised_context_onto_the_transaction(tmp_path):
    graph = InMemoryGraphStore()
    payloads = FilePayloadStore(tmp_path)
    service = IngestionService(graph, RegexSanitiser(), payloads)
    service.ingest(correction_payload(ContributionRequest(
        transaction_id="context-1", signal_type="self_reflection",
        learning="In this repo, build the wheel before tagging.",
        source_ref="session:release", session_summary=SUMMARY_WITH_PHONE,
        project_name="Release", reuse_case="Future release builds",
        learning_evidence=EVIDENCE_WITH_EMAIL), PRINCIPAL, SourceRuntime.MCP), PRINCIPAL)
    scrubbed = ("Ship the release; the owner is on <PHONE_1>.", "Release",
                "Future release builds", "The failure report came from <EMAIL_1>.")
    stored = graph.get_transaction("context-1")
    assert _context(stored) == scrubbed
    assert _context(payloads.get("context-1")) == scrubbed
    assert stored.held_reason is None
    written = (tmp_path / "context-1.json").read_text()
    assert "ops@example.com" not in written and "7946" not in written

    # Absent context stays absent: nothing is derived from the learning.
    service.ingest(correction_payload(ContributionRequest(
        transaction_id="context-2", correction="Keep receipts for expense claims."),
        PRINCIPAL, SourceRuntime.HTTP), PRINCIPAL)
    assert _context(graph.get_transaction("context-2")) == (None, None, None, None)


def test_placeholder_growth_is_clipped_to_the_limit_rather_than_quarantined(tmp_path):
    graph = InMemoryGraphStore()
    payloads = FilePayloadStore(tmp_path)
    service = IngestionService(graph, RegexSanitiser(), payloads)
    # Each field arrives exactly at its limit. The 6-character address becomes
    # the 9-character `<EMAIL_1>`, so scrubbing alone takes it 3 over.
    sent, kept = "Owner a@b.io; ", "Owner <EMAIL_1>; "
    learning = "In this repo, build the wheel before tagging."
    service.ingest(correction_payload(ContributionRequest(
        transaction_id="growth", signal_type="self_reflection", learning=learning,
        **{name: sent + "x" * (limit - len(sent)) for name, limit in LIMITS.items()}),
        PRINCIPAL, SourceRuntime.MCP), PRINCIPAL)
    assert graph.quarantined_payloads("acme") == []
    stored, payload = graph.get_transaction("growth"), payloads.get("growth")
    for name, limit in LIMITS.items():
        clipped = kept + "x" * (limit - len(kept) - 1) + "…"
        assert len(clipped) == limit
        assert getattr(stored, name) == getattr(payload, name) == clipped
    assert payload.learning == learning and stored.held_reason is None
    # Only growth inside the sanitiser is clipped: input over a limit is
    # still refused at the boundary, with the field named.
    for name, limit in LIMITS.items():
        with pytest.raises(ValidationError, match=name):
            _thin(**{name: "x" * (limit + 1)})


def test_the_clip_leaves_short_values_and_none_untouched():
    context = ExecutionContext(
        user_input="Prepare the release", agent_raw_output="Released",
        learning="In this repo, build the wheel before tagging.",
        session_summary="Ship the release.", project_name="x" * PROJECT_NAME_MAX)
    assert clip_context_fields(context) == context
    # What a sanitiser hands over: built without validation, one field grown.
    grown = ExecutionContext.model_construct(
        user_input="", agent_raw_output="", reuse_case="x" * (REUSE_CASE_MAX + 3))
    assert clip_context_fields(grown).reuse_case == "x" * (REUSE_CASE_MAX - 1) + "…"


def test_the_mcp_input_schemas_advertise_the_fields():
    thin = ContributionRequest.model_json_schema()["properties"]
    envelope = CorrectionPayload.model_json_schema()["$defs"]["ExecutionContext"]["properties"]
    for properties in (thin, envelope):
        for name, limit in LIMITS.items():
            branches = properties[name].get("anyOf", [properties[name]])
            assert [branch["maxLength"] for branch in branches if "maxLength" in branch] == [limit]
            assert properties[name]["description"] == DESCRIPTIONS[name]


def test_the_context_survives_a_store_round_trip(store):
    written = _transaction(session_summary="Ship the release.", project_name="Release",
                           reuse_case="Future release builds",
                           learning_evidence="The wheel was missing a file.")
    store.upsert_transaction(written)
    read = store.get_transaction("context-1")
    assert _context(read) == _context(written)
    # Workflow code reads a row, changes one field and writes the whole row
    # back, so a field the read missed would be erased by the next write.
    read.workflow_state = "awaiting_manual_review"
    store.upsert_transaction(read)
    assert _context(store.get_transaction("context-1")) == _context(written)


def test_graph_reads_never_carry_the_session_context(store):
    store.upsert_transaction(_transaction(
        session_summary="Ship the release.", project_name="Release",
        reuse_case="Future release builds", learning_evidence="The wheel was missing a file."))
    store.upsert_rule(Rule(id="rule-1", body="Build the wheel before tagging.", tenant_id="acme"))
    store.attach_edge(Edge(type=EdgeType.DERIVED_FROM, from_id="rule-1", to_id="context-1"))
    node = store.graph_node("context-1", "acme")
    (neighbour,) = [found for found in store.graph_neighbours("rule-1", "acme")["nodes"]
                    if found["id"] == "context-1"]
    for record in (node, neighbour):
        assert record["kind"] == "transaction"
        assert record["properties"]["signal_type"] == "self_reflection"
        assert not set(LIMITS) & set(record["properties"])
        served = json.dumps(record)
        assert "Ship the release." not in served and "missing a file" not in served


def test_clearing_the_context_erases_it_and_keeps_the_record(store):
    # Deleting a payload file does not erase what was copied from it onto the
    # transaction row, so an erasure clears the four fields and the summary
    # too. The rest of the row stays: it is the record that a contribution
    # arrived and what became of it.
    store.upsert_transaction(_transaction(
        session_summary="Ship the release.", project_name="Release",
        reuse_case="Future release builds", learning_evidence="The wheel was missing a file.",
        summary="Build the wheel before tagging.", person_id="p-17"))
    store.upsert_transaction(replace(_transaction(), id="context-2", summary="Unrelated.",
                                     session_summary="Another person's work."))
    store.upsert_transaction(replace(_transaction(), id="context-other-tenant",
                                     tenant_id="globex", summary="Not this workspace."))

    cleared = store.clear_transaction_context(
        ["context-1", "context-other-tenant", "no-such-transaction"], "acme")

    assert cleared == 1
    erased = store.get_transaction("context-1")
    assert _context(erased) == (None, None, None, None) and erased.summary is None
    assert (erased.person_id, erased.signal_type, erased.sanitised_payload_ref) == (
        "p-17", SignalType.SELF_REFLECTION, "context-1.json")
    # Only the named transactions of this workspace change.
    assert store.get_transaction("context-2").session_summary == "Another person's work."
    assert store.get_transaction("context-other-tenant").summary == "Not this workspace."
    # Nothing left to clear: a second run answers 0, and no ids cost nothing.
    assert store.clear_transaction_context(["context-1"], "acme") == 0
    assert store.clear_transaction_context([], "acme") == 0


def test_delete_payload_clears_the_context_on_the_transaction_too(tmp_path, monkeypatch, capsys):
    import oms.adapters.neo4j.driver as driver_module
    import oms.community.app as app_module
    from oms import cli
    from oms.community.app import build_memory

    services = build_memory(tmp_path / "workspace", tenant="acme")
    services.store.upsert_transaction(_transaction(
        session_summary="Ship the release.", learning_evidence="The wheel was missing a file.",
        summary="Build the wheel before tagging."))
    services.payloads.put("context-1", ExecutionContext(
        user_input="", agent_raw_output="", learning="Build the wheel before tagging."))

    class Driver:
        def close(self) -> None:
            return None

    # The command builds its services from a Neo4j driver; these stand in.
    monkeypatch.setattr(driver_module, "build_driver", lambda *args: Driver())
    monkeypatch.setattr(app_module, "build_community", lambda settings, driver: services)
    monkeypatch.setenv("OMS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OMS_TENANT", "acme")

    assert cli.main(["delete-payload", "context-1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": "context-1", "context_cleared": 1}
    assert services.payloads.get("context-1") is None
    erased = services.store.get_transaction("context-1")
    assert _context(erased) == (None, None, None, None) and erased.summary is None
