from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from oms.adapters.memory.review_queue import InMemoryReviewQueue
from oms.adapters.memory.store import InMemoryGraphStore
from oms.community.repository import MemoryWorkflowRepository, Neo4jWorkflowRepository
from oms.domain.identity import SkillRef
from oms.domain.models import Skill
from oms.domain.types import SkillVersionCause
from oms.ports.mutation import MutationContext, MutationRequest
from oms.publish.publisher import Publisher
from oms.skills.history import SkillHistory
from oms.skills.service import SkillAdminService
from oms.sources.models import ActionContext, DurableEvent, Operation, OperationResult, SkillOutcome
from oms.sources.mutation import source_repository
from tests.community.test_critic_workflow import critic_neo4j_driver  # noqa: F401


@dataclass
class MutationCase:
    store: object
    queue: object
    repository: object
    skill: SkillRef = SkillRef("acme", "expenses")
    denied: bool = False

    @property
    def sources(self):
        return source_repository(self.store)

    def capture_generation(self):
        return self.sources.get_generations(self.skill)

    def description(self):
        return self.store.get_skill(self.skill.skill_id, tenant_id=self.skill.tenant_id).description

    def edit_description(self, text):
        return self.repository.atomic("editor", "acme", lambda graph, queue:
            SkillAdminService(store=graph).update_metadata("expenses", "acme", description=text))

    def factory(self, graph, reviews):
        def admit():
            if self.denied:
                raise PermissionError("Authority revoked")
        sources = source_repository(graph)
        history = SkillHistory(store=graph, publisher=Publisher(graph))
        return MutationContext(graph=graph, reviews=reviews, sources=sources,
                               history=history, events=sources, admit=admit,
                               rollback_participants=(history,))

    def request(self, generation, *, operation_id="prepared", digest="request"):
        return MutationRequest(operation_id=operation_id, tenant_id="acme", actor_id="reviewer",
            affected_skills=(self.skill,), expected_generations=(generation,), request_digest=digest)

    def apply_prepared(self, generation, text, *, operation_id="prepared", digest="request", fault=None):
        request = self.request(generation, operation_id=operation_id, digest=digest)

        def apply(context):
            SkillAdminService(store=context.graph).update_metadata("expenses", "acme", description=text)
            context.history.capture_required("expenses", "acme", cause=SkillVersionCause.CONSOLE_EDIT)
            context.events.append_event(DurableEvent(event_id=operation_id, tenant_id="acme",
                operation_id=operation_id, action="source_update", skills=(self.skill,)))
            result = OperationResult(operation_id=operation_id, state="complete", committed=True,
                outcomes=(SkillOutcome(skill=self.skill, state="applied"),))
            context.sources.reserve_operation(Operation(operation_id=operation_id,
                context=ActionContext(tenant_id="acme", actor_id="reviewer", domain_scope=("finance",),
                    capabilities=frozenset({"review:decide"})), idempotency_key=operation_id,
                request_digest=digest, result=result))
            if fault:
                fault(context)
            return result
        return self.repository.atomic_skill_change(request, self.factory, apply)

    def snapshot(self):
        return (self.description(), self.capture_generation(),
                self.store.skill_versions("acme", "expenses"),
                self.queue.pending("acme"),
                self.sources.get_operation("prepared", tenant_id="acme"),
                self.sources.pending_events(tenant_id="acme", now=datetime.now(timezone.utc)))


@pytest.fixture(params=["memory", pytest.param("neo4j", marks=pytest.mark.integration)])
def mutation_case(request):
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
        source_repository(store).ensure_schema()
        repository = Neo4jWorkflowRepository(driver, store, queue)
    store.upsert_skill(Skill(id="expenses", name="Expenses", description="original", domain="finance", tenant_id="acme"))
    return MutationCase(store, queue, repository)


@pytest.fixture
def source_world(mutation_case, tmp_path):
    from tests.sources.source_world import SourceWorld
    return SourceWorld(mutation_case, tmp_path)
