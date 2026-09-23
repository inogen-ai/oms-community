"""Optional custody beyond sanitised local payload storage."""
from typing import Protocol, runtime_checkable
from oms.domain.models import Principal
from oms.ingestion.schema import CorrectionPayload


@runtime_checkable
class PayloadCustody(Protocol):
    status: str
    def retain(self, redactions, transaction_id: str, tenant_id: str) -> None: ...
    def quarantine(self, payload: CorrectionPayload, principal: Principal) -> None: ...
    def forget(self, transaction_id: str, tenant_id: str) -> None: ...


class RetentionDisabled:
    """Raw redactions are discarded; sanitised contribution custody remains local."""
    status = "disabled"

    def retain(self, redactions, transaction_id, tenant_id):
        return None

    def quarantine(self, payload, principal):
        return None

    def forget(self, transaction_id, tenant_id):
        return None
