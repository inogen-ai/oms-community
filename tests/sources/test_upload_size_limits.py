"""One bundled file may be up to 25 MB, and every stage of a local upload agrees.

The upload gate is the strictest and the only one a reader meets: the later source budgets and the
file check sit at or above it, so a file the upload admits is never refused or held for its size
further on, where the message is a bare code or a hold nobody can accept.
"""
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from oms.skills.upload import UploadLimits, UploadTooLarge, UploadService
from oms.sources.limits import AcquisitionLimits
from oms.sources.local_upload import LocalSelection, LocalUploadRequest, apply_source_upload
from oms.sources.safety import SourceSafety

from tests.sources.test_local_upload import uploads_for

MB = 1_000_000


def test_upload_limits_admit_a_25_mb_file_and_a_100_mb_upload():
    limits = UploadLimits()
    assert limits.max_file_bytes == 25 * MB
    assert limits.max_archive_bytes == 100 * MB
    assert limits.max_total_bytes == 150 * MB


def test_later_budgets_never_refuse_what_the_upload_admits():
    upload, sources = UploadLimits(), AcquisitionLimits()
    assert sources.file_bytes >= upload.max_file_bytes
    assert sources.package_bytes >= upload.max_total_bytes
    assert sources.expanded_bytes >= upload.max_total_bytes
    body = b"\x00" * upload.max_file_bytes
    assert SourceSafety().check_file(body, path="assets/deck.pptx").code != "file_size_limit"


def test_a_file_over_the_default_limit_is_held_for_its_size():
    held = SourceSafety().check_file(b"\x00" * (26 * 1024 * 1024 + 1), path="assets/huge.bin")
    assert held.code == "file_size_limit"


def _zip(members: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as file:
        for name, body in members.items():
            file.writestr(name, body)
    return output.getvalue()


def test_a_file_over_the_limit_is_refused_in_megabytes(tmp_path):
    uploads = UploadService(store=None, importer=None, staging_root=tmp_path,
                            limits=UploadLimits(max_file_bytes=10 * MB))
    with pytest.raises(UploadTooLarge) as refused:
        uploads.stage(_zip({"deck/assets/layout-library.pptx": b"\x00" * 14_472_000}), "acme")
    assert str(refused.value) == "'deck/assets/layout-library.pptx' is 14.5 MB; the per-file limit is 10 MB"


def test_an_upload_over_the_limit_is_refused_in_megabytes(tmp_path):
    uploads = UploadService(store=None, importer=None, staging_root=tmp_path,
                            limits=UploadLimits(max_archive_bytes=MB))
    with pytest.raises(UploadTooLarge) as refused:
        uploads.stage(b"\x00" * 1_500_000, "acme")
    assert str(refused.value) == "the upload is 1.5 MB; the limit is 1 MB"


def test_a_package_with_a_20_mb_asset_installs(source_world, tmp_path):
    world = source_world
    uploads = uploads_for(world, tmp_path)
    skill_md = world.blobs.get(world.package("c", "Local description").manifest[0].blob_ref)
    asset = b"PK\x03\x04" + b"\x00" * (20 * MB)
    staged = uploads.stage(_zip({"skills/deck/SKILL.md": skill_md, "skills/deck/assets/layout.pptx": asset}), "acme")
    request = LocalUploadRequest(upload_id=staged.id, selections=(LocalSelection(
        package_path="skills/deck", local_name="deck", domain="design"),))
    result = apply_source_upload(uploads, world.context, request, idempotency_key="large-asset")
    assert result.state == "complete" and result.committed
    file, path = world.store.artefacts_for_skill("deck", tenant_id="acme")[0]
    assert path == "assets/layout.pptx" and file.size == len(asset)
    assert world.blobs.get(file.content_ref) == asset
