"""Conditional skill mutation within the existing workflow transaction."""
from dataclasses import dataclass
from collections.abc import Callable
from typing import Protocol, Self

from pydantic import model_validator

from oms.domain.identity import SkillRef
from oms.domain.models import SkillVersion
from oms.domain.types import SkillVersionCause
from oms.ports.graph_store import GraphStore
from oms.ports.review_queue import ReviewQueue
from oms.ports.source_store import SourceRepository
from oms.sources.models import DurableEvent, Generations, NonEmpty, Record


class MutationRequest(Record):
    operation_id: NonEmpty
    tenant_id: NonEmpty
    actor_id: NonEmpty
    affected_skills: tuple[SkillRef, ...]
    expected_generations: tuple[Generations, ...]
    request_digest: NonEmpty

    @model_validator(mode="after")
    def complete_guards(self) -> Self:
        skills = self.affected_skills
        guards = self.expected_generations
        if len(set(skills)) != len(skills):
            raise ValueError("Affected skills must be unique")
        if any(skill.tenant_id != self.tenant_id for skill in skills):
            raise ValueError("Mutation crosses tenant boundary")
        if len(guards) != len(skills) or {guard.skill for guard in guards} != set(skills):
            raise ValueError("Every affected skill requires exactly one generation guard")
        object.__setattr__(self, "affected_skills", tuple(sorted(skills)))
        object.__setattr__(self, "expected_generations", tuple(sorted(guards, key=lambda g: g.skill)))
        return self


class RequiredHistory(Protocol):
    def capture_required(self, skill_id: str, tenant_id: str, *,
                         cause: SkillVersionCause, actor: str | None = None,
                         detail: str | None = None, group_id: str | None = None,
                         restore_source: str | None = None,
                         restore_taken: str | None = None,
                         source_operation_id: str | None = None,
                         source_origin_id: str | None = None,
                         source_revision: str | None = None) -> SkillVersion | None: ...


class EventWriter(Protocol):
    def append_event(self, event: DurableEvent) -> None: ...


class RollbackParticipant(Protocol):
    def snapshot_state(self) -> object: ...
    def restore_state(self, state: object) -> None: ...


@dataclass(frozen=True)
class MutationContext:
    graph: GraphStore
    reviews: ReviewQueue
    sources: SourceRepository
    history: RequiredHistory
    events: EventWriter
    admit: Callable[[], None]
    rollback_participants: tuple[RollbackParticipant, ...] = ()


class MutationContextFactory(Protocol):
    def __call__(self, graph: GraphStore, reviews: ReviewQueue) -> MutationContext:
        """Bind every collaborator to these adapters' existing transaction.

        The graph's bound driver/session must be reused for edition extensions.
        Memory source state must participate in the same rollback snapshot.
        The factory neither opens nor commits an independent transaction.
        """
        ...
