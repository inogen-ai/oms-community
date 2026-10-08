"""The HTTP routes for sources, shared by both editions.

The composing edition supplies the function that turns a request into a trusted
action context. The routes map typed source errors to status codes and a stable
error envelope, serve bounded file previews, and expose the explicit approval of an
uploaded local package.
"""
from collections.abc import Callable
import logging
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException

from oms.domain.identity import SkillRef
from oms.sources import api_models as dto
from oms.sources import models as m
from oms.sources.api_models import FilePreview, OperationView
from oms.sources.credentials import ProfileAccessError
from oms.sources.discovery import DiscoveryAccessError
from oms.sources.errors import CheckThrottled, SourceConflict, SourceError, SourceForbidden, SourceNotFound
from oms.sources.files import FileSide, require_path
from oms.sources.git_reader import AcquisitionError
from oms.sources.limits import BudgetExceeded
from oms.sources.links import LinkError
from oms.sources.local_upload import LocalUploadRequest, apply_source_upload
from oms.sources.mutation import source_repository
from oms.sources.primitives import exact_digest
from oms.sources.reads import SourceReads
from oms.sources.service import SourceService
from oms.web.capabilities import RouteBundle

logger = logging.getLogger(__name__)
ResolveContext = Callable[[Request, str], m.ActionContext]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=512)]
Limit = Annotated[int, Query(ge=1, le=100)]


def source_routes(service: SourceService, reads: SourceReads,
                  resolve_context: ResolveContext, *, automation: bool = False, uploads=None) -> RouteBundle:
    router = APIRouter(prefix="/api", route_class=SourceRoute)

    def context(request, action, key=None):
        admitted = resolve_context(request, action)
        expected_workspace = request.headers.get("X-Source-Workspace")
        if (expected_workspace is not None and not request.url.path.endswith("/skill-source-workspace")
                and expected_workspace != reads.workspace(admitted)):
            raise SourceConflict("workspace_changed")
        if key is not None:
            bind_operation_receipt(request, service.repository, admitted, key)
        return admitted

    @router.get("/skill-source-workspace")
    def workspace(request: Request):
        return JSONResponse({"workspace_id": reads.workspace(context(request, "read"))},
                            headers={"Cache-Control": "no-store"})

    @router.post("/skill-source-discoveries", response_model=dto.DiscoveryView)
    def discover(body: dto.DiscoverBody, request: Request, idempotency_key: Key):
        ctx = context(request, "discover", idempotency_key)
        result = service.discover(ctx, m.DiscoveryRequest(actor_id=ctx.actor_id,
            ref=m.RefRequest(tenant_id=ctx.tenant_id, **body.model_dump(exclude={"discovery_root"})),
            discovery_root=body.discovery_root), idempotency_key=idempotency_key)
        return reads.discovery(ctx, result.discovery_id)

    @router.get("/skill-source-discoveries/{discovery_id}", response_model=dto.DiscoveryView)
    def discovery(discovery_id: str, request: Request, cursor: str | None = None, limit: Limit = 100):
        return reads.discovery(context(request, "discover"), discovery_id, cursor=cursor, limit=limit)

    @router.delete("/skill-source-discoveries/{discovery_id}", status_code=204)
    def discard_discovery(discovery_id: str, request: Request, idempotency_key: Key):
        reads.delete_discovery(context(request, "discover", idempotency_key), discovery_id)
        return Response(status_code=204)

    @router.get("/skill-sources")
    def sources(request: Request, cursor: str | None = None, limit: Limit = 100):
        return reads.sources(context(request, "list_sources"), cursor=cursor, limit=limit)

    @router.get("/skill-sources/{source_id}")
    def source(source_id: str, request: Request, cursor: str | None = None, limit: Limit = 100):
        return reads.source(context(request, "list_sources"), source_id, cursor=cursor, limit=limit)

    @router.get("/skills/{skill_id}/source-binding")
    def binding(skill_id: str, request: Request):
        return reads.binding(context(request, "read"), skill_id)

    @router.get("/skills/{skill_id}/source-history", response_model=m.Page[dto.SourceHistoryEntry])
    def history(skill_id: str, request: Request, cursor: str | None = None, limit: Limit = 100):
        return reads.history(context(request, "history"), skill_id, cursor=cursor, limit=limit)

    @router.get("/skill-updates")
    def updates(request: Request, cursor: str | None = None, limit: Limit = 100):
        return reads.updates(context(request, "list_sources"), cursor=cursor, limit=limit)

    @router.get("/skill-updates/{update_id}", response_model=dto.UpdateView)
    def update(update_id: str, request: Request):
        return reads.update(context(request, "read"), update_id)

    @router.get("/skill-source-operations", response_model=dto.OperationView)
    def operation_for_key(request: Request, request_key: Annotated[str, Query(min_length=1, max_length=512)]):
        ctx = context(request, "operation")
        operation_id = "source-operation-" + exact_digest([ctx.tenant_id, ctx.actor_id, request_key])
        return dto.OperationView.from_result(service.get_operation(ctx, operation_id))

    @router.get("/skill-source-operations/{operation_id}", response_model=dto.OperationView)
    def operation(operation_id: str, request: Request):
        return dto.OperationView.from_result(service.get_operation(context(request, "operation"), operation_id))

    @router.get("/skill-source-discoveries/{discovery_id}/files")
    def discovery_file(discovery_id: str, request: Request, path: str,
                       package_path: str = "", preview: bool = True):
        body = reads.discovery_file(context(request, "discover"), discovery_id, package_path, path)
        return file_response(path, "discovery", body, preview=preview)

    @router.get("/skill-updates/{update_id}/files")
    def update_file(update_id: str, request: Request, path: str,
                    side: Literal["base", "local", "upstream"], preview: bool = True):
        body = reads.update_file(context(request, "read"), update_id, side, path)
        return file_response(path, side, body, preview=preview)

    def generation(ctx, row):
        return m.Generations(skill=SkillRef(ctx.tenant_id, row.skill_id), content=row.content, binding=row.binding)

    def update_input(ctx, body, path, kind):
        return kind(update_id=path["update_id"], skill=SkillRef(ctx.tenant_id, body.skill_id),
                    **body.model_dump(exclude={"skill_id", "update_id"}))

    def binding_input(ctx, body, path, kind):
        return kind(skill=SkillRef(ctx.tenant_id, path["skill_id"]), **body.model_dump())

    def removal_input(ctx, body, path, kind):
        return kind(source_id=path["source_id"], expected_source_generation=body.expected_source_generation,
            expected_generations=tuple(generation(ctx, row) for row in body.expected_generations),
            **({"enabled": body.enabled} if isinstance(body, dto.ScheduleBody) else {}))

    def undo_input(ctx, body, path):
        reads.verify_undo(ctx, path["update_id"], body.undo_id, body.skill_id)
        return m.UndoRequest(undo_id=body.undo_id, skill=SkillRef(ctx.tenant_id, body.skill_id),
            expected_generations=tuple(generation(ctx, row) for row in body.expected_generations),
            policy_version=body.policy_version)

    def mutation(path, action, model, convert):
        def invoke(body, request: Request, idempotency_key: Key):
            ctx = context(request, action, idempotency_key)
            typed = convert(ctx, body, request.path_params)
            result = getattr(service, action)(ctx, typed, idempotency_key=idempotency_key)
            view = dto.OperationView.from_result(result)
            return JSONResponse(view.model_dump(), status_code=202
                                if result.state in {"fetching", "planning", "applying"} else 200)
        invoke.__name__ = "source_" + action
        invoke.__annotations__["body"] = model
        router.add_api_route(path, invoke, methods=["POST"], response_model=dto.OperationView)

    mutation("/skill-source-installations", "install", dto.InstallBody,
             lambda ctx, body, path: m.InstallRequest.model_validate(body.model_dump()))
    mutation("/skill-sources", "create_source", dto.CreateSourceBody,
             lambda ctx, body, path: m.CreateSourceRequest(discovery_id=body.discovery_id))
    mutation("/skill-sources/{source_id}/check", "check", dto.CheckBody, lambda ctx, body, path:
             m.CheckRequest(source_id=path["source_id"], expected_source_generation=body.expected_source_generation,
                            skills=tuple(SkillRef(ctx.tenant_id, skill_id) for skill_id in body.skill_ids)))
    for action, model, kind in (("link", dto.LinkBody, m.LinkRequest), ("relink", dto.LinkBody, m.LinkRequest),
                                ("retarget", dto.RetargetBody, m.RetargetRequest),
                                ("unlink", dto.BindingBody, m.UnlinkRequest)):
        mutation("/skills/{skill_id}/source-binding/" + action, action, model,
                 lambda ctx, body, path, kind=kind: binding_input(ctx, body, path, kind))
    for action, suffix, model, kind in (("save_draft", "draft", dto.DraftBody, m.SaveDraftRequest),
        ("recheck", "recheck", dto.UpdateBody, m.UpdateRequest), ("apply", "apply", dto.ApplyBody, m.ApplyRequest),
        ("skip", "skip", dto.UpdateBody, m.UpdateRequest), ("adopt", "adopt", dto.UpdateBody, m.UpdateRequest)):
        mutation("/skill-updates/{update_id}/" + suffix, action, model,
                 lambda ctx, body, path, kind=kind: update_input(ctx, body, path, kind))
    mutation("/skill-updates/{update_id}/undo", "undo", dto.UndoBody, undo_input)
    mutation("/skill-updates/bulk-apply", "bulk_apply", dto.BulkApplyBody, lambda ctx, body, path:
             m.BulkApplyRequest(updates=tuple(update_input(ctx, row, {"update_id": row.update_id}, m.ApplyRequest)
                for row in body.updates)))
    mutation("/skill-sources/{source_id}/remove", "remove_source", dto.RemoveSourceBody,
             lambda ctx, body, path: removal_input(ctx, body, path, m.RemoveSourceRequest))
    mutation("/skill-sources/{source_id}/relocate", "relocate_source", dto.RelocateSourceBody,
             lambda ctx, body, path: m.RelocateSourceRequest(source_id=path["source_id"],
                 destination_url=body.destination_url, expected_source_generation=body.expected_source_generation,
                 expected_generations=tuple(generation(ctx, row) for row in body.expected_generations)))
    if automation:
        mutation("/skills/{skill_id}/source-binding/automation", "configure_automation", dto.AutomationBody,
                 lambda ctx, body, path: binding_input(ctx, body, path, m.AutomationRequest))
        mutation("/skill-sources/{source_id}/schedule", "configure_schedule", dto.ScheduleBody,
                 lambda ctx, body, path: removal_input(ctx, body, path, m.ScheduleRequest))
    if uploads is not None:
        router.include_router(local_upload_routes(uploads, lambda request, action: context(request, action),
                                                  repository=service.repository))
    return RouteBundle("github_skill_sources", router, frozenset({"github_skill_sources"}), ("sources", "source_reads"))


HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
           "Content-Security-Policy": "sandbox"}


def file_response(path: str, side: FileSide, body: bytes, *, preview: bool) -> Response:
    require_path(path)
    if not preview:
        disposition = "attachment; filename*=UTF-8''" + quote(path.rsplit("/", 1)[-1], safe="")
        return Response(body, media_type="application/octet-stream",
                        headers={**HEADERS, "Content-Disposition": disposition})
    try:
        text = body.decode("utf-8")
        binary = "\x00" in text
    except UnicodeDecodeError:
        text, binary = "", True
    truncated = False
    if not binary:
        bounded = body[:65536].decode("utf-8", errors="ignore")
        bounded = "".join(bounded.splitlines(keepends=True)[:1000])
        truncated = bounded != text
        text = bounded
    result = FilePreview(path=path, side=side, size=len(body), binary=binary,
                         text=None if binary else text, truncated=truncated)
    return JSONResponse(result.model_dump(), headers=HEADERS)


def bind_operation_receipt(request, repository, context, key):
    """Bind caller identity and raw durable existence without disclosing a receipt."""
    operation_id = "source-operation-" + exact_digest([context.tenant_id, context.actor_id, key])
    request.state.source_operation_id = operation_id
    # Admission-filtered reads may hide an existing or committed operation.
    request.state.source_operation_exists = lambda: repository.metadata(
        "source:error_receipt", context.tenant_id, lambda graph, queue:
            source_repository(graph).get_operation(operation_id, tenant_id=context.tenant_id) is not None)


def error_response(exc: Exception, operation_id: str | None) -> JSONResponse:
    status, code, retry = 500, "source_request_failed", None
    if isinstance(exc, DiscoveryAccessError):
        status, code = 404, "discovery_not_found"
    elif isinstance(exc, (SourceForbidden, ProfileAccessError, PermissionError)):
        status, code = 403, "source_action_forbidden"
    elif isinstance(exc, SourceNotFound):
        status, code = 404, exc.code
    elif isinstance(exc, SourceConflict):
        status, code = 409, exc.code
    elif isinstance(exc, CheckThrottled):
        status, code, retry = 429, exc.code, exc.retry_after_seconds
    elif isinstance(exc, BudgetExceeded):
        code = exc.code
        status = {"upstream_timeout": 504, "source_busy": 429,
                  "acquisition_refused": 409}.get(code, 413)
        retry = 60 if status == 429 else None
    elif isinstance(exc, AcquisitionError):
        status, code = 502, "upstream_unavailable"
    elif isinstance(exc, (SourceError, LinkError, ValueError)):
        status, code = 422, exc.code if isinstance(exc, SourceError) else "invalid_source_request"
    if status == 500:
        logger.warning("Source request failed", extra={"code": code, "operation_id": operation_id})
    # The code names the family a client acts on; the exception's safe message keeps the specific
    # condition. Forbidden stays generic so a 403 discloses nothing about why.
    message = str(exc) if isinstance(exc, SourceError) and status != 403 else code.replace("_", " ")
    return JSONResponse({"code": code, "message": message,
                         "operation_id": operation_id, "retry_after_seconds": retry}, status_code=status,
                        headers={"Retry-After": str(retry)} if retry is not None else None)


class SourceRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def guarded(request: Request):
            try:
                return await original(request)
            except (HTTPException, RequestValidationError):
                raise
            except Exception as exc:
                operation_id = getattr(request.state, "source_operation_id", None)
                exists = getattr(request.state, "source_operation_exists", None)
                if exists is not None:
                    try:
                        if await run_in_threadpool(exists) is False:
                            operation_id = None
                    except Exception:
                        pass  # Unavailable storage cannot prove that reservation failed.
                return error_response(exc, operation_id)
        return guarded


def local_upload_routes(uploads, resolve_context, *, repository):
    router = APIRouter(route_class=SourceRoute)

    @router.post("/skill-local-imports", response_model=OperationView)
    def apply_local(body: LocalUploadRequest, request: Request,
                    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=512)]):
        context = resolve_context(request, "local_import")
        bind_operation_receipt(request, repository, context, idempotency_key)
        result = apply_source_upload(uploads, context, body, idempotency_key=idempotency_key)
        return JSONResponse(OperationView.from_result(result).model_dump(),
                            status_code=202 if result.state in {"fetching", "planning", "applying"} else 200)
    return router
