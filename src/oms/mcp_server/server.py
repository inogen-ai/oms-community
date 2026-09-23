import base64
import json
from typing import Callable

from mcp.server import Server
from mcp import types

from oms.domain.models import Principal
from oms.ingestion.contribution import ContributionRequest
from oms.ingestion.schema import CorrectionPayload
from oms.ingestion.service import IngestionService
from oms.mcp_server.handlers import (
    ToolDocument, ToolError, handle_list_skills, handle_log_correction,
    handle_log_signal, handle_query_skill, handle_query_skill_resource,
)
from oms.publish.catalogue import SkillCatalogue

_PAYLOAD_SCHEMA = CorrectionPayload.model_json_schema()
_CONTRIBUTION_SCHEMA = ContributionRequest.model_json_schema()

TOOL_SCHEMAS = [
    {"name": "log_correction", "description": "Capture useful durable guidance in the local manual inbox. Submit a standing preference, a correction to existing guidance, or an evidenced reusable lesson. Do not submit task requests, answer outlines, generic advice or one-off answer coaching. Zero contributions is normal. For self_reflection send learning instead of correction. A person reviews the wording and target skills before publication.", "inputSchema": _CONTRIBUTION_SCHEMA},
    {"name": "log_signal", "description": "Capture an interaction or useful session learning with its source and signal type for local manual review. For self_reflection, execution_context.learning must contain the reusable instruction; user_input and agent_raw_output are only historical context. Do not submit tasks, generic advice or one-off answer coaching. Zero contributions is normal.", "inputSchema": _PAYLOAD_SCHEMA},
]

# Consumption Tier 2 (spec §9.2): the read side of the same connection. A host
# without native skill selection reads the published manifest, matches a task to
# a trigger, and fetches the skill through these. Offered only when the server
# is built with a catalogue, so an ingest-only deployment advertises nothing it
# cannot answer.
READ_TOOL_SCHEMAS = [
    {
        "name": "list_skills",
        "description": (
            "List the organisation's published skills: name, description, and "
            "current version for each. Call this to discover what guidance "
            "exists before starting a task, then fetch the relevant one with "
            "query_skill. Optionally pass domain_hint to narrow the list to a "
            "department or topic (for example \"finance\" or \"code review\")."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "domain_hint": {
                    "type": "string",
                    "description": "Narrow the catalogue to skills matching this domain or topic.",
                },
            },
        },
    },
    {
        "name": "query_skill",
        "description": (
            "Fetch one skill's full body - the organisation's rules for that "
            "kind of work - and follow it for the current task. Pass the name "
            "exactly as listed by list_skills or named in the AGENTS.md "
            "dispatch table. Returns the body plus a version you can cache "
            "against: refetch only when the version changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The skill to fetch."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "query_skill_resource",
        "description": (
            "Fetch one of a skill's reference files - the detail deliberately "
            "kept out of the body (edge cases, low-frequency rules, checklists). "
            "Call it only when the skill body points you at that resource by "
            "name; query_skill lists what is available."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "The skill that owns the resource."},
                "resource": {"type": "string",
                             "description": "Resource path as named in the skill body."},
            },
            "required": ["skill", "resource"],
        },
    },
]

_DISPATCH = {
    "log_correction": handle_log_correction,
    "log_signal": handle_log_signal,
}

_READ_DISPATCH = {
    "list_skills": handle_list_skills,
    "query_skill": handle_query_skill,
    "query_skill_resource": handle_query_skill_resource,
}

# Resolves the authenticated principal for a call. The transport layer supplies
# it: stdio closes over a fixed local principal (Section 8.4), HTTP reads the
# bearer token. A None result means the caller could not be authenticated.
PrincipalResolver = Callable[[], Principal | None]


def authorise(resolve_principal: PrincipalResolver) -> Principal:
    """Resolve the caller's principal or refuse the call (Section 8.4)."""
    principal = resolve_principal()
    if principal is None:
        raise ToolError("unauthorised: no valid principal", code="unauthorised")
    return principal


def _content(result: dict | ToolDocument) -> list[types.ContentBlock]:
    """One JSON block for a plain result; metadata + raw document for a fetched
    skill or reference, so the model reads markdown rather than escaped JSON.

    An image document's second block is `ImageContent`, which is the shape a
    vision model consumes; base64 in a text block is bytes it can neither see
    nor use. Text is untouched, and that matters: this path was a typed
    refusal for binaries until images were served, so every call that works
    today keeps exactly the two text blocks it has always had.
    """
    if isinstance(result, ToolDocument):
        head = types.TextContent(type="text", text=json.dumps(result.metadata))
        if result.blob is not None:
            return [head, types.ImageContent(
                type="image",
                data=base64.b64encode(result.blob).decode("ascii"),
                mimeType=result.media_type or "application/octet-stream")]
        return [head, types.TextContent(type="text", text=result.body or "")]
    return [types.TextContent(type="text", text=json.dumps(result))]


def build_server(service: IngestionService, resolve_principal: PrincipalResolver | None = None,
                 catalogue: SkillCatalogue | None = None,
                 admit_write: Callable[[Principal], None] | None = None,
                 admit_read: Callable[[Principal], None] | None = None,
                 *, tool_schemas=None) -> Server:
    # Default to refusing every call: an unconfigured transport is unauthorised,
    # never anonymous-write. A stdio deployment passes a local-principal resolver.
    #
    # admit_write is a second, write-only gate (bundle-token ruling, spec
    # §8.2): authorise() below only checks that SOME principal resolved,
    # which the bundle token's pass-through semantics always satisfy (an
    # unrecognised or missing credential degrades to unbound, never None) -
    # so it cannot by itself express "refuse this write". Called only before
    # a _DISPATCH (write-tool) handler. Default None means no extra gate -
    # what the stdio transport passes. It is handed the principal authorise()
    # has already resolved for this call, so a gate that needs one (the HTTP
    # mount's does, for its rate limit) does not resolve a second identity
    # from the same credential.
    #
    # admit_read is its read-side twin, and exists for the same reason: the
    # comment below about an unauthenticated caller having no catalogue to
    # read holds for stdio, where an absent credential can resolve to None,
    # and NOT for the HTTP mount, where §8.1 requires a missing credential to
    # degrade to an unbound principal rather than refuse. So every anonymous
    # HTTP caller passed authorise() and read the whole catalogue. Two
    # callables rather than one because they answer differently: a deployment
    # that opens reads (mcp_open_reads, for a private network where the
    # network is the boundary) must still refuse anonymous writes.
    resolve = resolve_principal or (lambda: None)
    server = Server("oms-ingestion")
    tools = list(TOOL_SCHEMAS if tool_schemas is None else tool_schemas) + (READ_TOOL_SCHEMAS if catalogue is not None else [])

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(**schema) for schema in tools]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        try:
            handler = _DISPATCH.get(name)
            reader = _READ_DISPATCH.get(name) if catalogue is not None else None
            if handler is None and reader is None:
                raise ToolError(f"unknown tool: {name}", code="invalid_payload")
            # Authorise before dispatch, reads included: the tenant a read is
            # scoped to comes from the principal, so an unauthenticated caller
            # has no catalogue to read.
            principal = authorise(resolve)
            if handler is not None and admit_write is not None:
                admit_write(principal)
            # Before dispatch, so a refusal never bills a usage event or
            # touches the catalogue's store.
            if handler is None and admit_read is not None:
                admit_read(principal)
            result = (handler(service, arguments, principal) if handler is not None
                      else reader(catalogue, arguments, principal))
            return types.CallToolResult(content=_content(result), isError=False)
        except ToolError as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps({"error": str(exc), "code": exc.code}))],
                isError=True,
            )

    return server
