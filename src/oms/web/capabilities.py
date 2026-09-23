"""Capabilities are properties of constructed route and service bundles."""
from dataclasses import dataclass
from fastapi import APIRouter

API_CONTRACT_VERSION = "1.1"
FEATURES = (
    "manual_learning", "semantic_compilation", "multi_user_identity", "team_scoping",
    "personal_mutes", "contributor_portal", "redaction_vault", "model_settings",
    "scheduled_publish", "managed_publish", "graph_query_console", "advanced_review", "usage_analytics")


@dataclass(frozen=True)
class RouteBundle:
    name: str
    router: APIRouter
    capabilities: frozenset[str] = frozenset()
    required_services: tuple[str, ...] = ()


def capabilities_for(edition: str, services, bundles: tuple[RouteBundle, ...], *, advertised=None):
    enabled = set()
    paths = set()
    for bundle in bundles:
        if not bundle.router.routes:
            raise ValueError(f"route bundle is empty: {bundle.name}")
        if bundle.capabilities and not bundle.required_services:
            raise ValueError(f"capability bundle has no required service: {bundle.name}")
        for name in bundle.required_services:
            if getattr(services, name, None) is None:
                raise ValueError(f"route bundle {bundle.name} requires service {name}")
        if not bundle.capabilities <= set(FEATURES):
            raise ValueError("route bundle names an unknown capability")
        for route in bundle.router.routes:
            key = (route.path, tuple(sorted(route.methods or [])))
            if key in paths:
                raise ValueError("duplicate route registration")
            paths.add(key)
        enabled.update(bundle.capabilities)
    result = {"edition": edition, "api_contract_version": API_CONTRACT_VERSION,
              "schema_version": 1, **{name: name in enabled for name in FEATURES}}
    if advertised is not None and dict(advertised) != result:
        raise ValueError("advertised capabilities disagree with constructed services and routes")
    return result
