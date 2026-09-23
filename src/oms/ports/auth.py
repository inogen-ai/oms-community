from typing import Protocol, runtime_checkable

from oms.domain.models import Principal


@runtime_checkable
class PrincipalStore(Protocol):
    def get_by_credential(self, credential: str) -> Principal | None:
        """Return the principal a bearer credential maps to, or None when the
        credential is unknown or revoked (Section 8.2)."""
        ...


@runtime_checkable
class Authenticator(Protocol):
    def resolve(self, credential: str) -> Principal | None:
        """Resolve a bearer credential to a Principal, or None when it is absent
        or invalid. The initial adapter validates opaque tokens against a
        PrincipalStore; an OIDC/JWT adapter can drop in behind the same port
        without touching the ingestion handler (Section 8.2)."""
        ...
