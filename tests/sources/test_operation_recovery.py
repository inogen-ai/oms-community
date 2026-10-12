from datetime import datetime, timedelta, timezone

import pytest

from oms.domain.identity import SkillRef
from oms.sources.errors import SourceConflict, StaleMutation
from oms.sources.models import (
    ActionContext, Binding, CheckThrottle, Operation, OperationResult,
    OriginRef, ResolvedRef, Source,
)
from tests.sources.test_store_contract import source_repository  # noqa: F401
from tests.sources.test_store_contract import make_snapshot


def operation(identifier, *, key="request", digest="body"):
    return Operation(operation_id=identifier, idempotency_key=key, request_digest=digest,
        context=ActionContext(tenant_id="acme", actor_id="operator", domain_scope=("finance",),
                              capabilities=frozenset({"review:decide"})),
        result=OperationResult(operation_id=identifier, state="complete", committed=True))


def test_same_key_recovers_recorded_result_from_fresh_repository(source_repository):
    expected = operation("first")
    source_repository.reserve_operation(expected)
    if hasattr(source_repository, "_driver"):
        fresh = type(source_repository)(source_repository._driver)
    else:
        fresh = type(source_repository)(source_repository.store)
    assert fresh.reserve_operation(operation("retry")) == expected
    with pytest.raises(SourceConflict):
        fresh.reserve_operation(operation("changed", digest="different body"))
    assert fresh.operation_for_key("request", tenant_id="other", actor_id="operator") is None
    assert fresh.operation_for_key("request", tenant_id="acme", actor_id="different") is None


def test_binding_tombstone_cannot_be_reactivated_by_old_work(source_repository):
    source_repository.put_source(Source(source_id="source", tenant_id="acme",
                                         canonical_url="https://github.com/example/skills"))
    binding = Binding(origin=OriginRef(skill=SkillRef("acme", "expenses"), origin_id="origin",
        kind="github", generation=1), source_id="source", package_path="skills/expenses",
        ref=ResolvedRef(canonical_url="https://github.com/example/skills", kind="branch",
                         name="main", commit="a" * 40))
    source_repository.put_binding(binding)
    old_generation = source_repository.get_generations(binding.origin.skill)
    tombstone = Binding.model_validate(binding.model_dump() | {"active": False,
        "origin": binding.origin.model_dump() | {"generation": 2}})
    source_repository.put_binding(tombstone)
    with pytest.raises(StaleMutation):
        source_repository.put_binding(binding)
    assert not source_repository.get_binding(binding.origin.skill).active
    with pytest.raises(StaleMutation):
        source_repository.compare_generations((old_generation,))


def test_manual_throttle_is_source_wide_across_actors(source_repository):
    now = datetime.now(timezone.utc)
    first = CheckThrottle(tenant_id="acme", source_id="source", actor_id="one",
                          next_allowed_at=now + timedelta(seconds=60))
    second = CheckThrottle.model_validate(first.model_dump() | {"actor_id": "two"})
    assert source_repository.claim_manual_check(first, now=now)
    assert not source_repository.claim_manual_check(second, now=now + timedelta(seconds=1))
    next_slot = CheckThrottle.model_validate(second.model_dump() | {"next_allowed_at": now + timedelta(seconds=120)})
    assert source_repository.claim_manual_check(next_slot, now=now + timedelta(seconds=60))


@pytest.mark.parametrize("kind", ["tag", "commit"])
def test_pinned_binding_requires_a_new_generation_to_change_commit(source_repository, kind):
    source_repository.put_source(Source(source_id="source", tenant_id="acme",
                                         canonical_url="https://github.com/example/skills"))
    binding = Binding(origin=OriginRef(skill=SkillRef("acme", "expenses"), origin_id="origin",
        kind="github", generation=1), source_id="source", package_path="skills/expenses",
        ref=ResolvedRef(canonical_url="https://github.com/example/skills", kind=kind,
                         name="v1", commit="a" * 40), first_reconciliation=False)
    source_repository.put_binding(binding)
    replacement = Binding.model_validate(binding.model_dump() | {
        "ref": binding.ref.model_dump() | {"commit": "b" * 40}})
    with pytest.raises(StaleMutation):
        source_repository.put_binding(replacement)
    assert source_repository.get_binding(binding.origin.skill).ref.commit == "a" * 40


def test_update_cannot_lose_evidence_reopen_or_switch_origin(source_repository):
    from oms.sources.models import MergePlan, PlanFingerprint, SourceSnapshot, Update
    incoming = make_snapshot()
    plan = MergePlan(skill=incoming.ref.origin.skill, origin=incoming.ref.origin, base=None,
        incoming=incoming.ref, fingerprint=PlanFingerprint(content_generation=0, binding_generation=1,
            update_generation=1, policy_version="1", local_digest="local", candidate_digest="incoming"))
    update = Update(update_id="card", plan=plan, generation=1, status="open", review_item_id="review")
    with pytest.raises(SourceConflict, match="retained snapshot missing"):
        source_repository.put_update(update)
    source_repository.put_snapshot(incoming)
    source_repository.put_update(update)
    local = SourceSnapshot.model_validate(incoming.model_dump() | {
        "ref": {"snapshot_id": "local-snapshot", "origin": incoming.ref.origin.model_dump() | {
            "kind": "local", "origin_id": "local"}}})
    source_repository.put_snapshot(local)
    changed_plan = MergePlan.model_validate(plan.model_dump() | {
        "origin": local.ref.origin, "incoming": local.ref,
        "fingerprint": plan.fingerprint.model_dump() | {"update_generation": 2}})
    with pytest.raises(SourceConflict, match="update origin changed"):
        source_repository.put_update(Update.model_validate(update.model_dump() | {"plan": changed_plan, "generation": 2}))
    closed = Update.model_validate(update.model_dump() | {"status": "skipped"})
    source_repository.put_update(closed)
    with pytest.raises(SourceConflict, match="update is terminal"):
        source_repository.put_update(update)
    assert source_repository.open_update(plan.skill) is None


@pytest.mark.integration
def test_operation_and_generations_survive_a_new_process():
    import os
    import subprocess
    import sys
    from neo4j import GraphDatabase
    from testcontainers.neo4j import Neo4jContainer
    from oms.adapters.neo4j.source_store import Neo4jSourceRepository
    from oms.sources.models import Generations
    with Neo4jContainer("neo4j:5.20") as container:
        uri, password = container.get_connection_url(), container.password
        with GraphDatabase.driver(uri, auth=("neo4j", password)) as driver:
            store = Neo4jSourceRepository(driver)
            store.ensure_schema()
            store.reserve_operation(operation("durable"))
            ref = SkillRef("acme", "expenses")
            store.advance_generations(store.get_generations(ref), Generations(skill=ref, content=1, binding=0))
        script = """
import os
from neo4j import GraphDatabase
from oms.adapters.neo4j.source_store import Neo4jSourceRepository
from oms.domain.identity import SkillRef
from oms.sources.models import Operation
with GraphDatabase.driver(os.environ['SOURCE_TEST_URI'], auth=('neo4j', os.environ['SOURCE_TEST_PASSWORD'])) as driver:
    store = Neo4jSourceRepository(driver)
    old = store.operation_for_key('request', tenant_id='acme', actor_id='operator')
    assert old is not None and old.operation_id == 'durable' and old.result.committed
    retry = Operation.model_validate(old.model_dump() | {'operation_id':'retry',
        'result':old.result.model_dump() | {'operation_id':'retry'}})
    assert store.reserve_operation(retry) == old
    assert store.get_generations(SkillRef('acme','expenses')).content == 1
"""
        environment = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME"}}
        environment.update(PYTHON_DOTENV_DISABLED="1", SOURCE_TEST_URI=uri, SOURCE_TEST_PASSWORD=password)
        subprocess.run([sys.executable, "-I", "-c", script], env=environment,
                       check=True, capture_output=True, timeout=30)
