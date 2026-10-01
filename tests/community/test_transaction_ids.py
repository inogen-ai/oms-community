"""A transaction id names one contribution, from one caller, of one kind.

A retry sends the same id again, so an answer that never arrived records
nothing twice. OMS answers a retry from the stored transaction, with its
current state, and does not compare what was sent. An id that another caller
already used, or that was already used for another kind of signal, is not a
retry: answering it with the stored receipt told the second caller that a
contribution was accepted when nothing of it was stored. Each door refuses it
as a conflict instead. The refusal is final for that id and writes nothing,
and the caller sends the contribution again under a new id.

A person keeps one identity across credentials, so the same person retrying
from a second token is still a retry. A transaction stored without a caller
gives nothing to compare, so it still answers a retry.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient
import mcp.types as types
from mcp.server import Server
import pytest

from oms.adapters.memory.review_queue import InMemoryReviewQueue
from oms.adapters.memory.store import InMemoryGraphStore
from oms.community.app import CoreServices, build_memory
from oms.community.workflow import Decision
from oms.domain.auth import INGEST_WRITE
from oms.domain.models import Principal, Transaction, unbound_principal
from oms.domain.types import SignalType, SourceRuntime
from oms.ingestion.contribution import ContributionRequest, correction_payload
from oms.ingestion.payload_store import FilePayloadStore
from oms.ingestion.sanitiser import RegexSanitiser
from oms.ingestion.schema import CorrectionPayload
from oms.ingestion.service import (
    TRANSACTION_ID_REUSED, TRANSACTION_ID_REUSED_MESSAGE, IngestionService, TransactionIdReused,
)
from oms.mcp_server.server import build_server
from oms.web.api import create_app

TENANT = "acme"
SUGGESTION = "suggestion-memo-1"
ALICE = Principal(id="token:alice", tenant_id=TENANT, scopes=frozenset({INGEST_WRITE}),
                  person_id="alice")
# Alice again, from a second credential: one person, two tokens.
ALICE_ELSEWHERE = Principal(id="token:alice-laptop", tenant_id=TENANT,
                            scopes=frozenset({INGEST_WRITE}), person_id="alice")
BOB = Principal(id="token:bob", tenant_id=TENANT, scopes=frozenset({INGEST_WRITE}),
                person_id="bob")
ALICE_SAYS = "Send the memo to the client before the board meeting."
BOB_SAYS = "File expense claims within thirty days."
REFUSAL = {"code": TRANSACTION_ID_REUSED, "retryable": False}


class World:
    """One graph, queue and payload root behind an ingestion service."""

    def __init__(self, root: Path) -> None:
        self.graph = InMemoryGraphStore()
        self.queue = InMemoryReviewQueue()
        self.root = root
        self.service = IngestionService(self.graph, RegexSanitiser(), FilePayloadStore(root),
                                        queue=self.queue)

    def written(self) -> dict:
        """Everything an ingest can leave behind, copied so a later change shows."""
        return deepcopy({
            "transactions": self.graph.transactions,
            "files": {path.relative_to(self.root).as_posix(): path.read_bytes()
                      for path in sorted(self.root.rglob("*")) if path.is_file()},
            "review_items": self.queue.items,
        })


def correction(transaction_id: str, text: str, principal: Principal) -> CorrectionPayload:
    return correction_payload(ContributionRequest(transaction_id=transaction_id, correction=text),
                              principal, SourceRuntime.MCP)


def learning(transaction_id: str, text: str, principal: Principal) -> CorrectionPayload:
    return correction_payload(ContributionRequest(
        transaction_id=transaction_id, learning=text, signal_type="self_reflection"),
        principal, SourceRuntime.MCP)


def test_another_persons_id_is_refused_and_writes_nothing(tmp_path):
    world = World(tmp_path)
    alices = world.service.ingest(correction(SUGGESTION, ALICE_SAYS, ALICE), ALICE)
    before = world.written()
    with pytest.raises(TransactionIdReused) as reused:
        world.service.ingest(correction(SUGGESTION, BOB_SAYS, BOB), BOB)
    assert (reused.value.code, reused.value.retryable) == (TRANSACTION_ID_REUSED, False)
    assert reused.value.message == TRANSACTION_ID_REUSED_MESSAGE
    # Alice's transaction is untouched and nothing of Bob's was stored.
    assert world.written() == before
    assert world.graph.get_transaction(SUGGESTION) == alices
    # Bob's contribution goes in under a new id.
    assert world.service.ingest(correction("suggestion-memo-2", BOB_SAYS, BOB),
                                BOB).person_id == "bob"


def test_the_same_person_retrying_from_another_credential_gets_the_receipt(tmp_path):
    world = World(tmp_path)
    first = world.service.ingest(correction(SUGGESTION, ALICE_SAYS, ALICE), ALICE)
    before = world.written()
    assert world.service.ingest(correction(SUGGESTION, ALICE_SAYS, ALICE_ELSEWHERE),
                                ALICE_ELSEWHERE) == first
    assert world.written() == before


def test_an_id_used_for_another_kind_of_signal_is_refused(tmp_path):
    # One caller, but a correction and a session learning are different
    # contributions, and the second would otherwise never be stored.
    world = World(tmp_path)
    world.service.ingest(correction(SUGGESTION, ALICE_SAYS, ALICE), ALICE)
    before = world.written()
    with pytest.raises(TransactionIdReused):
        world.service.ingest(learning(SUGGESTION, "In this repo, run the linter first.", ALICE),
                             ALICE)
    assert world.written() == before


def test_an_unbound_caller_cannot_take_a_persons_id(tmp_path):
    world = World(tmp_path)
    world.service.ingest(correction(SUGGESTION, ALICE_SAYS, ALICE), ALICE)
    anonymous = unbound_principal(TENANT)
    with pytest.raises(TransactionIdReused):
        world.service.ingest(correction(SUGGESTION, BOB_SAYS, anonymous), anonymous)


def test_a_transaction_stored_without_a_caller_still_answers_a_retry(tmp_path):
    # Written before callers were recorded: there is nothing to compare, and
    # refusing on missing evidence would turn a genuine retry into a duplicate.
    world = World(tmp_path)
    legacy = Transaction(id=SUGGESTION, signal_type=SignalType.EXPLICIT_CORRECTION,
                         source_runtime=SourceRuntime.HTTP, sanitised_payload_ref="legacy.json",
                         timestamp=datetime(2026, 1, 5, tzinfo=timezone.utc), tenant_id=TENANT)
    world.graph.upsert_transaction(legacy)
    assert world.service.ingest(correction(SUGGESTION, ALICE_SAYS, ALICE), ALICE) == legacy


async def _call(server: Server, name: str, arguments: dict) -> tuple[bool, dict]:
    handler = server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=name, arguments=arguments))
    result = (await handler(request)).root
    return bool(result.isError), json.loads(result.content[0].text)


async def test_both_mcp_tools_refuse_a_reused_id_as_final(tmp_path):
    world = World(tmp_path)
    world.service.ingest(correction(SUGGESTION, ALICE_SAYS, ALICE), ALICE)
    before = world.written()
    bob = build_server(world.service, resolve_principal=lambda: BOB)
    envelope = correction(SUGGESTION, BOB_SAYS, BOB).model_dump(mode="json")
    for name, arguments in (("log_correction", {"transaction_id": SUGGESTION,
                                                "correction": BOB_SAYS}),
                            ("log_signal", envelope)):
        is_error, answer = await _call(bob, name, arguments)
        assert is_error is True
        assert answer == {"error": TRANSACTION_ID_REUSED_MESSAGE, **REFUSAL}
    assert world.written() == before
    # The same person's retry over MCP is still answered with the receipt.
    alice = build_server(world.service, resolve_principal=lambda: ALICE_ELSEWHERE)
    is_error, answer = await _call(alice, "log_correction",
                                   {"transaction_id": SUGGESTION, "correction": ALICE_SAYS})
    assert (is_error, answer) == (False, {"transaction_id": SUGGESTION, "status": "accepted"})


@pytest.fixture
def services(tmp_path: Path) -> CoreServices:
    return build_memory(tmp_path / "workspace", tenant=TENANT)


def _client(services: CoreServices) -> TestClient:
    return TestClient(create_app(services), base_url="http://127.0.0.1:4317")


def test_the_http_door_answers_409_for_another_callers_id(services):
    # The local operator is the only caller Community authenticates, so the
    # other caller's transaction is stored directly, as an import or a
    # restored database would leave it.
    services.store.upsert_transaction(Transaction(
        id=SUGGESTION, signal_type=SignalType.EXPLICIT_CORRECTION,
        source_runtime=SourceRuntime.HTTP, sanitised_payload_ref=f"{SUGGESTION}.json",
        timestamp=datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc), tenant_id=TENANT,
        principal_id="token:someone", person_id="someone", summary=ALICE_SAYS))
    with _client(services) as client:
        response = client.post("/api/ingest",
                               json={"transaction_id": SUGGESTION, "correction": BOB_SAYS})
    assert response.status_code == 409
    assert response.json() == {"detail": TRANSACTION_ID_REUSED_MESSAGE, "reason": TRANSACTION_ID_REUSED,
                               "retryable": False}
    assert services.store.get_transaction(SUGGESTION).summary == ALICE_SAYS
    assert services.manual.inbox(TENANT) == []


def test_a_retry_answers_with_the_transactions_current_state(services):
    # The receipt reports where the contribution is now, not where it was
    # when the first answer was lost.
    body = {"transaction_id": SUGGESTION, "correction": ALICE_SAYS}
    with _client(services) as client:
        first = client.post("/api/ingest", json=body)
        assert first.status_code == 202
        services.manual.decide(SUGGESTION, TENANT, "local-reviewer", Decision(action="reject"))
        retry = client.post("/api/ingest", json=body)
    assert retry.status_code == 202
    decided = services.store.get_transaction(SUGGESTION).workflow_state
    assert decided != first.json()["state"]
    assert retry.json() == {"transaction_id": SUGGESTION, "status": "accepted", "state": decided}


def test_the_thin_schema_asks_for_a_new_uuid_per_contribution():
    # The MCP input schema is the only guidance an agent calling the tool
    # directly reads about the id.
    described = ContributionRequest.model_json_schema()["properties"]["transaction_id"]
    assert described["description"] == (
        "A new UUID per contribution; resend the same id only to retry the identical request.")
