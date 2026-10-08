from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event

import pytest

from oms.activity.models import AdminAction, AdminEvent
from oms.adapters.memory.activity import InMemoryAdminEventStore
from oms.adapters.memory.review_queue import InMemoryReviewQueue
from oms.adapters.memory.store import InMemoryGraphStore
from oms.community.repository import MemoryWorkflowRepository


def test_failed_mutation_cannot_erase_an_unrelated_concurrent_audit_event():
    repository = MemoryWorkflowRepository(InMemoryGraphStore(), InMemoryReviewQueue())
    events = InMemoryAdminEventStore()
    events.bind_workflow_lock(repository._mutex)
    written, release, attempting, finished = Event(), Event(), Event(), Event()

    def event(identifier):
        return AdminEvent(id=identifier, tenant_id="acme", at=datetime.now(timezone.utc),
                          action=AdminAction.SKILL_EDITED, subject_id="expenses")

    def fail(graph, queue):
        events.record_admin_event(event("rolled-back"))
        written.set()
        assert release.wait(5)
        raise OSError("Commit failed")

    def record_unrelated():
        attempting.set()
        events.record_admin_event(event("unrelated"))
        finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        transaction = pool.submit(repository.atomic, "edit", "acme", fail, rollback_participants=(events,))
        assert written.wait(5)
        unrelated = pool.submit(record_unrelated)
        assert attempting.wait(5)
        assert not finished.wait(0.05)
        release.set()
        with pytest.raises(OSError):
            transaction.result(timeout=5)
        unrelated.result(timeout=5)
    assert [row.id for row in events.admin_events("acme")] == ["unrelated"]
