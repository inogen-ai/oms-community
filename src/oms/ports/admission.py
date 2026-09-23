"""Edition-neutral admission contract for new processing operations.

An admission covers one operation, including its in-flight stages. Callers
must request a new admission for the next job, even when reusing a service.
Capture, manual decisions, reads, export and security controls do not use it.
"""
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PaidOperation(StrEnum):
    COMPILE = "semantic_compile"
    REVISE = "authorial_revision"
    SCAN = "consistency_scan"


@dataclass(frozen=True)
class Admission:
    allowed: bool
    reason: str
    expires_at: int | None = None


class ProcessingHeld(RuntimeError):
    def __init__(self, decision: Admission):
        self.decision = decision
        super().__init__(f"New paid processing is held: {decision.reason}. "
                         "An administrator can import or refresh the licence.")


class OperationPolicy(Protocol):
    def admit(self, operation: PaidOperation, tenant_id: str) -> Admission: ...


def require_operation(policy: OperationPolicy, operation: PaidOperation,
                      tenant_id: str) -> Admission:
    decision = policy.admit(operation, tenant_id)
    if not decision.allowed:
        raise ProcessingHeld(decision)
    return decision


class UnrestrictedOperations:
    """Explicit policy for test fixtures and non-commercial compositions."""

    def admit(self, operation: PaidOperation, tenant_id: str) -> Admission:
        return Admission(True, "unrestricted")


class NoPaidOperations:
    """Community composition: no licence lookup and no paid processing."""

    def admit(self, operation: PaidOperation, tenant_id: str) -> Admission:
        return Admission(False, "unsupported_in_community")
