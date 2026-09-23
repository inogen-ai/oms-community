"""Neo4j VocabularyStore: the persistence the port always anticipated.

Until this adapter existed the only implementation was process-local, so the
web process and the compile worker each held their own copy and every change
died at restart (design §1.2). Same JSON-string storage as the settings
adapter and for the same reason: the body is a nested dict, read and written
whole, never queried into.

Unlike settings, the seed is a REAL copy of the defaults. The vocabulary is
not an overlay: `DEFAULT_VOCABULARY` is the starting point a tenant then
edits, and the in-memory adapter and the importer's fallback both already
treat it that way.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from oms.domain.models import TenantVocabulary
from oms.import_skills.vocabulary import load_default_vocabulary

logger = logging.getLogger(__name__)


def _decode(raw: str | None, tenant_id: str) -> dict[str, Any]:
    try:
        body = json.loads(raw or "")
    except ValueError:
        body = None
    if not isinstance(body, dict):
        logger.warning("tenant %s vocabulary body unreadable; using defaults",
                       tenant_id)
        return load_default_vocabulary()
    return body


class Neo4jVocabularyStore:
    def __init__(self, driver) -> None:
        self._driver = driver

    def get_or_seed_default(self, tenant_id: str) -> TenantVocabulary:
        with self._driver.session() as session:
            record = session.run(
                "MERGE (v:TenantVocabulary {tenant_id:$tid}) "
                "ON CREATE SET v:Entity, v.version=1, v.body=$body, "
                "v.created_at=$now, v.updated_at=$now "
                "RETURN v",
                tid=tenant_id, body=json.dumps(load_default_vocabulary()),
                now=datetime.now(timezone.utc),
            ).single()
        if record is None:
            # Same posture as the settings adapter: this read sits on the
            # import path, and an unreadable vocabulary must mean the
            # defaults, not a stopped import.
            logger.warning("tenant %s vocabulary could not be read; using defaults",
                           tenant_id)
            return TenantVocabulary(tenant_id=tenant_id, version=0,
                                    body=load_default_vocabulary())
        return self._from_node(record["v"])

    def get(self, tenant_id: str) -> TenantVocabulary | None:
        with self._driver.session() as session:
            record = session.run(
                "MATCH (v:TenantVocabulary {tenant_id:$tid}) RETURN v",
                tid=tenant_id,
            ).single()
        return None if record is None else self._from_node(record["v"])

    def add_pattern(self, tenant_id: str, *, field: str, kind: str, pattern: str) -> None:
        vocab = self.get_or_seed_default(tenant_id)
        bucket = vocab.body.setdefault(field, {}).setdefault(kind, [])
        if pattern not in bucket:
            bucket.append(pattern)
        self._write(tenant_id, vocab.body)

    def replace(self, tenant_id: str, body: dict) -> None:
        self.get_or_seed_default(tenant_id)
        self._write(tenant_id, body)

    def _write(self, tenant_id: str, body: dict[str, Any]) -> None:
        with self._driver.session() as session:
            session.run(
                "MATCH (v:TenantVocabulary {tenant_id:$tid}) "
                "SET v.body=$body, v.version=v.version+1, v.updated_at=$now",
                tid=tenant_id, body=json.dumps(body),
                now=datetime.now(timezone.utc),
            )

    def _from_node(self, n) -> TenantVocabulary:
        return TenantVocabulary(
            tenant_id=n["tenant_id"], version=n["version"],
            body=_decode(n.get("body"), n["tenant_id"]),
            created_at=n["created_at"].to_native(),
            updated_at=n["updated_at"].to_native(),
        )
