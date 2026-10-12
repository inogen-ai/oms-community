"""Edition-neutral HTTP construction and capability registration.

The caller owns storage, authentication, product routes and background jobs.
This module only mounts constructed bundles and verifies their declaration.
"""
from collections.abc import Callable

from fastapi import FastAPI

from oms.web.capabilities import RouteBundle, capabilities_for


def validate_route_capabilities(app, bundles, requirement: Callable | None = None):
    declarations = {}
    mounted = {(route.path, method): route.endpoint for route in app.routes
               for method in getattr(route, "methods", ()) or ()}
    for bundle in bundles:
        for route in bundle.router.routes:
            for method in getattr(route, "methods", ()) or ():
                key = (route.path, method)
                if key in declarations:
                    raise ValueError(f"duplicate route registration: {key}")
                if key not in mounted or mounted[key] is not route.endpoint:
                    raise ValueError(f"declared route is not mounted: {key}")
                declarations[key] = bundle.capabilities
    for route in app.routes:
        required = frozenset(requirement(route) or ()) if requirement else frozenset()
        for method in getattr(route, "methods", ()) or ():
            if not required <= declarations.get((route.path, method), frozenset()):
                raise ValueError(f"protected route lacks its capability: {method} {route.path}")


def register_capabilities(app, services, bundles, *, edition,
                          requirement=None, advertised=None):
    bundles = tuple(bundles)
    capabilities = capabilities_for(edition, services, bundles, advertised=advertised)
    validate_route_capabilities(app, bundles, requirement)
    app.state.services = services
    app.state.route_bundles = bundles
    app.state.capabilities = capabilities
    return capabilities


def create_core_app(services, bundles: tuple[RouteBundle, ...], *, edition,
                    title, lifespan=None, requirement=None, advertised=None):
    app = FastAPI(title=title, version="1.2", lifespan=lifespan)
    for bundle in bundles:
        app.include_router(bundle.router)
    register_capabilities(app, services, bundles, edition=edition,
                          requirement=requirement, advertised=advertised)

    @app.get("/api/capabilities")
    def describe_capabilities():
        return app.state.capabilities

    return app
