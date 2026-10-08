"""Bounded acquisition; caller admission is the service's responsibility."""
from typing import TYPE_CHECKING, Protocol

from oms.ports.blob_store import BlobStore

from oms.sources.models import (
    AcquiredPackage, DiscoveryRequest, DiscoveryResult, PackageRequest,
    RefRequest, ResolvedRef,
)

if TYPE_CHECKING:
    from oms.sources.settings import SourceSettings


class SourceReader(Protocol):
    def resolve(self, request: RefRequest) -> ResolvedRef: ...
    def read(self, request: PackageRequest) -> AcquiredPackage: ...
    def discover(self, request: DiscoveryRequest) -> DiscoveryResult: ...


class SourceReaderFactory(Protocol):
    def __call__(self, settings: "SourceSettings", *, blob_store: BlobStore) -> SourceReader: ...
