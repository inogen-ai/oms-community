"""Neo4j administrative audit persistence.

The one node type this feature writes. Everything else in the activity feed is
read from what the graph already records, which is why there is no adapter here
for the other three sources.
"""
from datetime import datetime

from oms.activity.models import AdminAction, AdminEvent


class Neo4jAdminEventStore:
    def __init__(self, driver, *, event_factory=AdminEvent, action_factory=AdminAction) -> None:
        self._driver = driver
        self._event_factory = event_factory
        self._action_factory = action_factory

    def record_admin_event(self, event: AdminEvent) -> None:
        # MERGE on id for the same reason record_usage_event does: a client
        # that retries a call it already logged must not inflate the feed.
        # `:Entity` matches the label every other node in this graph carries.
        with self._driver.session() as session:
            session.run(
                "MERGE (a:AdminEvent {id:$id}) "
                "SET a:Entity, a.tenant_id=$tid, a.at=$at, a.action=$action, "
                "a.subject_id=$subject, a.subject_kind=$subject_kind, "
                "a.actor_person_id=$actor, a.before=$before, a.after=$after",
                id=event.id, tid=event.tenant_id, at=event.at,
                action=event.action.value, subject=event.subject_id,
                subject_kind=event.subject_kind,
                actor=event.actor_person_id, before=event.before,
                after=event.after,
            )

    def admin_events(self, tenant_id: str,
                     since: datetime | None = None) -> list[AdminEvent]:
        with self._driver.session() as session:
            recs = session.run(
                "MATCH (a:AdminEvent {tenant_id:$tid}) "
                "WHERE ($since IS NULL OR a.at >= $since) "
                "RETURN a ORDER BY a.at DESC",
                tid=tenant_id, since=since,
            )
            return [self._event_from_node(r["a"]) for r in recs]

    def _event_from_node(self, n) -> AdminEvent:
        return self._event_factory(
            id=n["id"], tenant_id=n["tenant_id"], at=n["at"].to_native(),
            action=self._action_factory(n["action"]),
            subject_id=n["subject_id"],
            # Rows written before administration could touch anything but a
            # person are exactly that: people.
            subject_kind=n.get("subject_kind") or "person",
            # `.get`, not `[...]`: an unproven actor is stored as null and a
            # missing property must read as None rather than raising.
            actor_person_id=n.get("actor_person_id"),
            before=n.get("before"), after=n.get("after"),
        )
