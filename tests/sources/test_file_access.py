from hashlib import sha256

import pytest

from oms.sources.models import ManifestEntry


def test_preview_is_inert_and_bounded_by_both_bytes_and_lines():
    from oms.sources.api import file_response

    body = ("<script>alert('unsafe')</script>\n" * 2000).encode()
    response = file_response("guide.html", "upstream", body, preview=True)
    assert response.media_type == "application/json"
    import json
    preview = json.loads(response.body)
    assert preview["media_type"] == "text/plain"
    assert preview["truncated"]
    assert len(preview["text"].splitlines()) == 1000
    assert len(preview["text"].encode()) <= 65536
    assert response.headers["x-content-type-options"] == "nosniff"


def test_download_preserves_exact_binary_bytes_and_safe_filename():
    from oms.sources.api import file_response

    body = b"\x00\xff\r\n"
    response = file_response('references/quo"te.bin', "base", body, preview=False)
    assert response.body == body
    assert response.media_type == "application/octet-stream"
    assert response.headers["content-disposition"].endswith("quo%22te.bin")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-security-policy"] == "sandbox"


def test_file_custody_refuses_mismatched_content_even_when_blob_exists():
    from oms.sources.files import verified_bytes
    from oms.sources.errors import SourceConflict

    class Blobs:
        def get(self, ref):
            return b"changed"
    manifest = ManifestEntry(path="SKILL.md", blob_ref="retained", digest=sha256(b"old").hexdigest(),
                             size=3, mode=0o100644)
    with pytest.raises(SourceConflict, match="custody"):
        verified_bytes(Blobs(), manifest)


@pytest.mark.parametrize("path", ["../secret", "/secret", "a\\secret", "a/../secret", "a\x00b"])
def test_file_response_refuses_non_manifest_paths(path):
    from oms.sources.api import file_response
    from oms.sources.errors import SourceNotFound

    with pytest.raises(SourceNotFound):
        file_response(path, "upstream", b"content", preview=False)
