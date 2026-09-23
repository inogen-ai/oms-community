"""Process-local VocabularyStore. Persistence across process restarts is a
future concern; for now the importer always seeds from defaults if the tenant
isn't already known."""
from __future__ import annotations

from datetime import datetime, timezone

from oms.domain.models import TenantVocabulary
from oms.import_skills.vocabulary import load_default_vocabulary


class InMemoryVocabularyStore:
    def __init__(self) -> None:
        self._store: dict[str, TenantVocabulary] = {}

    def get_or_seed_default(self, tenant_id: str) -> TenantVocabulary:
        if tenant_id not in self._store:
            self._store[tenant_id] = TenantVocabulary(
                tenant_id=tenant_id, version=1, body=load_default_vocabulary())
        return self._store[tenant_id]

    def get(self, tenant_id: str) -> TenantVocabulary | None:
        return self._store.get(tenant_id)

    def add_pattern(self, tenant_id: str, *, field: str, kind: str, pattern: str) -> None:
        v = self.get_or_seed_default(tenant_id)
        bucket = v.body.setdefault(field, {}).setdefault(kind, [])
        if pattern not in bucket:
            bucket.append(pattern)
        v.version += 1
        v.updated_at = datetime.now(timezone.utc)

    def replace(self, tenant_id: str, body: dict) -> None:
        v = self.get_or_seed_default(tenant_id)
        v.body = body
        v.version += 1
        v.updated_at = datetime.now(timezone.utc)
