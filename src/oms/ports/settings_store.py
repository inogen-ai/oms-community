"""Per-tenant settings storage port (administrator settings design §5).

The body is an overlay over `DEFAULT_SETTINGS`: a key is present only when an
administrator set it, so resolution can fall through to the environment and
then to the code default. That is why there is no `replace` here, unlike the
vocabulary port: wholesale replacement is exactly the operation that would
claim every key away from its fallback at once, and no caller needs it.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from oms.settings.record import TenantSettings


@runtime_checkable
class SettingsStore(Protocol):
    def get_or_seed_default(self, tenant_id: str) -> TenantSettings:
        """Return the tenant's settings, creating an EMPTY overlay (version 1)
        if absent. Empty, not a copy of the defaults: stored defaults would
        shadow the environment for ever (see settings/models.py)."""
        ...

    def get(self, tenant_id: str) -> TenantSettings | None:
        """Return the tenant's settings, or None if never seeded."""
        ...

    def update(self, tenant_id: str, changes: dict[str, Any]) -> TenantSettings:
        """Merge `changes` into the body and bump the version. A value of None
        REMOVES the key, returning it to the environment-then-default chain;
        that is what the console's per-key reset sends. Validation is the
        caller's job: this port stores what it is given, and resolution
        guards itself against a body it cannot trust."""
        ...
