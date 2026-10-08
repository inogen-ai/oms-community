from datetime import datetime
from copy import deepcopy
from threading import RLock

from oms.activity.models import AdminEvent


class InMemoryAdminEventStore:
    def __init__(self) -> None:
        self.events: dict[str, AdminEvent] = {}
        self._mutex = RLock()
        self._workflow_mutex = None

    def bind_workflow_lock(self, mutex) -> None:
        """Bind during composition so rollback and unrelated event writes serialize."""
        if self._workflow_mutex is not None and self._workflow_mutex is not mutex:
            raise ValueError("Audit store is already bound to another workflow")
        self._workflow_mutex = self._mutex = mutex

    def snapshot_state(self) -> object:
        with self._mutex:
            return deepcopy(self.events)

    def restore_state(self, state: object) -> None:
        if not isinstance(state, dict):
            raise TypeError("Invalid event rollback state")
        with self._mutex:
            self.events = deepcopy(state)

    def record_admin_event(self, event: AdminEvent) -> None:
        # Keyed by id, so a retry replaces rather than appends - the same MERGE
        # semantics the Neo4j adapter gets from Cypher.
        with self._mutex:
            self.events[event.id] = event

    def admin_events(self, tenant_id: str,
                     since: datetime | None = None) -> list[AdminEvent]:
        with self._mutex:
            rows = [e for e in self.events.values()
                    if e.tenant_id == tenant_id and (since is None or e.at >= since)]
        return sorted(rows, key=lambda e: e.at, reverse=True)
