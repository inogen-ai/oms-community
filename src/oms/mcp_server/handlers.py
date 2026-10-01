from dataclasses import dataclass

from pydantic import ValidationError

from oms.domain.models import Principal
from oms.domain.types import SourceRuntime
from oms.ingestion.contribution import ContributionRequest, correction_payload
from oms.ingestion.schema import SESSION_CONTEXT_FIELDS, CorrectionPayload
from oms.ingestion.service import (
    TRANSACTION_ID_REUSED,
    TRANSACTION_ID_REUSED_MESSAGE,
    IngestionService,
    SanitisationError,
    TenantMismatchError,
    TransactionIdReused,
    UnauthorisedError,
)
from oms.ports.contribution_policy import (
    POLICY_UNAVAILABLE, POLICY_UNAVAILABLE_MESSAGE,
    ContributionPolicyUnavailable, ContributionRefused,
)
from oms.publish.catalogue import (
    CatalogueBlocked, ResourceNotFound, SkillCatalogue, SkillNotFound,
)


@dataclass
class ToolDocument:
    """A tool result that hands back a document: machine-readable metadata plus
    the document itself. The transport emits them as two content blocks, so the
    model reads clean markdown rather than a JSON-escaped blob, and a caching
    client still gets the version without paying for the body twice.

    Exactly one of `body` and `blob` is set. `blob` carries an image, which the
    transport emits as an `ImageContent` block; base64 in a text block is
    something a model can neither see nor use. Every currently working call
    sets `body` and keeps its exact wire shape, because the binary path was a
    typed refusal until now and nothing can be relying on it.
    """
    metadata: dict
    body: str | None = None
    blob: bytes | None = None
    media_type: str | None = None


class ToolError(Exception):
    """Raised when a tool call cannot be processed; surfaced to the MCP client
    with a typed `code` so clients can branch without string-matching (Section 5.3).

    `retryable` is set only where the server knows whether resending the same
    call can succeed: False for a policy refusal or a reused transaction id,
    True for a policy that could not be read. None leaves the key out of the
    result, so every error that existed before it keeps its exact shape."""

    def __init__(self, message: str, code: str = "error",
                 retryable: bool | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def _submit(service: IngestionService, payload: CorrectionPayload, principal: Principal) -> dict:
    try:
        txn = service.ingest(payload, principal)
    except UnauthorisedError as exc:
        raise ToolError(f"unauthorised: {exc}", code="unauthorised") from exc
    except TenantMismatchError as exc:
        raise ToolError(f"tenant mismatch: {exc}", code="tenant_mismatch") from exc
    except SanitisationError as exc:
        raise ToolError(f"sanitisation failed, quarantined: {exc}", code="quarantined") from exc
    # Final for this id, so `retryable` is False; the sentence tells the
    # caller that the contribution itself can go again under a new id.
    except TransactionIdReused as exc:
        raise ToolError(TRANSACTION_ID_REUSED_MESSAGE, code=TRANSACTION_ID_REUSED,
                        retryable=False) from exc
    # Both policy outcomes are errors, never a receipt, so a client that reads
    # only `isError` cannot take either for an accepted contribution. The
    # policy's own sentence goes through for a refusal. An unreadable policy
    # gets the fixed sentence, because its exception may describe the store.
    except ContributionRefused as exc:
        raise ToolError(exc.message, code=exc.code, retryable=False) from exc
    except ContributionPolicyUnavailable as exc:
        raise ToolError(POLICY_UNAVAILABLE_MESSAGE, code=POLICY_UNAVAILABLE,
                        retryable=True) from exc
    result = {"transaction_id": txn.id, "status": "accepted"}
    if txn.workflow_state is not None:
        result["state"] = txn.workflow_state
    return result


def _explain(exc: ValidationError) -> str:
    """A validation failure that names the field and says what was wrong.

    `exc.error_count()` alone produced "invalid payload: 1 error(s)", which
    tells a caller that something is wrong and nothing about what. Observed
    consequence: an agent sent a legal-looking `signal_type`, was refused, and
    recovered by deleting fields one at a time until a call succeeded - two
    round trips and a guess, for a fault a sentence would have resolved.

    Pydantic's own message is used rather than a rewrite of it: it already says
    both halves, and paraphrasing would drift from what the model actually
    enforces.
    """
    parts = []
    for error in exc.errors()[:3]:
        field = ".".join(str(p) for p in error.get("loc", ())) or "(body)"
        parts.append(f"{field}: {error.get('msg', 'invalid')}")
    return "; ".join(parts)


def _ingest(service: IngestionService, arguments: dict, principal: Principal) -> dict:
    # The full envelope keeps the session context inside `execution_context`,
    # and the model ignores keys it does not declare, so a copy at the top
    # level was dropped without a word: the agent heard "accepted" and the
    # reviewer read that no context was provided. Refused by name instead,
    # before anything is written, because nothing says which copy was meant.
    if "execution_context" in arguments:
        misplaced = [name for name in SESSION_CONTEXT_FIELDS if name in arguments]
        if misplaced:
            raise ToolError(f"invalid payload: {', '.join(misplaced)}: send these inside "
                            "execution_context when the call carries one",
                            code="invalid_payload")
    try:
        payload = CorrectionPayload.model_validate(arguments)
    except ValidationError as exc:
        raise ToolError(f"invalid payload: {_explain(exc)}", code="invalid_payload") from exc
    return _submit(service, payload, principal)


def handle_log_correction(service: IngestionService, arguments: dict, principal: Principal) -> dict:
    # Preferred thin form: just the correction; OMS builds the envelope from
    # the principal (same contract as the HTTP front door). The full
    # CorrectionPayload stays accepted for existing clients and for
    # skill-import style calls that need the envelope's extra fields.
    if "execution_context" not in arguments:
        # Building the envelope validates it again, and the envelope checks
        # one thing the thin body does not: the form of a supplied
        # `transaction_id`. Inside the same `try`, so that refusal is typed
        # and names the field like every other, rather than reaching the SDK
        # as an exception whose text repeats the id.
        try:
            body = ContributionRequest.model_validate(arguments)
            payload = correction_payload(body, principal, SourceRuntime.MCP)
        except ValidationError as exc:
            raise ToolError(f"invalid payload: {_explain(exc)}",
                            code="invalid_payload") from exc
        return _submit(service, payload, principal)
    return _ingest(service, arguments, principal)


def handle_log_signal(service: IngestionService, arguments: dict, principal: Principal) -> dict:
    return _ingest(service, arguments, principal)


# --- Tier 2 consumption (spec §9.2) -----------------------------------------
# The read tools. Every one of them takes its tenant from the authenticated
# principal: an argument claiming a tenant is ignored, so no credential can
# read across the partition.

def _required(arguments: dict, field: str) -> str:
    value = arguments.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"invalid payload: {field} is required", code="invalid_payload")
    return value.strip()


def handle_list_skills(catalogue: SkillCatalogue, arguments: dict, principal: Principal) -> dict:
    hint = arguments.get("domain_hint")
    # The SDK no longer checks arguments against the input schema, which
    # declares this one a string. Checked here instead: a hint that is not
    # text is refused as the schema says, never quietly read as no hint,
    # which would answer with the whole catalogue.
    if "domain_hint" in arguments and not isinstance(hint, str):
        raise ToolError("invalid payload: domain_hint must be a string",
                        code="invalid_payload")
    try:
        listings = catalogue.list_skills(principal.tenant_id, domain_hint=hint,
                                         person_id=principal.person_id)
    except CatalogueBlocked as exc:
        raise ToolError(str(exc), code="blocked") from exc
    return {"skills": [
        {"id": s.id, "name": s.name, "description": s.description,
         "domain": s.domain, "version": s.version}
        for s in listings
    ]}


def handle_query_skill(catalogue: SkillCatalogue, arguments: dict,
                       principal: Principal) -> ToolDocument:
    name = _required(arguments, "name")
    try:
        doc = catalogue.query_skill(principal.tenant_id, name, principal_id=principal.id,
                                    person_id=principal.person_id)
    except SkillNotFound as exc:
        raise ToolError(str(exc), code="not_found") from exc
    except CatalogueBlocked as exc:
        raise ToolError(str(exc), code="blocked") from exc
    return ToolDocument(
        metadata={"id": doc.id, "name": doc.name, "version": doc.version,
                  "resources": doc.resources},
        body=doc.body)


def handle_query_skill_resource(catalogue: SkillCatalogue, arguments: dict,
                                principal: Principal) -> ToolDocument:
    skill = _required(arguments, "skill")
    resource = _required(arguments, "resource")
    try:
        found = catalogue.query_skill_resource(principal.tenant_id, skill, resource,
                                               principal_id=principal.id,
                                               person_id=principal.person_id)
    except (SkillNotFound, ResourceNotFound) as exc:
        raise ToolError(str(exc), code="not_found") from exc
    except CatalogueBlocked as exc:
        raise ToolError(str(exc), code="blocked") from exc
    return ToolDocument(
        metadata={"skill": found.skill_id, "resource": found.resource,
                  "version": found.version, "media_type": found.media_type},
        body=found.body, blob=found.blob, media_type=found.media_type)
