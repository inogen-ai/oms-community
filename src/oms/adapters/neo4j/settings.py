"""Neo4j SettingsStore: the adapter that makes tenant settings real.

The body is a nested dict and Neo4j properties cannot hold maps, so it is
stored as one JSON string property. That is fine here: the body is small,
always read and written whole, and never queried into.

A body that fails to parse reads as EMPTY rather than raising. The settings
overlay is the one store whose failure mode must be "fall back to the
defaults", because every reader of it is a gate on the compile or publish
path, and a corrupt property must not be able to stop either (design §3).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from oms.settings.record import TenantSettings

logger = logging.getLogger(__name__)


def _decode(raw: str | None, tenant_id: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        logger.warning("tenant %s settings body is not JSON; treating as empty",
                       tenant_id)
        return {}
    if not isinstance(body, dict):
        logger.warning("tenant %s settings body is not an object; treating as empty",
                       tenant_id)
        return {}
    return body


class Neo4jSettingsStore:
    def __init__(self, driver) -> None:
        self._driver = driver

    def get_or_seed_default(self, tenant_id: str) -> TenantSettings:
        # ON CREATE only: an empty overlay, never a copy of the defaults
        # (settings/models.py says why), and never a reset of what is there.
        with self._driver.session() as session:
            record = session.run(
                "MERGE (s:TenantSettings {tenant_id:$tid}) "
                "ON CREATE SET s:Entity, s.version=1, s.body='{}', "
                "s.created_at=$now, s.updated_at=$now "
                "RETURN s",
                tid=tenant_id, now=datetime.now(timezone.utc),
            ).single()
        if record is None:
            # A MERGE that returns no row cannot happen on a healthy Neo4j,
            # but this read sits on the ingest and compile paths, and the
            # overlay's whole promise is that failing to read it means the
            # defaults, never a stopped pipeline (design §3). Version 0 marks
            # the answer as unpersisted; a write still fails loudly below.
            logger.warning("tenant %s settings could not be read; using defaults",
                           tenant_id)
            return TenantSettings(tenant_id=tenant_id, version=0, body={})
        return self._from_node(record["s"])

    def get(self, tenant_id: str) -> TenantSettings | None:
        with self._driver.session() as session:
            record = session.run(
                "MATCH (s:TenantSettings {tenant_id:$tid}) RETURN s",
                tid=tenant_id,
            ).single()
        return None if record is None else self._from_node(record["s"])

    def update(self, tenant_id: str, changes: dict[str, Any]) -> TenantSettings:
        # Read-merge-write in Python rather than map surgery in Cypher: the
        # body is one JSON property, and this is the only writer.
        settings = self.get_or_seed_default(tenant_id)
        for key, value in changes.items():
            if value is None:
                settings.body.pop(key, None)
            else:
                settings.body[key] = value
        now = datetime.now(timezone.utc)
        with self._driver.session() as session:
            record = session.run(
                "MATCH (s:TenantSettings {tenant_id:$tid}) "
                "SET s.body=$body, s.version=s.version+1, s.updated_at=$now "
                "RETURN s",
                tid=tenant_id, body=json.dumps(settings.body), now=now,
            ).single()
        if record is None:
            # Unlike the read above, a write that did not land must be LOUD:
            # an administrator whose save silently vanished would trust a
            # threshold that is not in force.
            raise RuntimeError(f"tenant {tenant_id} settings write did not land")
        return self._from_node(record["s"])

    def _from_node(self, n) -> TenantSettings:
        return TenantSettings(
            tenant_id=n["tenant_id"], version=n["version"],
            body=_decode(n.get("body"), n["tenant_id"]),
            created_at=n["created_at"].to_native(),
            updated_at=n["updated_at"].to_native(),
        )
