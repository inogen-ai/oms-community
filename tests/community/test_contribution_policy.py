"""A tenant policy consulted before a new contribution is admitted.

A composition can refuse new work through the policy port, for example a
workspace that no longer accepts session learnings from agents whose
instruction files still ask for them. A refusal is final and every transport
says so in a form a client can branch on; it writes nothing at all. An
unreadable policy is an operational fault to retry later and never an
admission. A transaction admitted before the policy changed still returns
its original receipt. The Community default admits everything, so nothing
changes unless a policy is injected.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path

from fastapi.testclient import TestClient
import mcp.types as types
import pytest

from oms.adapters.memory.review_queue import InMemoryReviewQueue
from oms.adapters.memory.store import InMemoryGraphStore
from oms.community.app import build_memory
from oms.domain.auth import INGEST_WRITE
from oms.domain.models import Principal
from oms.domain.types import SourceRuntime
from oms.ingestion.contribution import ContributionRequest, correction_payload
from oms.ingestion.coordinator import ContributionCoordinator
from oms.ingestion.payload_store import FilePayloadStore
from oms.ingestion.sanitiser import RegexSanitiser, Sanitised
from oms.ingestion.schema import CorrectionPayload, ExecutionContext
from oms.ingestion.service import (
    IngestionService, SanitisationError, TenantMismatchError, UnauthorisedError,
)
from oms.mcp_server.server import build_server
from oms.ports.contribution_policy import (
    SESSION_LEARNINGS_DISABLED, AdmitEveryContribution, ContributionPolicy,
    ContributionPolicyUnavailable, ContributionRefused,
)
from oms.web.api import create_app


PRINCIPAL = Principal(id="agent", tenant_id="acme", scopes=frozenset({INGEST_WRITE}))
# What the stand-in policy says. The wording belongs to whichever composition
# injects a policy; the transports carry it through unchanged.
REFUSAL = "Session learnings are not accepted here. Nothing was recorded."
# Fixed by the transports, whatever the policy's own exception said.
UNAVAILABLE = "contribution policy is unavailable; nothing was recorded; retry later"
# Admitted, this learning leaves everything a contribution can leave: the
# sanitiser scrubs the address, which holds the transaction and queues a
# review item, and custody is asked to retain the redaction.
HELD_LEARNING = "Send release failures to lead@example.com before retrying."


class RefuseNewContributions:
    """A stand-in tenant policy that no longer accepts session learnings."""

    def __init__(self) -> None:
        self.consulted: list[str] = []

    def admit(self, payload: CorrectionPayload, principal: Principal) -> None:
        self.consulted.append(payload.transaction_id)
        raise ContributionRefused(SESSION_LEARNINGS_DISABLED, REFUSAL)


class UnreadablePolicy:
    """A policy whose own store did not answer."""

    def admit(self, payload: CorrectionPayload, principal: Principal) -> None:
        raise ContributionPolicyUnavailable("the policy store timed out after 5s")


class FalseInABooleanTest(RefuseNewContributions):
    """A refusing policy that tests false, as one sized over an empty
    collection would. Only None may mean "no policy"."""

    def __bool__(self) -> bool:
        return False


class RecordingCustody:
    """Custody that remembers every call, so a test can prove there were none."""
    status = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def retain(self, redactions, transaction_id: str, tenant_id: str) -> None:
        self.calls.append(("retain", transaction_id))

    def quarantine(self, payload: CorrectionPayload, principal: Principal) -> None:
        self.calls.append(("quarantine", payload.transaction_id))

    def forget(self, transaction_id: str, tenant_id: str) -> None:
        self.calls.append(("forget", transaction_id))


class CountingSanitiser:
    """The regex sanitiser, counted; or, with `fail`, one that cannot run."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def sanitise(self, ctx: ExecutionContext) -> Sanitised:
        self.calls += 1
        if self.fail:
            raise RuntimeError("the sanitiser could not run")
        return RegexSanitiser().sanitise(ctx)


@dataclass
class World:
    graph: InMemoryGraphStore
    queue: InMemoryReviewQueue
    payloads: FilePayloadStore
    payload_root: Path
    custody: RecordingCustody
    sanitiser: CountingSanitiser
    service: IngestionService

    def with_policy(self, policy: ContributionPolicy | None) -> IngestionService:
        """The same stores behind a service consulting a different policy."""
        return IngestionService(self.graph, self.sanitiser, self.payloads, queue=self.queue,
                                custody=self.custody, policy=policy)

    def written(self) -> dict:
        """Everything an ingest can leave behind, copied so a later change shows."""
        return deepcopy({
            "transactions": self.graph.transactions,
            "quarantine_records": self.graph.quarantined_payloads("acme"),
            "files": {path.relative_to(self.payload_root).as_posix(): path.read_bytes()
                      for path in sorted(self.payload_root.rglob("*")) if path.is_file()},
            "review_items": self.queue.items,
            "custody": self.custody.calls,
        })


NOTHING = {"transactions": {}, "quarantine_records": [], "files": {},
           "review_items": [], "custody": []}


def world(root: Path, policy: ContributionPolicy | None = None, *,
          failing_sanitiser: bool = False) -> World:
    graph, queue, custody = InMemoryGraphStore(), InMemoryReviewQueue(), RecordingCustody()
    sanitiser = CountingSanitiser(fail=failing_sanitiser)
    payloads = FilePayloadStore(root)
    service = IngestionService(graph, sanitiser, payloads, queue=queue, custody=custody,
                               policy=policy)
    return World(graph, queue, payloads, root, custody, sanitiser, service)


def learning(transaction_id: str, principal: Principal = PRINCIPAL) -> CorrectionPayload:
    """A session learning, as an agent's stale instruction file still sends it."""
    return correction_payload(ContributionRequest(
        transaction_id=transaction_id, signal_type="self_reflection",
        learning=HELD_LEARNING, source_ref="session:release"), principal, SourceRuntime.MCP)


def test_the_default_policy_admits_everything(tmp_path):
    default = AdmitEveryContribution()
    assert isinstance(default, ContributionPolicy)
    stated = correction_payload(ContributionRequest(
        transaction_id="stated", correction="Keep receipts for expense claims."),
        PRINCIPAL, SourceRuntime.HTTP)
    for payload in (stated, learning("learned")):
        assert default.admit(payload, PRINCIPAL) is None
    # A service built without a policy admits and holds exactly as before
    # the port existed.
    admitted = world(tmp_path / "payloads")
    for payload in (stated, learning("learned")):
        assert admitted.service.ingest(payload, PRINCIPAL).id == payload.transaction_id
    written = admitted.written()
    assert sorted(written["transactions"]) == ["learned", "stated"]
    assert [item.id for item in written["review_items"]] == ["review-held-learned"]


def test_a_policy_that_tests_false_is_still_consulted(tmp_path):
    with pytest.raises(ContributionRefused):
        world(tmp_path / "payloads", FalseInABooleanTest()).service.ingest(learning("falsy"), PRINCIPAL)


def test_a_refused_contribution_writes_nothing(tmp_path):
    refused_world = world(tmp_path / "refused", RefuseNewContributions())
    with pytest.raises(ContributionRefused) as refused:
        refused_world.service.ingest(learning("stale-1"), PRINCIPAL)
    assert (refused.value.code, refused.value.message, str(refused.value)) == (
        SESSION_LEARNINGS_DISABLED, REFUSAL, REFUSAL)
    assert refused.value.retryable is False
    assert refused_world.written() == NOTHING
    # The same contribution under the default policy leaves a transaction, a
    # payload file, a review item and a custody entry, so their absence above
    # is the refusal's doing.
    admitted = world(tmp_path / "admitted")
    admitted.service.ingest(learning("stale-1"), PRINCIPAL)
    written = admitted.written()
    assert list(written["transactions"]) == ["stale-1"]
    assert list(written["files"]) == ["stale-1.json"]
    assert [item.id for item in written["review_items"]] == ["review-held-stale-1"]
    assert written["custody"] == [("retain", "stale-1")]


def test_the_policy_runs_before_sanitisation(tmp_path):
    refused_world = world(tmp_path / "refused", RefuseNewContributions(), failing_sanitiser=True)
    with pytest.raises(ContributionRefused):
        refused_world.service.ingest(learning("stale-2"), PRINCIPAL)
    assert refused_world.sanitiser.calls == 0
    assert refused_world.written() == NOTHING
    # Admitted, the same failing sanitiser quarantines: a file, a record and a
    # custody copy. A refused contribution never reaches that branch.
    admitted = world(tmp_path / "admitted", failing_sanitiser=True)
    with pytest.raises(SanitisationError):
        admitted.service.ingest(learning("stale-2"), PRINCIPAL)
    written = admitted.written()
    assert list(written["files"]) == ["_flagged_errors/stale-2.txt"]
    assert [record.transaction_id for record in written["quarantine_records"]] == ["stale-2"]
    assert written["custody"] == [("quarantine", "stale-2")]


def test_an_admitted_transaction_retried_after_a_refusing_policy_returns_its_receipt(tmp_path):
    before = world(tmp_path / "payloads")
    original = before.service.ingest(learning("admitted-1"), PRINCIPAL)
    written = before.written()
    # The policy changes underneath the same stores.
    policy = RefuseNewContributions()
    after = before.with_policy(policy)
    assert after.ingest(learning("admitted-1"), PRINCIPAL) == original
    # Answered from the replay check: the policy was never asked, nothing was
    # sanitised again, and no row, file, review item or custody entry moved.
    assert policy.consulted == []
    assert before.sanitiser.calls == 1
    assert before.written() == written
    # New work through the same service is refused.
    with pytest.raises(ContributionRefused):
        after.ingest(learning("admitted-2"), PRINCIPAL)
    assert policy.consulted == ["admitted-2"]
    assert before.written() == written


def test_authorisation_comes_before_the_policy(tmp_path):
    # A caller who may not write here learns nothing about the policy: the
    # scope check, the tenant check and a colliding id from another workspace
    # all answer before it is asked.
    shared = world(tmp_path / "payloads")
    shared.service.ingest(learning("collision"), PRINCIPAL)
    policy = RefuseNewContributions()
    service = shared.with_policy(policy)
    other = Principal(id="agent", tenant_id="globex", scopes=frozenset({INGEST_WRITE}))
    reader = Principal(id="reader", tenant_id="acme")
    for payload, principal, error in ((learning("fresh"), reader, UnauthorisedError),
                                      (learning("fresh"), other, TenantMismatchError),
                                      (learning("collision", other), other, TenantMismatchError)):
        with pytest.raises(error):
            service.ingest(payload, principal)
    assert policy.consulted == []


def test_an_unavailable_policy_is_an_error_not_an_admission(tmp_path):
    unavailable = world(tmp_path / "payloads", UnreadablePolicy())
    with pytest.raises(ContributionPolicyUnavailable) as fault:
        unavailable.service.ingest(learning("later-1"), PRINCIPAL)
    assert fault.value.retryable is True
    # Distinct from a refusal: a client told to stop would drop work that a
    # later retry would have recorded.
    assert not isinstance(fault.value, ContributionRefused)
    assert unavailable.sanitiser.calls == 0
    assert unavailable.written() == NOTHING
    # Nothing was reserved, so the retry the error asks for, once the policy
    # answers, admits the same id as new work.
    recovered = unavailable.with_policy(None)
    assert recovered.ingest(learning("later-1"), PRINCIPAL).id == "later-1"
    assert list(unavailable.written()["transactions"]) == ["later-1"]


async def _call(server, name: str, arguments: dict) -> tuple[types.CallToolResult, dict]:
    handler = server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=name, arguments=arguments))
    result = (await handler(request)).root
    return result, json.loads(result.content[0].text)


def _thin_learning(transaction_id: str) -> dict:
    return {"transaction_id": transaction_id, "signal_type": "self_reflection",
            "learning": HELD_LEARNING}


async def test_the_mcp_tool_result_is_an_error_that_says_do_not_retry(tmp_path):
    refused_world = world(tmp_path / "payloads", RefuseNewContributions())
    server = build_server(refused_world.service, resolve_principal=lambda: PRINCIPAL)
    calls = (("log_correction", _thin_learning("stale-mcp-1")),
             ("log_signal", learning("stale-mcp-2").model_dump(mode="json")))
    for name, arguments in calls:
        result, error = await _call(server, name, arguments)
        assert result.isError is True
        assert error == {"error": REFUSAL, "code": SESSION_LEARNINGS_DISABLED,
                         "retryable": False}
    assert refused_world.written() == NOTHING
    # Every existing error keeps its exact two-key shape. A learning without
    # its signal type passes the input schema and fails the model.
    result, error = await _call(server, "log_correction", {"learning": HELD_LEARNING})
    assert result.isError is True
    assert set(error) == {"error", "code"} and error["code"] == "invalid_payload"
    quarantining = world(tmp_path / "quarantine", failing_sanitiser=True)
    result, error = await _call(build_server(quarantining.service, resolve_principal=lambda: PRINCIPAL),
                                "log_correction", _thin_learning("stale-mcp-3"))
    assert result.isError is True
    assert error == {"error": "sanitisation failed, quarantined: stale-mcp-3",
                     "code": "quarantined"}


async def test_an_unavailable_policy_tells_an_mcp_client_to_retry_later(tmp_path):
    unavailable = world(tmp_path / "payloads", UnreadablePolicy())
    server = build_server(unavailable.service, resolve_principal=lambda: PRINCIPAL)
    result, error = await _call(server, "log_correction", _thin_learning("later-mcp"))
    assert result.isError is True
    # The fixed sentence, never the policy's own text about its store.
    assert error == {"error": UNAVAILABLE, "code": "unavailable", "retryable": True}
    assert unavailable.written() == NOTHING


@pytest.fixture
def community(tmp_path):
    """The Community app over one workspace, with a policy injected on request."""
    services = build_memory(tmp_path / "workspace", tenant="acme")

    def serve(policy: ContributionPolicy) -> TestClient:
        ingestion = IngestionService(services.store, RegexSanitiser(), services.payloads,
                                     policy=policy)
        coordinator = ContributionCoordinator(ingestion, services.coordinator.disposition,
                                              services.repository)
        return TestClient(create_app(replace(services, coordinator=coordinator)),
                          base_url="http://127.0.0.1:4317")
    return services, serve, tmp_path / "workspace" / "payloads"


def _left_behind(services, payload_root: Path, transaction_id: str) -> tuple:
    return (services.store.get_transaction(transaction_id), services.queue.items,
            services.manual.inbox("acme"), sorted(payload_root.rglob("*")))


def test_the_community_ingest_route_answers_403_with_a_reason(community):
    services, serve, payload_root = community
    with serve(RefuseNewContributions()) as client:
        response = client.post("/api/ingest", json=_thin_learning("stale-http"))
    assert response.status_code == 403
    assert response.json() == {"detail": REFUSAL, "reason": SESSION_LEARNINGS_DISABLED,
                               "retryable": False}
    assert _left_behind(services, payload_root, "stale-http") == (None, [], [], [])


def test_the_community_ingest_route_answers_503_while_the_policy_is_unavailable(community):
    services, serve, payload_root = community
    with serve(UnreadablePolicy()) as client:
        response = client.post("/api/ingest", json=_thin_learning("later-http"))
    assert response.status_code == 503
    assert response.json() == {"detail": UNAVAILABLE, "reason": "unavailable",
                               "retryable": True}
    assert _left_behind(services, payload_root, "later-http") == (None, [], [], [])


def test_the_coordinator_leaves_nothing_behind_on_a_refusal(community):
    services, _serve, payload_root = community
    principal = services.principal_resolver.resolve()
    ingestion = IngestionService(services.store, RegexSanitiser(), services.payloads,
                                 policy=RefuseNewContributions())
    # The coordinator runs a copy of the service inside the repository's unit
    # of work, so this refusal also proves the copy keeps the policy.
    refusing = ContributionCoordinator(ingestion, services.coordinator.disposition,
                                       services.repository)
    payload = learning("stale-coordinated", principal)
    with pytest.raises(ContributionRefused):
        refusing.ingest(payload, principal)
    assert _left_behind(services, payload_root, "stale-coordinated") == (None, [], [], [])
    assert services.repository.undisposed("acme") == []
    assert refusing.reconcile("acme") == 0
    # The composed coordinator, on the default policy, then admits the same id
    # as new work: the refusal reserved nothing.
    admitted = services.coordinator.ingest(payload, principal)
    assert admitted.id == "stale-coordinated"
    assert [item["transaction_id"] for item in services.manual.inbox("acme")] == ["stale-coordinated"]
