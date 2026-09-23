from datetime import datetime
from typing import Protocol, runtime_checkable

from oms.domain.models import ReviewItem


@runtime_checkable
class ReviewQueue(Protocol):
    def enqueue(self, item: ReviewItem) -> None: ...
    def enqueue_once(self, item: ReviewItem) -> None:
        """Atomically create an absent item; preserve any existing item exactly.

        Import retries use this operation so neither a pending review's
        baseline nor a recorded decision can be replaced by a repeated source.
        """
        ...
    def pending(self, tenant_id: str) -> list[ReviewItem]: ...
    def get(self, item_id: str) -> ReviewItem | None: ...
    def resolve(self, item_id: str, resolution: str,
                decided_by: str | None = None) -> ReviewItem:
        """Mark an item resolved with the given resolution ("approved" /
        "rejected"). Raises KeyError when no such item exists and ValueError
        when it is already resolved.

        `decided_by` is the reviewer's person_id, or None when no person was
        proven (no reviewer gate configured, or the operator's break-glass
        token). `decided_at` is stamped by the implementation regardless, so a
        decision always records when it happened even where it cannot record
        who. Defaulted so that every existing caller keeps compiling and keeps
        meaning what it meant.
        """
        ...

    def history(self, tenant_id: str, since: datetime | None = None,
                subject_id: str | None = None) -> list[ReviewItem]:
        """Resolved items, NEWEST DECISION FIRST, optionally narrowed to
        decisions at or after `since` and to one subject.

        The counterpart to `pending`, and the reason it did not exist is worth
        recording: every decision this queue has ever taken was written with
        `resolution`, `decided_by` and `decided_at` and then reachable only
        through `get(item_id)` by a caller who already knew the id. Retractions
        made that visible - `ReviewService._audit` writes an immediately
        resolved item whose entire purpose is the audit trail, into a store
        with no way to read the trail back.

        Ordered on `decided_at`, not `created_at`: this answers "what was
        decided, and when", and an item raised in March and decided in August
        belongs at August. Items resolved before `decided_at` existed carry
        None and sort last, which is the honest place for a decision whose time
        was never recorded.
        """
        ...
