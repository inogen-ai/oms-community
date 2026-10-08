from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from threading import Barrier

from oms.adapters.blobs.file_blob_store import FileBlobStore


def test_identical_concurrent_writes_keep_one_complete_blob(tmp_path, monkeypatch):
    store = FileBlobStore(tmp_path)
    content = b"same immutable bytes\n" * 5000
    digest = sha256(content).hexdigest()
    target = store._path(digest)
    barrier = Barrier(2)
    exists = Path.exists

    def both_observe_missing(path):
        if path == target:
            barrier.wait(timeout=5)
            return False
        return exists(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "exists", both_observe_missing)
        with ThreadPoolExecutor(max_workers=2) as pool:
            references = list(pool.map(store.put, (content, content)))
    assert references == [f"sha256-{digest}"] * 2
    assert store.get(references[0]) == content
    assert list(target.parent.iterdir()) == [target]
