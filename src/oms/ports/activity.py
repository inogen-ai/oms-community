"""Administrative audit persistence.

One protocol rather than a method on `GraphStore`, for the reason the identity
ports split: this is written by one router and read by one service, and folding
it into the store protocol would make every fake in the suite grow a method it
does not use.
"""
from datetime import datetime
from typing import Protocol, runtime_checkable

from oms.activity.models import AdminEvent


@runtime_checkable
class AdminEventStore(Protocol):
    def record_admin_event(self, event: AdminEvent) -> None:
        """Append one administrative mutation. Idempotent on `id`: a retried
        request must not put the same change in the feed twice."""
        ...

    def admin_events(self, tenant_id: str,
                     since: datetime | None = None) -> list[AdminEvent]:
        """This tenant's administrative history, NEWEST FIRST, optionally
        narrowed to events at or after `since`.

        Newest first, unlike most reads in this codebase, and the exception is
        deliberate: this feeds a reverse-chronological table whose first page
        is the most recent thing that happened. Sorting it the other way and
        reversing in the caller would put the ordering promise in two places.
        """
        ...
