import logging
from datetime import datetime, timezone

from oms.settings.thresholds import tenant_threshold
from oms.domain.auth import INGEST_WRITE
from oms.domain.models import (
    Principal, QuarantinedPayload, ReviewItem, Transaction,
)
from oms.domain.types import Verdict
from oms.ingestion.payload_store import FilePayloadStore
from oms.ingestion.sanitiser import Sanitiser
from oms.ingestion.schema import CorrectionPayload, ExecutionContext
from oms.ports.repositories import IngestionRepository
from oms.domain.held import pack
from oms.ports.injection_screen import CLEAN, InjectionScreen
from oms.ports.custody import PayloadCustody, RetentionDisabled
from oms.ports.review_queue import ReviewQueue
from oms.ports.settings_store import SettingsStore

logger = logging.getLogger(__name__)

# A reason long enough to name a fault and short enough to be a graph property.
_MAX_REASON = 200


def quarantine_reason(exc: BaseException) -> str:
    """What a refused payload's record may say about why it was refused.

    The exception's type, never its message. `repr(exc)` was the obvious
    choice and it is unsafe: an exception raised while handling text tends to
    quote that text. A sanitiser that reports what it could not scrub puts the
    span in its own message; pydantic's ValidationError renders `input_value=`
    verbatim. Both were demonstrated carrying a payload before this was
    written. `QuarantinedPayload` is a graph node the explorer's read routes
    serve without a token, so `repr` there is a disclosure of exactly the text
    the refusal existed to contain.

    The type is the whole diagnostic value anyway. The row exists so an
    operator can see that contributions are being refused and roughly why -
    an unreachable model and a malformed payload are different classes - and
    the local quarantine file records the same type-only reason. Untrusted
    exception messages never enter either diagnostic record.
    """
    kind = type(exc)
    name = kind.__qualname__
    module = getattr(kind, "__module__", "")
    if module and module != "builtins":
        name = f"{module}.{name}"
    return name[:_MAX_REASON]

_SUMMARY_CHARS = 512


def summarise(ctx: ExecutionContext, limit: int = _SUMMARY_CHARS) -> str | None:
    """A short, sanitised contribution snippet with whitespace collapsed.
    The node stays lean; the payload blob keeps the full context.

    Public because the skill importer writes Transaction nodes too and should
    summarise them the same way; every consumer of `summary` (the review inbox,
    the transaction list, search) then reads one shape rather than two."""
    source = ctx.contribution_text()
    text = " ".join(source.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


#: The only `source_ref` prefix that means "these belong on one card".
_SESSION_REF = "session:"


def group_id_for(source_ref: str | None) -> str | None:
    """The review group a contribution belongs to, or None.

    Narrowed to `session:` deliberately. `source_ref` is a general "where did
    this come from" field and the skill importer puts file paths in it, so
    accepting anything would put every rule imported from one SKILL.md on a
    single card - a different feature, and not one anybody asked for.

    An empty id is refused rather than passed through: "session:" with nothing
    after it would collapse every such item into one group across every
    session, which is worse than not grouping at all.
    """
    if not source_ref or not source_ref.startswith(_SESSION_REF):
        return None
    session = source_ref[len(_SESSION_REF):].strip()
    return f"{_SESSION_REF}{session}" if session else None


class SanitisationError(Exception):
    """Raised when an incoming payload cannot be sanitised; payload is quarantined."""


class UnauthorisedError(Exception):
    """Raised when the principal lacks the ingest:write scope (Section 8.3)."""


class TenantMismatchError(Exception):
    """Raised when the payload names a tenant the principal may not write to,
    including a transaction-id collision against another tenant (Sections 8.3, 9)."""


class IngestionService:
    def in_transaction(self, store: IngestionRepository):
        """Use a repository unit of work; the coordinator owns disposition."""
        from copy import copy
        service = copy(self)
        service._store = store
        service._queue = None
        return service

    def __init__(self, store: IngestionRepository, sanitiser: Sanitiser, payload_store: FilePayloadStore,
                 queue: ReviewQueue | None = None,
                 injection_screen: InjectionScreen | None = None,
                 injection_threshold: float = 0.5,
                 settings_store: SettingsStore | None = None,
                 custody: PayloadCustody | None = None) -> None:
        self._store = store
        self._sanitiser = sanitiser
        self._payloads = payload_store
        self._queue = queue
        # Inert when no screen is wired, so a deployment that has not opted in
        # behaves exactly as before (app.build_ingestion_service supplies one).
        self._screen = injection_screen
        # The constructor float is the fallback; the tenant's own value wins
        # when a settings store is wired, read live at ingest so a console
        # change applies to the next contribution rather than the next
        # restart (administrator settings design §5.1).
        self._injection_threshold = injection_threshold
        self._settings_store = settings_store
        self._custody = custody or RetentionDisabled()

    def ingest(self, payload: CorrectionPayload, principal: Principal) -> Transaction:
        # [A]/[C] Authorise before any work: scope, then tenant binding (Section 8.3).
        if INGEST_WRITE not in principal.scopes:
            raise UnauthorisedError(principal.id)
        if payload.tenant_id != principal.tenant_id:
            raise TenantMismatchError(payload.transaction_id)

        # Idempotency: a replayed transaction id returns the original ack without
        # re-sanitising or re-enqueuing, scoped to the principal's tenant (Section 9).
        existing = self._store.get_transaction(payload.transaction_id)
        if existing is not None:
            if existing.tenant_id != principal.tenant_id:
                raise TenantMismatchError(payload.transaction_id)
            return existing

        try:
            result = self._sanitiser.sanitise(payload.execution_context)
        except Exception as exc:  # quarantine on any sanitiser failure (spec 4.3)
            # Type-only diagnostics: sanitiser exception messages may contain
            # the very text this boundary could not prove safe.
            self._payloads.quarantine(payload.transaction_id, quarantine_reason(exc))
            # The CORRECTION into the vault, which is the half that was
            # missing (design §11). S4.3 promised failed payloads were
            # "quarantined for human review" and what a reviewer could reach
            # was an exception string; the correction itself was destroyed,
            # silently, because `ingest` raises before a Transaction exists
            # and contribution is fire-and-forget.
            self._custody.quarantine(payload, principal)
            # The raised error and the caller's contract are unchanged, so a
            # contributor still sees `quarantined` and no partly-sanitised
            # text reaches the graph.
            # The file above is the evidence; this is the record. Writing only
            # the file meant a refused contribution existed nowhere the console
            # could see: the health strip could not list it, the activity feed
            # never knew, and the contributor got a 422 while an administrator
            # saw a quiet week. A sanitiser that starts refusing everything -
            # a bad rule set, a misconfigured model - looked like no traffic.
            #
            # The exception's TYPE, never its message. The payload is
            # quarantined because nobody could prove what is in it, so the
            # record must not carry text derived from it - and an exception
            # message routinely is. See `quarantine_reason`.
            self._store.record_quarantine(QuarantinedPayload(
                transaction_id=payload.transaction_id,
                tenant_id=principal.tenant_id,
                reason=quarantine_reason(exc),
                quarantined_at=datetime.now(timezone.utc),
                principal_id=principal.id,
            ))
            raise SanitisationError(payload.transaction_id) from exc

        sanitised = result.context
        ref = self._payloads.put(payload.transaction_id, sanitised)
        # A redaction inside the correction itself is a stop signal, not a
        # cleaning: either real PII was about to become a published rule (a
        # human must rephrase it) or the sanitiser misfired and the mangled
        # text would corrupt the skill. Hold the transaction out of the compile
        # queue and put the decision in front of a reviewer.
        held_reason = None
        original = payload.execution_context.learning or payload.execution_context.user_correction
        if original is not None and (sanitised.learning or sanitised.user_correction) != original:
            held_reason = "sanitiser altered the correction text"

        # Prompt-injection screen (work plan G5). It FLAGS, never rejects: a
        # security tenant's genuine corrections quote attack strings verbatim,
        # and contribution is fire-and-forget, so refusing would silently
        # discard the corpus such a tenant most needs recorded. Reusing
        # held_reason puts a suspicious correction in front of the reviewer
        # through the existing held_transaction flow before further processing.
        if self._screen is not None:
            # The port promises a screen never raises, but the promise lives
            # in the implementations and this is the boundary that pays if
            # one breaks it. Belt and braces here, because the cost of a
            # violated contract at this line is a LOST CORRECTION, and losing
            # a correction is worse than missing one detection.
            try:
                screened = self._screen.screen(sanitised.learning or sanitised.user_correction or "")
            except Exception:
                logger.exception("injection screen failed; treating %s as clean",
                                 payload.transaction_id)
                screened = CLEAN
            threshold = tenant_threshold(self._settings_store, principal.tenant_id,
                                         "injection_threshold_ingest",
                                         self._injection_threshold)
            if screened.score >= threshold:
                held_reason = "; ".join(r for r in (held_reason, screened.reason()) if r)
        txn = Transaction(
            id=payload.transaction_id,
            signal_type=payload.signal_type,
            source_runtime=payload.source_runtime,
            sanitised_payload_ref=ref,
            timestamp=payload.timestamp,
            tenant_id=principal.tenant_id,        # authoritative tenant, not the payload's claim
            source_ref=payload.source_ref,
            skill_hint=payload.skill_hint,
            principal_id=principal.id,
            source_agent_id=payload.source_agent_id,
            summary=summarise(sanitised),
            # Identity is captured here because ingest is the only moment the
            # credential exists: it is a transport-level artefact and is gone by
            # compile time. Ingest binds and does NOT judge - what this identity
            # earns is decided at compile, against the live trust table (§8, §9).
            person_id=principal.person_id,
            assurance=principal.assurance,
            held_reason=held_reason,
            # Retain the validated evidence supplied with the contribution.
            signal_confidence=payload.signal_confidence,
            # Preserve the normalised repository key after the request ends.
            repo=payload.repo,
        )
        self._store.upsert_transaction(txn)
        # Written after the transaction, not before: an entry whose transaction
        # does not exist could never be revealed (reveal starts from a review
        # item), so it would be retained PII with no way to reach it and no
        # decision to delete it.
        self._custody.retain(result.redactions, txn.id, principal.tenant_id)
        if held_reason is not None:
            logger.warning("transaction %s held: %s", txn.id, held_reason)
            if self._queue is not None:
                # Only the sanitised text is shown - the raw correction is
                # discarded by design and must never reach the queue.
                self._queue.enqueue(ReviewItem(
                    id=f"review-held-{txn.id}",
                    kind="held_transaction", subject_id=txn.id, other_id=None,
                    verdict=Verdict.AMBIGUOUS,
                    # State the ACTUAL reason: a transaction can now be held by
                    # the sanitiser or by the injection screen, and a reviewer
                    # told the wrong one will approve on the wrong grounds.
                    # Built by domain/held.py, which is also where the review
                    # service takes it apart again (its `_enrich` splits the
                    # cause, the score and the text into their own fields for
                    # the card). Packed rather than three columns because
                    # `reason` is the only field a ReviewItem has that can
                    # carry the text at all, and because a client that renders
                    # nothing else must keep showing the whole story.
                    reason=pack(held_reason, sanitised.learning or sanitised.user_correction or ""),
                    tenant_id=txn.tenant_id,
                    # A batch of learnings an agent filed about one piece of
                    # work shares a session ref, so the console can put them on
                    # one card and cost a reviewer one decision instead of four.
                    group_id=group_id_for(payload.source_ref),
                ))
        return txn

    # -- retention ------------------------------------------------------------
