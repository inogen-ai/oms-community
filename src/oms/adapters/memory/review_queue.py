from datetime import datetime, timezone
from threading import RLock

from oms.domain.models import ReviewItem


class InMemoryReviewQueue:
    def __init__(self) -> None:
        self.items: list[ReviewItem] = []
        self._mutex = RLock()

    def enqueue_once(self, item: ReviewItem) -> None:
        with self._mutex:
            if self.get(item.id) is None:
                self.items.append(item)

    def enqueue(self, item: ReviewItem) -> None:
        # MERGE semantics, like the Neo4j queue: re-enqueueing an id updates
        # the item rather than duplicating it.
        existing = self.get(item.id)
        if existing is not None:
            self.items[self.items.index(existing)] = item
            return
        self.items.append(item)

    def pending(self, tenant_id: str) -> list[ReviewItem]:
        return [i for i in self.items if i.tenant_id == tenant_id and not i.resolved]

    def get(self, item_id: str) -> ReviewItem | None:
        return next((i for i in self.items if i.id == item_id), None)

    def resolve(self, item_id: str, resolution: str,
                decided_by: str | None = None) -> ReviewItem:
        item = self.get(item_id)
        if item is None:
            raise KeyError(item_id)
        if item.resolved:
            raise ValueError(f"item {item_id} already resolved")
        item.resolved = True
        item.resolution = resolution
        item.decided_by = decided_by
        item.decided_at = datetime.now(timezone.utc)
        return item

    def history(self, tenant_id: str, since: datetime | None = None,
                subject_id: str | None = None) -> list[ReviewItem]:
        rows = [i for i in self.items
                if i.tenant_id == tenant_id and i.resolved
                and (subject_id is None or i.subject_id == subject_id)
                and (since is None or (i.decided_at is not None
                                       and i.decided_at >= since))]
        # `datetime.min` for a decision with no recorded time, so it sorts last
        # under reverse=True rather than raising on a None comparison.
        # tz-aware, because decided_at is always tz-aware when it is set.
        return sorted(
            rows,
            key=lambda i: i.decided_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True)
