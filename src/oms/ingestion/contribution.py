"""The thin contribution body shared by the HTTP and MCP front doors.

Supply `correction`, or `learning` for self-reflections; OMS builds the full CorrectionPayload envelope
(id, timestamp, tenant, runtime, signal type) from the resolved principal, so
a prose-following agent has almost nothing to get wrong. Both front doors use
this module, so the contract cannot drift between them.
"""
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oms.domain.models import Principal
from oms.domain.repo import repo_key
from oms.domain.types import SignalType, SourceRuntime
from oms.ingestion.schema import CorrectionPayload, ExecutionContext


class ContributionRequest(BaseModel):
    """The thin body an agent posts to a contribution front door (spec §6.3)."""
    correction: str | None = Field(default=None, min_length=1, max_length=100_000,
        description="A person's durable correction or standing preference, in their words. Omit for self_reflection.")
    learning: str | None = Field(default=None, min_length=1, max_length=100_000,
        description="A useful reusable instruction discovered during work. Required with signal_type='self_reflection'; keep task requests and original output in their context fields.")
    skill_hint: str | None = None
    domain_hint: str | None = None
    assigned_role: str | None = None
    user_input: str | None = None
    agent_raw_output: str | None = None
    transaction_id: str | None = None   # optional; OMS generates a uuid4 if absent
    source_agent_id: str | None = None
    # Where this came from, when that is a place rather than a person: the
    # transcript a backfill recovered it from, the file an import read. The
    # full envelope has carried it since the beginning and the Transaction node
    # stores it; the thin door could not express it, so a scan of a team's
    # history arrived looking exactly like something typed this morning.
    #
    # A reviewer needs the difference. "Recovered from a session in June" and
    # "somebody just said this" are different claims about the same sentence.
    source_ref: str | None = None
    # How this arrived, when it did not arrive from a person saying it. Absent
    # is the whole existing world and still means EXPLICIT_CORRECTION, so every
    # caller written before this field keeps its meaning exactly.
    #
    # A capture hook is a shell script with no MCP client, so it has to reach
    # the HTTP door; `log_signal` takes the full envelope and can already say
    # this, but nothing posting JSON could.
    signal_type: SignalType | None = None
    # The capturer's own estimate, 0-1. Carried rather than acted on: the hold
    # is decided by provenance (`MACHINE_ORIGINATED`), not by a threshold, so
    # this is evidence for a reviewer and a number to tune patterns against
    # before it is anything a gate reads.
    signal_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # The repository the contributing agent was working in, as a git remote in
    # any spelling. Normalised to a key by `correction_payload`, never stored
    # as sent.
    #
    # Preserve the caller's repository context without assigning rule scope.
    repo: str | None = None

    @model_validator(mode="after")
    def _candidate(self):
        if self.signal_type is SignalType.SELF_REFLECTION:
            if not (self.learning or "").strip():
                raise ValueError("self_reflection requires learning: supply the reusable instruction, not the original task; submit nothing if there is no useful learning")
            if self.correction is not None:
                raise ValueError("send learning, not correction, for self_reflection")
        elif self.learning is not None:
            raise ValueError("learning requires signal_type='self_reflection'")
        elif not (self.correction or "").strip():
            raise ValueError("correction is required for a stated correction")
        return self

    @field_validator("signal_type")
    @classmethod
    def _not_the_importers_path(cls, value: SignalType | None) -> SignalType | None:
        """Reserve SKILL_IMPORT for the trusted import entry point.

        Ordinary contributions may state their signal type explicitly; an
        omitted value uses the backwards-compatible correction default.
        """
        if value is SignalType.SKILL_IMPORT:
            raise ValueError(
                "signal_type 'skill_import' is the importer's own path and "
                "cannot be posted here; omit the field for a stated correction")
        return value


def correction_payload(body: ContributionRequest, principal: Principal,
                       source_runtime: SourceRuntime) -> CorrectionPayload:
    """Build the full envelope from a thin body and the authenticated principal.
    The tenant always comes from the principal - it is authoritative, never a
    caller claim."""
    return CorrectionPayload(
        transaction_id=body.transaction_id or str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc),
        source_agent_id=body.source_agent_id or principal.id,
        source_runtime=source_runtime,
        tenant_id=principal.tenant_id,
        skill_hint=body.skill_hint,
        domain_hint=body.domain_hint,
        assigned_role=body.assigned_role,
        # Preserve the default used by callers that omit a signal type.
        source_ref=body.source_ref,
        signal_type=body.signal_type or SignalType.EXPLICIT_CORRECTION,
        signal_confidence=body.signal_confidence,
        # Normalised here, at the one door every thin-body caller comes
        # through, so the store never holds two spellings of one repository.
        repo=repo_key(body.repo),
        execution_context=ExecutionContext(
            user_input=body.user_input or "",
            agent_raw_output=body.agent_raw_output or "",
            user_correction=body.correction,
            learning=body.learning,
        ),
    )
