"""Tenant policy consulted before a NEW contribution is admitted."""
from typing import Protocol, runtime_checkable

from oms.domain.models import Principal
from oms.ingestion.schema import CorrectionPayload

#: The one refusal this release defines. A composition raises it when a
#: tenant has turned automatic session learnings off.
SESSION_LEARNINGS_DISABLED = "session_learnings_disabled"

#: The reason and the sentence every transport gives when the policy could not
#: be read. One fixed sentence rather than the adapter's own exception text,
#: which may describe its infrastructure: the caller needs to know only that
#: nothing was recorded and that the same request can be sent again later.
POLICY_UNAVAILABLE = "unavailable"
POLICY_UNAVAILABLE_MESSAGE = "contribution policy is unavailable; nothing was recorded; retry later"


class ContributionRefused(Exception):
    """A deliberate, final refusal of a new contribution by policy.

    Never an acknowledgement and never retryable unchanged: the caller must
    not report success, mint a new id and resend, or relabel the signal."""
    retryable = False

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ContributionPolicyUnavailable(Exception):
    """The policy could not be read. An operational fault to retry later,
    never evidence that the contribution is allowed."""
    retryable = True


@runtime_checkable
class ContributionPolicy(Protocol):
    def admit(self, payload: CorrectionPayload, principal: Principal) -> None:
        """Return to admit; raise ContributionRefused or ContributionPolicyUnavailable.

        Two facts make a policy safe to write. `payload` has not been
        sanitised yet (the policy runs first, so a refusal writes nothing), so
        never log or store its text. And under `ContributionCoordinator` this
        runs inside the unit of work that holds the workspace's lock, so only
        read: a write through another connection would wait on that lock."""


class AdmitEveryContribution:
    """The Community default: no tenant policy, so behaviour is unchanged."""
    def admit(self, payload: CorrectionPayload, principal: Principal) -> None:
        return None
