"""Independent adversarial checks for the Community workflow boundary.

These tests exercise public behavior and durable state, including the races
which sequential happy-path acceptance tests cannot distinguish.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from threading import Barrier, Event, Lock

import pytest

from oms.ingestion.payload_store import FilePayloadStore
from oms.ingestion.schema import ExecutionContext

from oms.adapters.memory.review_queue import InMemoryReviewQueue
from oms.adapters.memory.store import InMemoryGraphStore
from oms.community.repository import MemoryWorkflowRepository, Neo4jWorkflowRepository
from oms.community.workflow import (
    Decision, DecisionConflict, ManualDecisionError, ManualReviewDisposition,
    ManualReviewService,
)
from oms.domain.auth import INGEST_WRITE
from oms.domain.models import Edge, Principal, ReviewItem, Rule, Skill
from oms.domain.types import EdgeType, Plane, RuleStatus, SignalType, SourceRuntime, Verdict
from oms.ingestion.contribution import ContributionRequest, correction_payload
from oms.ingestion.coordinator import ContributionCoordinator
from oms.ingestion.sanitiser import RegexSanitiser
from oms.ingestion.service import IngestionService, SanitisationError, TenantMismatchError
from oms.ports.workflow import DispositionResult, effective_state
from oms.security.injection import DeterministicScreen
from oms.skills.history import SkillHistory


@pytest.fixture(scope="module")
def critic_neo4j_driver():
    from neo4j import GraphDatabase
    external_uri = os.environ.get("OMS_TEST_NEO4J_URI")
    if external_uri:
        if os.environ.get("OMS_TEST_NEO4J_DISPOSABLE") != "1":
            raise pytest.UsageError("OMS_TEST_NEO4J_DISPOSABLE=1 is required before resetting an external test database")
        password = os.environ.get("OMS_TEST_NEO4J_PASSWORD")
        if not password:
            raise pytest.UsageError("OMS_TEST_NEO4J_PASSWORD is required for the disposable test database")
        with GraphDatabase.driver(external_uri, auth=("neo4j", password)) as driver:
            driver.verify_connectivity()
            yield driver
        return
    from testcontainers.neo4j import Neo4jContainer
    with Neo4jContainer("neo4j:5.20") as container:
        with GraphDatabase.driver(container.get_connection_url(), auth=("neo4j", container.password)) as driver:
            yield driver


@dataclass
class World:
    store: object
    queue: object
    payloads: FilePayloadStore
    repository: object
    service: ManualReviewService
    coordinator: ContributionCoordinator

    def payload(self, transaction_id, body="Keep receipts for expense claims.", *,
                tenant="acme", signal=SignalType.EXPLICIT_CORRECTION):
        principal = Principal(id="local-reviewer", tenant_id=tenant,
                              scopes=frozenset({INGEST_WRITE}))
        return correction_payload(ContributionRequest(
            **{"learning" if signal is SignalType.SELF_REFLECTION else "correction": body},
            transaction_id=transaction_id, signal_type=signal,
            source_ref="session:critic", skill_hint="Expenses"), principal, SourceRuntime.MCP), principal

    def ingest(self, transaction_id, body="Keep receipts for expense claims.", **kwargs):
        payload, principal = self.payload(transaction_id, body, **kwargs)
        return self.coordinator.ingest(payload, principal)

    def skill(self, skill_id="expenses", tenant="acme"):
        skill = Skill(id=skill_id, name="Expenses", description="Expense workflow", domain="finance",
                      tenant_id=tenant)
        self.store.upsert_skill(skill)
        return skill

    def decide(self, transaction_id, action="create", *, body="Keep receipts for expense claims.",
               skill_ids=None, rule_id=None, tenant="acme"):
        return self.service.decide(transaction_id, tenant, "local-reviewer", Decision(
            action=action, body=body, skill_ids=skill_ids if skill_ids is not None else ["expenses"],
            rule_id=rule_id))


@pytest.fixture(params=["memory", pytest.param("neo4j", marks=pytest.mark.integration)])
def world(request, tmp_path):
    if request.param == "memory":
        store, queue = InMemoryGraphStore(), InMemoryReviewQueue()
        repository = MemoryWorkflowRepository(store, queue)
    else:
        from oms.adapters.neo4j.review_queue import Neo4jReviewQueue
        from oms.adapters.neo4j.store import Neo4jGraphStore
        driver = request.getfixturevalue("critic_neo4j_driver")
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()
        store, queue = Neo4jGraphStore(driver), Neo4jReviewQueue(driver)
        store.ensure_schema()
        repository = Neo4jWorkflowRepository(driver, store, queue)
    payloads = FilePayloadStore(tmp_path / "payloads")
    disposition = ManualReviewDisposition(repository, payloads)
    # The public root omits the old compiler-facing safety queue; the manual
    # disposition owns the visible review item for both safe and held input.
    ingestion = IngestionService(store, RegexSanitiser(), payloads)
    return World(store, queue, payloads, repository, ManualReviewService(repository, payloads),
                 ContributionCoordinator(ingestion, disposition, repository))


@pytest.mark.parametrize("operation", ["put", "get", "delete", "quarantine", "delete_quarantine"])
def test_payload_identifiers_cannot_escape_the_payload_root(tmp_path, operation):
    root = tmp_path / "payloads"
    root.mkdir()
    (root / "_flagged_errors").mkdir()
    outside = tmp_path / "outside.json"
    original = ExecutionContext(user_input="", agent_raw_output="", user_correction="Original")
    outside.write_text(original.model_dump_json(), encoding="utf-8")
    flagged_outside = tmp_path / "outside.txt"
    flagged_outside.write_text("Original quarantine", encoding="utf-8")
    store = FilePayloadStore(root)
    context = ExecutionContext(user_input="", agent_raw_output="", user_correction="Changed")

    # Quarantine lives one directory deeper than an ordinary payload.
    identifier = "../../outside" if "quarantine" in operation else "../outside"
    try:
        if operation == "put":
            store.put(identifier, context)
        elif operation == "quarantine":
            store.quarantine(identifier, "Changed quarantine")
        elif operation == "get":
            assert store.get(identifier) is None, "an unsafe identifier read outside the root"
        else:
            getattr(store, operation)(identifier)
    except ValueError:
        pass  # Refusing an unsafe ID is the intended public contract.

    assert outside.read_text(encoding="utf-8") == original.model_dump_json()
    assert flagged_outside.read_text(encoding="utf-8") == "Original quarantine"


def test_payload_path_cannot_follow_an_existing_symlink_outside_root(tmp_path):
    root = tmp_path / "payloads"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("Original", encoding="utf-8")
    (root / "safe-id.json").symlink_to(outside)
    store = FilePayloadStore(root)
    context = ExecutionContext(user_input="", agent_raw_output="", user_correction="Changed")

    try:
        store.put("safe-id", context)
    except ValueError:
        pass

    assert outside.read_text(encoding="utf-8") == "Original"


def test_external_database_requires_disposable_acknowledgement_before_connecting(monkeypatch):
    monkeypatch.setenv("OMS_TEST_NEO4J_URI", "bolt://must-not-connect.invalid:7687")
    monkeypatch.setenv("OMS_TEST_NEO4J_PASSWORD", "test-only")
    monkeypatch.delenv("OMS_TEST_NEO4J_DISPOSABLE", raising=False)
    fixture = critic_neo4j_driver.__wrapped__()
    with pytest.raises(pytest.UsageError, match="DISPOSABLE=1"):
        next(fixture)


def test_manual_approval_records_one_rule_lineage_decision_and_version(world):
    world.skill()
    world.ingest("approval")
    result = world.decide("approval")
    repeated = world.decide("approval")

    assert result == repeated
    assert result.state == "applied" and result.rule_id == "rule-approval"
    rule = world.store.get_rule(result.rule_id)
    assert rule.corroboration_count == 1
    assert [txn.id for txn in world.store.lineage(rule.id)] == ["approval"]
    assert [skill.id for skill in world.store.skills_for_rule(rule.id)] == ["expenses"]
    assert world.service.inbox("acme") == []
    item = world.queue.get("manual-approval")
    assert item.resolved and item.decided_by == "local-reviewer" and item.decided_at
    assert world.store.latest_skill_version("acme", "expenses") is not None
    transaction = world.store.get_transaction("approval")
    assert transaction.person_id is None and transaction.assurance is None and not transaction.admitted


@pytest.mark.parametrize("decision", ["reject", "reinforce"])
def test_conflicting_terminal_decision_cannot_change_the_first_result(world, decision):
    world.skill()
    world.ingest("terminal")
    first = world.decide("terminal")
    with pytest.raises(DecisionConflict):
        world.decide("terminal", decision, rule_id=first.rule_id)
    assert world.store.get_rule(first.rule_id).corroboration_count == 1
    assert effective_state(world.store.get_transaction("terminal"), world.store) == "applied"


@pytest.mark.parametrize("target", ["missing", "foreign"])
def test_missing_or_foreign_target_rolls_back_the_whole_decision(world, target):
    world.skill()
    if target == "foreign":
        world.skill(target, tenant="other-workspace")
    world.ingest("bad-target")
    with pytest.raises(ManualDecisionError):
        world.decide("bad-target", skill_ids=["expenses", target])

    assert world.store.get_rule("rule-bad-target") is None
    assert effective_state(world.store.get_transaction("bad-target"), world.store) == "awaiting_manual_review"
    assert not world.queue.get("manual-bad-target").resolved
    assert world.store.latest_skill_version("acme", "expenses") is None


def test_rule_id_collision_does_not_overwrite_foreign_data(world):
    world.skill()
    world.store.upsert_rule(Rule(id="rule-collision", body="Foreign body", tenant_id="other"))
    world.ingest("collision")
    with pytest.raises(DecisionConflict):
        world.decide("collision")
    foreign = world.store.get_rule("rule-collision")
    assert foreign.tenant_id == "other" and foreign.body == "Foreign body"
    assert not world.queue.get("manual-collision").resolved


def test_deciding_a_foreign_transaction_does_not_resolve_or_disclose_it(world):
    world.skill()
    world.ingest("private-correction", tenant="other")
    with pytest.raises(ManualDecisionError):
        world.decide("private-correction")
    assert not world.queue.get("manual-private-correction").resolved
    assert world.service.inbox("acme") == []


def test_strict_history_failure_rolls_back_rule_edges_and_review(world, monkeypatch):
    world.skill()
    world.ingest("history-failure")
    original_capture = SkillHistory.capture_required

    def fail_history(*args, **kwargs):
        raise RuntimeError("injected durable history failure")

    monkeypatch.setattr(SkillHistory, "capture_required", fail_history)
    with pytest.raises(RuntimeError, match="injected durable history failure"):
        world.decide("history-failure")
    assert world.store.get_rule("rule-history-failure") is None
    assert not world.queue.get("manual-history-failure").resolved
    assert effective_state(world.store.get_transaction("history-failure"), world.store) == "awaiting_manual_review"

    monkeypatch.setattr(SkillHistory, "capture_required", original_capture)
    assert world.decide("history-failure").state == "applied"


def test_failed_disposition_is_completed_by_replayed_request(world, monkeypatch):
    world.skill()
    real_accept = world.coordinator.disposition.accept

    def fail_disposition(transaction):
        raise RuntimeError("injected disposition outage")

    monkeypatch.setattr(world.coordinator.disposition, "accept", fail_disposition)
    with pytest.raises(RuntimeError, match="injected disposition outage"):
        world.ingest("recover-retry")
    assert world.store.get_transaction("recover-retry") is not None
    assert world.queue.get("manual-recover-retry") is None

    monkeypatch.setattr(world.coordinator.disposition, "accept", real_accept)
    assert effective_state(world.ingest("recover-retry"), world.store) == "awaiting_manual_review"
    assert len(world.service.inbox("acme")) == 1


def test_queue_write_failure_leaves_the_received_transaction_recoverable(world, monkeypatch):
    original_enqueue = type(world.queue).enqueue
    failed_once = False

    def enqueue_then_fail(queue, item):
        nonlocal failed_once
        original_enqueue(queue, item)
        if item.kind == "manual_correction" and not failed_once:
            failed_once = True
            raise RuntimeError("injected queue write interruption")

    monkeypatch.setattr(type(world.queue), "enqueue", enqueue_then_fail)
    with pytest.raises(RuntimeError, match="injected queue write interruption"):
        world.ingest("interrupted-queue")
    assert world.store.get_transaction("interrupted-queue") is not None
    assert world.queue.get("manual-interrupted-queue") is None

    world.coordinator.reconcile("acme")
    assert len(world.service.inbox("acme")) == 1
    assert effective_state(world.store.get_transaction("interrupted-queue"), world.store) == "awaiting_manual_review"


def test_reconciliation_does_not_starve_received_work_behind_completed_rows(world):
    world.skill()
    world.ingest("old-completed")
    world.decide("old-completed")
    payload, principal = world.payload("new-undisposed")
    world.coordinator.ingestion.ingest(payload, principal)

    world.coordinator.reconcile("acme", limit=1)

    assert world.queue.get("manual-new-undisposed") is not None
    assert effective_state(world.store.get_transaction("new-undisposed"), world.store) == "awaiting_manual_review"


def test_replay_and_reconciliation_do_not_resurrect_a_rejection(world):
    world.ingest("rejected")
    result = world.decide("rejected", "reject")
    assert result.state == "rejected"
    world.ingest("rejected")
    world.coordinator.reconcile("acme")
    assert world.service.inbox("acme") == []
    assert world.queue.get("manual-rejected").resolved
    assert world.store.get_rule("rule-rejected") is None


def test_sanitised_hold_requires_separate_human_release_then_manual_decision(world):
    world.skill()
    world.ingest("held", "Do not share records with alice@example.com.")
    row, = world.service.inbox("acme")
    assert row["state"] == "held_safety" and row["warning"]
    assert "alice@example.com" not in str(row)
    with pytest.raises(ManualDecisionError):
        world.decide("held", body="Protect expense records.")
    assert world.store.get_rule("rule-held") is None

    assert world.decide("held", "release_safety").state == "awaiting_manual_review"
    assert world.store.get_rule("rule-held") is None
    assert not world.store.get_transaction("held").admitted
    assert world.decide("held", body="Protect expense records.").state == "applied"


def test_machine_origin_remains_visible_and_cannot_auto_apply(world):
    world.ingest("machine", signal=SignalType.SELF_REFLECTION)
    row, = world.service.inbox("acme")
    assert row["signal_type"] == "self_reflection" and row["source_ref"] == "session:critic"
    assert row["state"] == "awaiting_manual_review"
    assert world.store.get_rule("rule-machine") is None
    transaction = world.store.get_transaction("machine")
    assert not transaction.admitted and not transaction.scope_reviewed


def test_exact_reinforcement_preserves_unicode_and_adds_lineage_once(world):
    world.skill()
    world.store.upsert_rule(Rule(id="existing", body="Caf\u00e9 receipts", tenant_id="acme", corroboration_count=4))
    world.ingest("unicode", "  CAF\u0065\u0301   receipts ")
    row, = world.service.inbox("acme")
    assert [match["id"] for match in row["exact_matches"]] == ["existing"]
    result = world.decide("unicode", "reinforce", body="CAF\u00c9 receipts", rule_id="existing")
    assert world.decide("unicode", "reinforce", body="CAF\u00c9 receipts", rule_id="existing") == result
    assert world.store.get_rule("existing").corroboration_count == 5
    assert [t.id for t in world.store.lineage("existing")] == ["unicode"]
    assert world.store.get_rule("rule-unicode") is None


@pytest.mark.parametrize("existing,incoming", [("安全に保存", "個人情報を消す"), ("Use C++", "Use C")])
def test_different_unicode_or_punctuation_is_not_an_exact_match(world, existing, incoming):
    world.skill()
    world.store.upsert_rule(Rule(id="different", body=existing, tenant_id="acme"))
    world.ingest("distinct", incoming)
    row, = world.service.inbox("acme")
    assert row["exact_matches"] == []
    with pytest.raises(ManualDecisionError):
        world.decide("distinct", "reinforce", body=incoming, rule_id="different")
    assert world.store.get_rule("different").corroboration_count == 1


@pytest.mark.parametrize("rule_status,plane", [(RuleStatus.RETIRED, Plane.DATA), (RuleStatus.ACTIVE, Plane.CONTROL)])
def test_exact_match_cannot_revive_retired_or_control_plane_rules(world, rule_status, plane):
    world.skill()
    body = "Keep receipts for expense claims."
    world.store.upsert_rule(Rule(id="not-publishable", body=body, tenant_id="acme", status=rule_status, plane=plane))
    world.ingest("not-publishable")
    assert world.service.inbox("acme")[0]["exact_matches"] == []
    with pytest.raises(ManualDecisionError):
        world.decide("not-publishable", "reinforce", rule_id="not-publishable")


def test_reinforcing_shared_rule_versions_every_affected_skill(world):
    world.skill("expenses")
    world.skill("audit")
    world.ingest("original")
    world.decide("original", skill_ids=["audit"])
    original_version = world.store.latest_skill_version("acme", "audit")
    world.ingest("reinforcing")
    world.decide("reinforcing", "reinforce", rule_id="rule-original", skill_ids=["expenses"])

    assert world.store.latest_skill_version("acme", "audit").id != original_version.id
    assert world.store.latest_skill_version("acme", "expenses") is not None


def test_concurrent_replays_commit_one_decision(world):
    world.skill()
    world.ingest("concurrent")
    barrier = Barrier(8)

    def approve(_):
        barrier.wait(timeout=10)
        return world.decide("concurrent")

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(approve, range(8)))
    assert all(result == results[0] for result in results)
    assert world.store.get_rule("rule-concurrent").corroboration_count == 1
    assert len(world.store.lineage("rule-concurrent")) == 1


def test_concurrent_different_decisions_have_one_winner(world):
    world.skill()
    world.ingest("different-reviewers")
    barrier = Barrier(2)

    def decide(action):
        barrier.wait(timeout=10)
        try:
            return world.decide("different-reviewers", action)
        except DecisionConflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(decide, ["create", "reject"]))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert effective_state(world.store.get_transaction("different-reviewers"), world.store) == winners[0].state
    rule = world.store.get_rule("rule-different-reviewers")
    assert (rule is not None) == (winners[0].state == "applied")
    assert world.queue.get("manual-different-reviewers").resolved


def test_concurrent_distinct_reinforcements_do_not_lose_increments(world):
    world.skill()
    world.store.upsert_rule(Rule(id="shared", body="Keep receipts for expense claims.", tenant_id="acme"))
    for number in range(6):
        world.ingest(f"reinforce-{number}")
    barrier = Barrier(6)

    def reinforce(number):
        barrier.wait(timeout=10)
        return world.decide(f"reinforce-{number}", "reinforce", rule_id="shared")

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(reinforce, range(6)))
    assert all(result.state == "applied" for result in results)
    assert world.store.get_rule("shared").corroboration_count == 7
    assert len(world.store.lineage("shared")) == 6


def test_manual_review_cannot_be_taken_by_the_automatic_compiler(world):
    world.ingest("manual-only")
    assert "manual-only" not in {txn.id for txn in world.store.pending_transactions(tenant_id="acme")}
    assert not world.store.claim_compile("manual-only", "automatic-worker")
    assert not world.queue.get("manual-manual-only").resolved


def test_noop_disposition_cannot_report_an_actionable_state_without_persisting_it(world):
    class NoopDisposition:
        def accept(self, transaction):
            return DispositionResult(transaction.id, "awaiting_manual_review")

    world.coordinator.disposition = NoopDisposition()
    with pytest.raises(RuntimeError, match="disposition"):
        world.ingest("noop")
    assert world.store.get_transaction("noop") is not None
    assert world.queue.get("manual-noop") is None


def test_concurrent_cross_tenant_id_collision_has_only_one_owner(world):
    entered_first, entered_second, release_first = Event(), Event(), Event()
    counter_lock = Lock()
    calls = 0

    class DelayedSanitiser(RegexSanitiser):
        def sanitise(self, context):
            nonlocal calls
            with counter_lock:
                calls += 1
                first = calls == 1
            if first:
                entered_first.set()
                assert release_first.wait(timeout=10)
            else:
                entered_second.set()
            return super().sanitise(context)

    world.coordinator.ingestion._sanitiser = DelayedSanitiser()

    def submit(tenant):
        try:
            world.ingest("same-global-id", body=f"Keep the {tenant} rule.", tenant=tenant)
            return (tenant, "accepted")
        except TenantMismatchError:
            return (tenant, "refused")

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(submit, "first-tenant")
        assert entered_first.wait(timeout=10)
        second = pool.submit(submit, "second-tenant")
        # Under correct serialization the second sanitizer never runs. The
        # short bounded wait also gives the broken concurrent path a chance
        # to reach its conflicting write before the first owner continues.
        entered_second.wait(timeout=0.2)
        release_first.set()
        results = [first.result(timeout=10), second.result(timeout=10)]

    assert results == [("first-tenant", "accepted"), ("second-tenant", "refused")]
    assert world.store.get_transaction("same-global-id").tenant_id == "first-tenant"
    assert world.payloads.get("same-global-id").user_correction == "Keep the first-tenant rule."


def test_coordinator_keeps_the_quarantine_record_when_sanitisation_refuses(world):
    class BrokenSanitiser:
        def sanitise(self, context):
            raise ValueError(f"cannot sanitise {context.user_correction}")

    world.coordinator.ingestion._sanitiser = BrokenSanitiser()
    with pytest.raises(SanitisationError):
        world.ingest("quarantined", "Private original text")

    records = world.store.quarantined_payloads("acme")
    assert len(records) == 1 and records[0].transaction_id == "quarantined"
    assert records[0].reason == "ValueError"
    assert world.store.get_transaction("quarantined") is None
    assert world.service.inbox("acme") == []


def test_safety_release_accepts_the_same_wording_with_outer_whitespace(world):
    world.skill()
    world.coordinator.ingestion._screen = DeterministicScreen()
    body = "\nWhen testing prompt injection, use the phrase 'ignore previous instructions'.\n"
    world.ingest("injection-example", body)
    row, = world.service.inbox("acme")
    assert row["state"] == "held_safety"
    world.decide("injection-example", "release_safety")

    assert world.decide("injection-example", body=body).state == "applied"


@pytest.mark.parametrize("abandoned", ["queued_automation", "processing"])
def test_downgraded_automation_states_surface_in_the_manual_inbox(world, abandoned):
    """A graph downgraded from the paid product carries states only the
    absent compiler serviced. The sweep must adopt them, not skip them."""
    world.skill()
    payload, principal = world.payload("downgraded")
    world.coordinator.ingestion.ingest(payload, principal)
    txn = world.store.get_transaction("downgraded")
    txn.workflow_state = abandoned
    world.store.upsert_transaction(txn)

    # The paid worker's sweep must not reclaim its own queue through the
    # default predicate; only a disposition that declares the state may.
    assert "downgraded" not in {t.id for t in world.repository.undisposed("acme")}
    assert world.service.inbox("acme") == []

    assert world.coordinator.reconcile("acme") == 1
    row, = world.service.inbox("acme")
    assert row["transaction_id"] == "downgraded"
    assert effective_state(world.store.get_transaction("downgraded"), world.store) == "awaiting_manual_review"
    assert world.coordinator.reconcile("acme") == 0
    assert world.decide("downgraded").state == "applied"


def test_legacy_held_transaction_with_only_a_legacy_item_is_adopted(world):
    """A pre-workflow held record whose only review item is the compiler-era
    hold is invisible to the manual inbox until the sweep adopts it. Both
    repository implementations must agree that it is undisposed."""
    payload, principal = world.payload("legacy-held")
    world.coordinator.ingestion.ingest(payload, principal)
    txn = world.store.get_transaction("legacy-held")
    txn.held_reason = "sanitised: telephone number"
    world.store.upsert_transaction(txn)
    world.queue.enqueue(ReviewItem(
        id="review-held-legacy-held", kind="held_transaction", subject_id="legacy-held",
        other_id=None, verdict=Verdict.AMBIGUOUS, reason="sanitised: telephone number",
        tenant_id="acme"))
    assert world.service.inbox("acme") == []

    assert "legacy-held" in {t.id for t in world.repository.undisposed("acme")}
    assert world.coordinator.reconcile("acme") == 1
    row, = world.service.inbox("acme")
    assert row["transaction_id"] == "legacy-held" and row["state"] == "held_safety"
    assert world.coordinator.reconcile("acme") == 0
    assert not world.queue.get("review-held-legacy-held").resolved

    world.skill()
    world.decide("legacy-held", "release_safety")
    assert world.queue.get("review-held-legacy-held").resolved
    assert world.decide("legacy-held").state == "applied"


def test_ingest_verification_tolerates_an_immediate_concurrent_decision(world):
    """A reviewer deciding the item between the disposition write and the
    coordinator's verification read is success, not a disposition failure."""
    world.skill()
    inner = world.coordinator.disposition

    class DecideBeforeVerification:
        def accept(self, transaction):
            result = inner.accept(transaction)
            world.decide(transaction.id)
            return result

    world.coordinator.disposition = DecideBeforeVerification()
    returned = world.ingest("raced-decision")
    assert effective_state(returned, world.store) == "applied"
    assert world.store.get_rule("rule-raced-decision") is not None


def test_a_replayed_decision_with_reordered_skills_returns_the_recorded_result(world):
    world.skill("expenses")
    world.skill("audit")
    world.ingest("reordered")
    first = world.decide("reordered", skill_ids=["expenses", "audit"])
    replay = world.decide("reordered", skill_ids=["audit", "expenses", "audit"])
    assert first == replay and first.state == "applied"
    assert world.store.get_rule(first.rule_id).corroboration_count == 1
