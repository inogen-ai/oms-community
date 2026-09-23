"""Versioned settings record; policy and validation belong to the edition."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

def _now():
    return datetime.now(timezone.utc)

@dataclass
class TenantSettings:
    """Operational policy for one tenant: thresholds, model choice, publish
    budgets. `version` bumps on every write, so a polling worker reads one
    integer per tick rather than a whole body, and the activity feed has a
    monotonic handle on which change was which."""
    tenant_id: str
    version: int
    body: dict[str, Any]
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)

