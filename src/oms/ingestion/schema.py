from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from oms.domain.repo import repo_key
from oms.domain.types import SignalType, SourceRuntime


class ExecutionContext(BaseModel):
    user_input: str = Field(description="The user request or prompt that the agent was responding to.")
    agent_raw_output: str = Field(description="The agent's response that was corrected or that drew the signal.")
    user_correction: str | None = Field(
        default=None,
        description=(
            "The user's corrective text, in their words. Required in spirit for log_correction; "
            "leave null for log_signal, where the fix is inferred from behaviour rather than stated."
        ),
    )
    learning: str | None = Field(default=None, min_length=1, max_length=100_000,
        description="The proposed reusable instruction for a self_reflection. Required for self_reflection; never put the original task or agent response here.")

    def contribution_text(self, signal_type: SignalType | None = None) -> str:
        # Historical self-reflections sometimes supplied an explicit correction.
        # Keep that stated candidate, but never infer a learning from a task or
        # from the output which may itself have been wrong.
        if signal_type is SignalType.SELF_REFLECTION:
            return self.learning or self.user_correction or ""
        return self.learning or self.user_correction or self.user_input


class CorrectionPayload(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description="Caller-supplied unique id for this event; used for idempotency and audit lineage.")
    timestamp: datetime = Field(description="When the interaction occurred (ISO 8601, timezone-aware).")
    source_agent_id: str = Field(description="Stable identifier of the agent that produced the corrected output.")
    source_runtime: SourceRuntime = Field(description="Channel the event arrived on: 'mcp', 'http', or 'manual'.")
    tenant_id: str = Field(description="Tenant the correction belongs to; scopes the graph and must match the caller's principal.")
    domain_hint: str | None = Field(
        default=None,
        description="Optional soft suggestion of the domain. Not a routing decision; domain assignment happens in the graph.",
    )
    skill_hint: str | None = Field(
        default=None,
        description="Optional id of the skill the posting agent was operating in, to aid retrieval and clustering.",
    )
    assigned_role: str | None = Field(default=None, description="Optional role the agent was acting as when the output was produced.")
    signal_type: SignalType = Field(
        description=(
            "Kind of signal. Use 'explicit_correction' (or 'skill_import') with log_correction; "
            "use 'self_reflection' with execution_context.learning for a discovered lesson; "
            "use 'edit', 'rerun', 'thumbs_down', 'abandonment', or 'telemetry_anomaly' for implicit signals."
        ),
    )
    signal_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="How strongly the signal implies a correction (0.0-1.0). Most relevant for inferred (implicit) signals.",
    )
    source_ref: str | None = Field(
        default=None,
        description="Optional pointer to the origin of the event (for example a file path plus commit for an import).",
    )
    repo: str | None = Field(
        default=None,
        description=(
            "Normalised git remote of the repository the agent was working in "
            "(for example github.com/acme/payments). Context for the scope "
            "judgement and, for a repo-scoped rule, the bundle it anchors to."),
    )
    execution_context: ExecutionContext = Field(description="The interaction itself: the request, the agent output, and any stated correction.")

    @model_validator(mode="after")
    def _explicit_learning(self):
        if self.signal_type is SignalType.SELF_REFLECTION:
            if not (self.execution_context.learning or "").strip():
                raise ValueError("self_reflection requires execution_context.learning: supply the reusable instruction, not the original task; submit nothing if there is no useful learning")
            if self.execution_context.user_correction is not None:
                raise ValueError("send execution_context.learning, not user_correction, for self_reflection")
        elif self.execution_context.learning is not None:
            raise ValueError("learning requires signal_type='self_reflection'; use user_correction for a person's stated correction")
        return self

    @field_validator("repo")
    @classmethod
    def _one_spelling_per_repository(cls, value: str | None) -> str | None:
        """Normalise here, on the envelope, so there is genuinely one door.

        `correction_payload` keys the thin contribution body, and for a while
        that was the only call to `repo_key` - but it is not the only way an
        envelope is built. `MCPHandlers._ingest` validates a CorrectionPayload
        straight from the tool arguments, which is the whole of `log_signal`
        and of `log_correction` whenever an `execution_context` is supplied.
        `log_signal` is the tool the published instruction names for session
        learnings, and self-reflections are where narrow rules come from, so
        that was the busiest path for exactly this field. A caller sending
        `git@github.com:Acme/Pay.git` got a bundle named after the raw remote
        sitting beside the properly keyed one, with nothing reporting it.

        Making it a property of the envelope means a later caller who builds
        one some third way cannot miss it. `correction_payload`'s own call is
        now redundant rather than wrong: `repo_key` is idempotent, so the
        second application returns the first's output unchanged. It is kept
        because it is what makes the THIN body's own `repo` field meaningful
        at the point it is read, and losing it would leave that door's
        behaviour resting on a validator two modules away.
        """
        return repo_key(value)
