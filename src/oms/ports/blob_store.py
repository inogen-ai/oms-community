"""Content-addressed blob storage.

Used for byte-stable bodies of authorial section content, scripts, reference markdown,
READMEs, and other artefacts that ride alongside a skill package. The graph holds
metadata + `content_ref`; the bytes live here.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class BlobNotFound(Exception):
    """Raised when a content_ref does not resolve in the store."""


@runtime_checkable
class BlobStore(Protocol):
    def put(self, body: bytes) -> str:
        """Write bytes and return content_ref ('sha256-<hex>'). Idempotent on body."""

    def get(self, ref: str) -> bytes:
        """Read bytes by ref. Raises BlobNotFound if absent."""

    def exists(self, ref: str) -> bool:
        """Cheap existence check; used by fsck and publish gate."""
