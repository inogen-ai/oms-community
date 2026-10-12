"""Framework factory; it consumes services and performs no storage construction."""
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from oms.community.routes import build_routes
from oms.community.workflow import DecisionConflict, ManualDecisionError
from oms.ingestion.service import (
    SanitisationError, TenantMismatchError, TransactionIdReused, UnauthorisedError,
)
from oms.ports.contribution_policy import (
    POLICY_UNAVAILABLE, POLICY_UNAVAILABLE_MESSAGE,
    ContributionPolicyUnavailable, ContributionRefused,
)
from oms.skills.service import InvalidSkillEdit, SkillExists, SkillNotFound
from oms.skills.upload import UploadRefused
from oms.web.capabilities import capabilities_for
from oms.web.framework import create_core_app


def create_app(services, settings=None, *, route_bundles=None, advertised_capabilities=None):
    services.validate()
    settings = (settings or services.settings).validate()
    if settings.tenant_id != services.settings.tenant_id:
        raise ValueError("HTTP workspace and service workspace disagree")
    core_bundles = build_routes(services)
    bundles = tuple(core_bundles if route_bundles is None else route_bundles)
    def declarations(groups):
        return {(route.path, tuple(sorted(route.methods or []))): bundle.capabilities
                for bundle in groups for route in bundle.router.routes}
    if declarations(bundles) != declarations(core_bundles):
        raise ValueError("Community routes and their capability declarations must match the core contract")
    capabilities = capabilities_for("community", services, bundles, advertised=advertised_capabilities)
    if any(value for name, value in capabilities.items() if name not in
           ("edition", "api_contract_version", "schema_version", "manual_learning", "github_skill_sources")):
        raise ValueError("this composition does not implement the advertised capability")

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from oms.mcp_server.server import build_server
    manager = StreamableHTTPSessionManager(build_server(services.coordinator,
        resolve_principal=services.principal_resolver, catalogue=services.catalogue),
        stateless=True, json_response=True)

    async def reconcile():
        while True:
            await asyncio.sleep(30)
            try:
                await asyncio.to_thread(services.coordinator.reconcile, settings.tenant_id)
            except Exception:
                import logging
                logging.getLogger(__name__).exception("manual inbox reconciliation failed")

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            task = asyncio.create_task(reconcile())
            try:
                yield
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = create_core_app(services, bundles, edition="community", title="OMS Community",
                          lifespan=lifespan, advertised=capabilities)

    @app.get("/oms-config.json")
    def client_config():
        return {"api_url": ""}

    class McpEndpoint:
        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)
    app.router.routes.append(Route("/mcp", endpoint=McpEndpoint(), methods=["GET", "POST", "DELETE"]))

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        try:
            raw_host = request.headers.get("host", "")
            parsed_host = urlsplit("//" + raw_host)
            host = parsed_host.hostname
            _ = parsed_host.port
            if (parsed_host.username is not None or parsed_host.path or parsed_host.query
                    or parsed_host.fragment or any(c.isspace() for c in raw_host)):
                raise ValueError("invalid host authority")
        except ValueError:
            return JSONResponse({"detail": "invalid HTTP host"}, 400)
        if "*" not in settings.allowed_hosts and host not in settings.allowed_hosts:
            return JSONResponse({"detail": "untrusted HTTP host"}, 400)
        if "tenant_id" in request.query_params or "tenant" in request.query_params:
            return JSONResponse({"detail": "the workspace is fixed by the local server"}, 400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if (origin == "null" or (origin is not None and "*" not in settings.allowed_origins
                                       and origin not in settings.allowed_origins)
                    or request.headers.get("sec-fetch-site") == "cross-site"):
                return JSONResponse({"detail": "browser origin is not allowed"}, 403)
            length = request.headers.get("content-length", "0")
            if not length.isdecimal() or int(length) > 26_000_000:
                return JSONResponse({"detail": "request body is too large"}, 413)
        return await call_next(request)

    app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins),
                       allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
                       allow_headers=["Content-Type", "MCP-Protocol-Version", "MCP-Session-Id", "Idempotency-Key", "X-Source-Workspace"],
                       expose_headers=["MCP-Session-Id", "Retry-After", "Content-Disposition"])

    async def invalid(request, exc):
        status = 409 if isinstance(exc, (DecisionConflict, SkillExists)) else 422
        if isinstance(exc, (SkillNotFound,)):
            status = 404
        if isinstance(exc, (TenantMismatchError, UnauthorisedError)):
            status = 403
        return JSONResponse({"detail": str(exc)}, status)
    for exception in (ValueError, ManualDecisionError, DecisionConflict, SkillExists,
                      SkillNotFound, InvalidSkillEdit, UploadRefused, SanitisationError,
                      TenantMismatchError, UnauthorisedError):
        app.add_exception_handler(exception, invalid)

    async def policy_outcome(request: Request, exc: Exception) -> JSONResponse:
        # Kept apart from `invalid`: nothing is wrong with the request, and
        # the caller needs more than a sentence. `reason` is a code to branch
        # on and `retryable` says whether sending the same request again can
        # succeed: 403 is final, 503 asks for a later retry. Neither is the
        # 202 receipt, so a client that reads only the status still sees a
        # failure. An unreadable policy gets the fixed sentence, because its
        # exception may describe the store.
        if isinstance(exc, ContributionRefused):
            return JSONResponse({"detail": exc.message, "reason": exc.code,
                                 "retryable": False}, 403)
        return JSONResponse({"detail": POLICY_UNAVAILABLE_MESSAGE, "reason": POLICY_UNAVAILABLE,
                             "retryable": True}, 503)
    for exception in (ContributionRefused, ContributionPolicyUnavailable):
        app.add_exception_handler(exception, policy_outcome)

    async def reused_transaction_id(request: Request, exc: Exception) -> JSONResponse:
        # 409, never the 202 receipt: the id names another contribution, so
        # answering with that one's receipt would report this request as
        # accepted when nothing of it was stored. In the policy outcomes'
        # shape, so one client check covers both; `retryable` is False
        # because the same request meets the same transaction again.
        return JSONResponse({"detail": TransactionIdReused.message,
                             "reason": TransactionIdReused.code, "retryable": False}, 409)
    app.add_exception_handler(TransactionIdReused, reused_transaction_id)
    if settings.ui_dir is not None:
        app.mount("/", StaticFiles(directory=settings.ui_dir, html=True), name="community_ui")
    return app
