from datetime import datetime, timezone

import pytest

from oms.sources.errors import SourceConflict
from oms.sources.models import DurableEvent


def test_exhausted_delivery_does_not_starve_later_pending_events(mutation_case):
    sources = mutation_case.sources
    for index in range(11):
        event = DurableEvent(event_id=f"early-{index:02}", tenant_id="acme", operation_id="op",
                             action="source_apply", skills=(mutation_case.skill,))
        sources.append_event(event)
        sources.put_event(event.model_copy(update={"state": "failed", "attempts": 5, "error_code": "delivery_exhausted"}))
    sources.append_event(DurableEvent(event_id="later", tenant_id="acme", operation_id="op2",
        action="source_apply", skills=(mutation_case.skill,)))
    assert [row.event_id for row in sources.pending_events(tenant_id="acme", now=datetime.now(timezone.utc), limit=10)] == ["later"]


def test_delivery_updates_preserve_original_actor_time_and_terminal_state(mutation_case):
    sources = mutation_case.sources
    event = DurableEvent(event_id="event", tenant_id="acme", operation_id="op", action="source_apply",
        skills=(mutation_case.skill,), actor_id="reviewer", created_at=datetime(2026, 10, 6, tzinfo=timezone.utc))
    sources.append_event(event)
    with pytest.raises(SourceConflict):
        sources.put_event(event.model_copy(update={"actor_id": "different"}))
    exhausted = event.model_copy(update={"state": "failed", "attempts": 5})
    sources.put_event(exhausted)
    assert sources.get_event("event", tenant_id="acme") == exhausted
    with pytest.raises(SourceConflict):
        sources.put_event(exhausted.model_copy(update={"state": "pending", "attempts": 6}))
    assert sources.get_event("event", tenant_id="other") is None
