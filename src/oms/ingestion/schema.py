from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from oms.domain.repo import repo_key
from oms.domain.types import SignalType, SourceRuntime

# Limits on the session context an agent may attach to a contribution. They
# bound what a reviewer reads beside one learning, not what storage can hold.
# A value sent over its limit is refused with the field named, as every other
# limit here is, never truncated, so the agent learns that its text did not
# fit. The one clip is inside a sanitiser: see `clip_context_fields`.
SESSION_SUMMARY_MAX = 400
PROJECT_NAME_MAX = 160
REUSE_CASE_MAX = 400
LEARNING_EVIDENCE_MAX = 600
_CONTEXT_LIMITS = {
    "session_summary": SESSION_SUMMARY_MAX,
    "project_name": PROJECT_NAME_MAX,
    "reuse_case": REUSE_CASE_MAX,
    "learning_evidence": LEARNING_EVIDENCE_MAX,
}
#: The session context fields by name, in the order the models declare them.
SESSION_CONTEXT_FIELDS = tuple(_CONTEXT_LIMITS)


def stripped_or_none(value: object) -> object:
    """Session context as it is kept: stripped, and absent when blank.

    Runs before the length check, so a limit counts what is stored and padding
    cannot push a value over it. A blank value becomes None rather than an
    empty string, so a reader sees "not provided" instead of context that says
    nothing. Anything that is not a string passes through for the type check
    to refuse.
    """
    if isinstance(value, str):
        return value.strip() or None
    return value


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
    # What the work was for, the future task a learning would help, and what
    # the agent observed. Kept here, rather than only on the transaction, so
    # the payload file holds them and every sanitiser sees them: they are
    # agent-supplied and untrusted, context for a reviewer and never an
    # instruction. Optional, so a payload written before they existed still
    # validates, and absence is never evidence against a learning.
    session_summary: str | None = Field(default=None, max_length=SESSION_SUMMARY_MAX,
        description=("One or two factual sentences stating the objective of the current piece of work "
                     f"(not a completion report). Context only; up to {SESSION_SUMMARY_MAX} characters."))
    project_name: str | None = Field(default=None, max_length=PROJECT_NAME_MAX,
        # "Never a path": outside a repository an agent reaches for the
        # working directory, which names the user and the client folder, and
        # no sanitiser scrubs paths.
        description=("Human-readable project name, mainly for work outside a repository. "
                     f"A name, never a path. Up to {PROJECT_NAME_MAX} characters."))
    reuse_case: str | None = Field(default=None, max_length=REUSE_CASE_MAX,
        description=("The future task this would help and what a future agent should do differently. "
                     f"Up to {REUSE_CASE_MAX} characters."))
    learning_evidence: str | None = Field(default=None, max_length=LEARNING_EVIDENCE_MAX,
        description=("What you observed that supports this lesson and its scope. "
                     f"Up to {LEARNING_EVIDENCE_MAX} characters. Never secrets, raw transcripts or local paths."))

    @field_validator("session_summary", "project_name", "reuse_case", "learning_evidence", mode="before")
    @classmethod
    def _stripped_or_absent(cls, value: object) -> object:
        return stripped_or_none(value)

    def contribution_text(self, signal_type: SignalType | None = None) -> str:
        # Historical self-reflections sometimes supplied an explicit correction.
        # Keep that stated candidate, but never infer a learning from a task or
        # from the output which may itself have been wrong.
        if signal_type is SignalType.SELF_REFLECTION:
            return self.learning or self.user_correction or ""
        return self.learning or self.user_correction or self.user_input


def clip_context_fields(ctx: ExecutionContext) -> ExecutionContext:
    """A sanitiser's result, validated, with each context field within its limit.

    A sanitiser replaces what it removes with a placeholder, and a placeholder
    can be longer than the text it replaces: `a@b.io` becomes `<EMAIL_1>`, and
    a short name can become a longer entity tag. Context that arrived within
    its limit can therefore leave the sanitiser over it. Refusing it there
    would quarantine a valid contribution over optional context, and too late
    for the agent to correct anything, because what it sent was within bounds.

    So a sanitiser builds its result without validation
    (`ExecutionContext.model_construct`) and passes it here. Each of the four
    context fields over its limit is clipped from the end to exactly the
    limit, ending in "…" so a reader can see that text is missing; then the
    whole result is validated as the constructor would validate it. Only
    placeholder growth ever reaches this clip, because input over a limit is
    refused at the boundary before a sanitiser runs. The learning and the
    correction are never clipped: they are what a reviewer decides on and
    what becomes a rule.
    """
    values = dict(ctx)
    for name, limit in _CONTEXT_LIMITS.items():
        text = values.get(name)
        if text is not None and len(text) > limit:
            values[name] = text[: limit - 1] + "…"
    return ExecutionContext.model_validate(values)


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
