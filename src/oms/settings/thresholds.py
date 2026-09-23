"""Shared per-tenant threshold lookup with caller-supplied defaults.

Only numbers in [0, 1] are accepted; invalid values retain the caller's default.
"""
from __future__ import annotations

from oms.ports.settings_store import SettingsStore


def valid_threshold(raw: object, default: float) -> float:
    """Return `raw` as a float when it is a number in [0, 1] (bools rejected);
    otherwise the default."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and 0.0 <= raw <= 1.0:
        return float(raw)
    return default


def tenant_threshold(settings_store: SettingsStore | None, tenant_id: str,
                     key: str, default: float) -> float:
    """Look up `key` in the tenant's settings body, validated; the default
    stands when no store is wired or the value is untrustworthy."""
    if settings_store is None:
        return default
    return valid_threshold(settings_store.get_or_seed_default(tenant_id).body.get(key), default)
