"""Public contribution and workflow contracts, independent of an engine."""
from dataclasses import dataclass
from typing import Callable, Protocol, TypeVar, runtime_checkable

from oms.domain.models import Transaction
from oms.ports.graph_store import GraphStore
from oms.ports.review_queue import ReviewQueue

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
               operation: Callable[[GraphStore, ReviewQueue], T]) -> T: ...
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
