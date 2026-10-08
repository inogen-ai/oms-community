"""Filesystem-backed content-addressed blob store.

Layout (mirrors git-objects to avoid huge flat directories):

    <root>/blobs/sha256/<aa>/<rest>

where <aa> is the first two hex characters of the sha256 digest and <rest> is the
remaining 62. Writes are atomic via tmp + rename so partial writes never leak.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import os
import re
from tempfile import NamedTemporaryFile

from oms.ports.blob_store import BlobNotFound


class FileBlobStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root) / "blobs"

    def put(self, body: bytes) -> str:
        digest = sha256(body).hexdigest()
        ref = f"sha256-{digest}"
        path = self._path(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            staged = NamedTemporaryFile(dir=path.parent, prefix=".blob-", delete=False)
            tmp = Path(staged.name)
            try:
                with staged:
                    staged.write(body)
                    staged.flush()
                    os.fsync(staged.fileno())
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        return ref

    def get(self, ref: str) -> bytes:
        digest = self._digest(ref)
        path = self._path(digest)
        if not path.exists():
            raise BlobNotFound(ref)
        return path.read_bytes()

    def exists(self, ref: str) -> bool:
        try:
            return self._path(self._digest(ref)).exists()
        except ValueError:
            return False

    def _digest(self, ref: str) -> str:
        algo, _, digest = ref.partition("-")
        if algo != "sha256" or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError(f"unsupported ref: {ref!r}")
        return digest

    def _path(self, digest: str) -> Path:
        return self._root / "sha256" / digest[:2] / digest[2:]
