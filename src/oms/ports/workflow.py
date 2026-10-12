"""Public contribution and workflow contracts, independent of an engine."""
from dataclasses import dataclass
from typing import Callable, Protocol, TypeVar, runtime_checkable

from oms.domain.models import Transaction
from oms.domain.identity import SkillRef
from oms.ports.graph_store import GraphStore
from oms.ports.mutation import MutationContext, MutationContextFactory, MutationRequest, RollbackParticipant
from oms.ports.review_queue import ReviewQueue
from oms.sources.models import Generations

T = TypeVar("T")
TERMINAL_STATES = frozenset({"applied", "rejected", "failed"})


@dataclass(frozen=True)
class DispositionResult:
    transaction_id: str
    state: str


@runtime_checkable
class TransactionDisposition(Protocol):
    def accept(self, transaction: Transaction) -> DispositionResult: ...


@runtime_checkable
class WorkflowRepository(Protocol):
    store: GraphStore
    queue: ReviewQueue

    def atomic(self, transaction_id: str, tenant_id: str,
               operation: Callable[[GraphStore, ReviewQueue], T], *,
               expected_generations: tuple["Generations", ...] = (),
               rollback_participants: tuple["RollbackParticipant", ...] = ()) -> T: ...
    def metadata(self, transaction_id: str, tenant_id: str,
                 operation: Callable[[GraphStore, ReviewQueue], T], *,
                 rollback_participants: tuple[RollbackParticipant, ...] = ()) -> T:
        """Serialize queries/source bookkeeping without scanning skill content.

        The callback must not mutate skill content or live ownership claims.
        Authoritative writes use atomic or atomic_skill_change instead.
        """
        ...
    def query(self, transaction_id: str, tenant_id: str,
              operation: Callable[[GraphStore, ReviewQueue], T]) -> T:
        """Run a read-only callback in one read transaction, taking no workflow lock.

        Nothing is copied for rollback; a callback that writes is refused by
        Neo4j's read access mode and must use metadata or atomic instead.
        """
        ...
    def atomic_with_receipt(self, transaction_id: str, tenant_id: str,
                           operation: Callable[[GraphStore, ReviewQueue], T], *,
                           expected_generations: tuple[Generations, ...] = (),
                           rollback_participants: tuple[RollbackParticipant, ...] = ()) -> tuple[
                               T, tuple[Generations, ...], tuple[SkillRef, ...]]:
        """Also return the committed generations of the guarded and changed skills,
        and which of those skills still exist."""
        ...
    def atomic_skill_change(self, request: MutationRequest,
                            factory: MutationContextFactory,
                            operation: Callable[[MutationContext], T]) -> T:
        """Extend atomic with generation guards and bound source/history writes."""
        ...
    def transactions(self, tenant_id: str, limit: int = 1000) -> list[Transaction]: ...
    def undisposed(self, tenant_id: str, limit: int = 1000,
                   adopt: frozenset[str] = frozenset()) -> list[Transaction]: ...
    def tenants(self) -> list[str]: ...


def effective_state(transaction: Transaction, store: GraphStore) -> str:
    """Old records keep their meaning until a workflow explicitly adopts them."""
    if transaction.workflow_state:
        return transaction.workflow_state
    if store.rules_derived_from([transaction.id]).get(transaction.id):
        return "applied"
    if transaction.held_reason:
        return "held_safety"
    return store.workflow_state_for(transaction.id)
