"""Process-local SettingsStore, for tests and for entrypoints with no driver."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from copy import deepcopy
from threading import RLock
from functools import wraps

from oms.settings.record import TenantSettings


def _locked(method):
    @wraps(method)
    def apply(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)
    return apply


class InMemorySettingsStore:
    def __init__(self) -> None:
        self._mutex = RLock()
        self._workflow_mutex = None
        self._store: dict[str, TenantSettings] = {}

    @_locked
    def get_or_seed_default(self, tenant_id: str) -> TenantSettings:
        if tenant_id not in self._store:
            # An empty overlay, not a copy of DEFAULT_SETTINGS: a stored
            # default would shadow the environment (settings/models.py).
            self._store[tenant_id] = TenantSettings(
                tenant_id=tenant_id, version=1, body={})
        return self._store[tenant_id]

    @_locked
    def get(self, tenant_id: str) -> TenantSettings | None:
        return self._store.get(tenant_id)

    @_locked
    def update(self, tenant_id: str, changes: dict[str, Any]) -> TenantSettings:
        settings = self.get_or_seed_default(tenant_id)
        for key, value in changes.items():
            if value is None:
                settings.body.pop(key, None)
            else:
                settings.body[key] = value
        settings.version += 1
        settings.updated_at = datetime.now(timezone.utc)
        return settings

    def bind_workflow_lock(self, mutex):
        if self._workflow_mutex is not None and self._workflow_mutex is not mutex:
            raise ValueError("Settings store is already bound to another workflow")
        self._workflow_mutex = self._mutex = mutex

    @_locked
    def snapshot_state(self):
        return deepcopy(self._store)

    @_locked
    def restore_state(self, state):
        if not isinstance(state, dict):
            raise TypeError("Invalid settings rollback state")
        self._store = deepcopy(state)
