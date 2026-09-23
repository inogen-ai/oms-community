from datetime import datetime

from oms.activity.models import AdminEvent


class InMemoryAdminEventStore:
    def __init__(self) -> None:
        self.events: dict[str, AdminEvent] = {}

    def record_admin_event(self, event: AdminEvent) -> None:
        # Keyed by id, so a retry replaces rather than appends - the same MERGE
        # semantics the Neo4j adapter gets from Cypher.
        self.events[event.id] = event

    def admin_events(self, tenant_id: str,
                     since: datetime | None = None) -> list[AdminEvent]:
        rows = [e for e in self.events.values()
                if e.tenant_id == tenant_id and (since is None or e.at >= since)]
        return sorted(rows, key=lambda e: e.at, reverse=True)
