"""Source records in memory, under the graph store's workflow lock.

A write snapshots the whole record dictionary and puts it back on failure, so the
memory adapter rolls back the way the Neo4j transaction does.
"""
from copy import deepcopy
from threading import RLock

from oms.sources.repository import SourceRecords


class InMemorySourceRepository(SourceRecords):
    def __init__(self, store):
        self.store = store
        if not hasattr(store, "_workflow_mutex"):
            store._workflow_mutex = RLock()
        if not hasattr(store, "_source_records"):
            store._source_records = {}

    def _atomic(self, tenant_id, operation):
        # The mutex is re-entrant and shared with the graph store, so a source
        # write inside a workflow transaction joins it rather than deadlocking,
        # and the copy taken here is what a failure restores.
        with self.store._workflow_mutex:
            before = deepcopy(self.store._source_records)
            try:
                return operation(self)
            except BaseException:
                self.store._source_records = before
                raise

    def _tracks_content(self):
        return getattr(self.store, "_workflow_depth", 0) > 0

    def _get(self, tenant, kind, key):
        with self.store._workflow_mutex:
            return self.store._source_records.get((tenant, kind, key))

    def _put(self, tenant, kind, key, value):
        self.store._source_records[tenant, kind, key] = value

    def _delete(self, tenant, kind, key):
        self.store._source_records.pop((tenant, kind, key), None)

    def _rows(self, tenant, kind, *, prefix=""):
        with self.store._workflow_mutex:
            return sorted((key, value) for (owner, category, key), value in self.store._source_records.items()
                          if owner == tenant and category == kind and key.startswith(prefix))
