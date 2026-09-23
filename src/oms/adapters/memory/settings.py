"""Process-local SettingsStore, for tests and for entrypoints with no driver."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from oms.settings.record import TenantSettings


class InMemorySettingsStore:
    def __init__(self) -> None:
        self._store: dict[str, TenantSettings] = {}

    def get_or_seed_default(self, tenant_id: str) -> TenantSettings:
        if tenant_id not in self._store:
            # An empty overlay, not a copy of DEFAULT_SETTINGS: a stored
            # default would shadow the environment (settings/models.py).
            self._store[tenant_id] = TenantSettings(
                tenant_id=tenant_id, version=1, body={})
        return self._store[tenant_id]

    def get(self, tenant_id: str) -> TenantSettings | None:
        return self._store.get(tenant_id)

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
