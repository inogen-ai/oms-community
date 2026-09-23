"""Explicit single-operator authority, independent of development flags."""
from oms.domain.auth import INGEST_WRITE
from oms.domain.models import Principal


class LocalPrincipalResolver:
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    def resolve(self, credential=None):
        return Principal(id="local-operator", tenant_id=self.tenant_id,
            person_id="local-operator", scopes=frozenset({INGEST_WRITE}))

    __call__ = resolve


class SingleWorkspaceScope:
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    def require(self, tenant_id: str):
        if tenant_id != self.tenant_id:
            raise ValueError("this local operator cannot select another workspace")


class NullNotifier:
    """Notifications are explicitly disabled; local history remains available."""
    status = "disabled"

    def notify(self, event):
        return None
