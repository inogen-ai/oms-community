"""Upload limits a host can set per tenant, within ceilings fixed in code.

A host (the paid console's settings) supplies the limits for each tenant; the upload gate and the
safety file check use them. The source budgets are budgets, not policy: they sit at or above the
ceilings, so they never refuse what a tenant allows.
"""
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from oms.adapters.blobs.file_blob_store import FileBlobStore
from oms.skills.upload import (MAX_FILE_BYTES_CEILING, MAX_UPLOAD_BYTES_CEILING, UploadLimits,
                               UploadService, UploadTooLarge)
from oms.sources.findings import _label
from oms.sources.limits import AcquisitionLimits
from oms.sources.safety import SourceSafety, screen_snapshot

from tests.sources.test_check_findings import _snapshot

MB = 1_000_000


def _zip(members: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as file:
        for name, body in members.items():
            file.writestr(name, body)
    return output.getvalue()


def test_megabyte_limits_agree_with_the_defaults():
    assert UploadLimits.for_megabytes(file_mb=25, upload_mb=100) == UploadLimits()


def test_megabyte_limits_unpack_to_one_and_a_half_times_the_upload():
    limits = UploadLimits.for_megabytes(file_mb=10, upload_mb=40)
    assert (limits.max_file_bytes, limits.max_archive_bytes, limits.max_total_bytes) == (10 * MB, 40 * MB, 60 * MB)


@pytest.mark.parametrize("file_mb, upload_mb, expected", [
    (10_000, 10_000, (MAX_FILE_BYTES_CEILING, MAX_UPLOAD_BYTES_CEILING)),
    (0, -5, (MB, MB)),
])
def test_megabyte_limits_stay_within_the_ceilings(file_mb, upload_mb, expected):
    limits = UploadLimits.for_megabytes(file_mb=file_mb, upload_mb=upload_mb)
    assert (limits.max_file_bytes, limits.max_archive_bytes) == expected


def test_the_ceilings():
    assert (MAX_FILE_BYTES_CEILING, MAX_UPLOAD_BYTES_CEILING) == (50 * MB, 250 * MB)


def test_each_tenant_stages_under_its_own_limits(tmp_path):
    per_tenant = {"acme": UploadLimits.for_megabytes(file_mb=1, upload_mb=5)}
    uploads = UploadService(store=None, importer=None, staging_root=tmp_path,
                            limits_for=lambda tenant: per_tenant.get(tenant, UploadLimits()))
    assert uploads.limits("acme").max_file_bytes == MB
    assert uploads.limits("other") == UploadLimits()
    with pytest.raises(UploadTooLarge, match="the per-file limit is 1 MB"):
        uploads.stage(_zip({"deck/assets/a.bin": b"\x00" * 1_500_000}), "acme")
    with pytest.raises(UploadTooLarge, match="the limit is 5 MB"):
        uploads.stage(b"\x00" * 6 * MB, "acme")


def test_without_a_tenant_reader_the_fixed_limits_apply(tmp_path):
    fixed = UploadLimits(max_file_bytes=7)
    assert UploadService(store=None, importer=None, staging_root=tmp_path, limits=fixed).limits("acme") is fixed


def test_source_budgets_sit_at_or_above_the_ceilings():
    sources, ceiling = AcquisitionLimits(), UploadLimits.for_megabytes(file_mb=10_000, upload_mb=10_000)
    assert sources.file_bytes >= ceiling.max_file_bytes
    assert sources.package_bytes >= ceiling.max_total_bytes
    assert sources.expanded_bytes >= ceiling.max_total_bytes
    # GitHub sources: one blob is read through one bounded command, header and newline included,
    # and a package's files must fit in what one acquisition may fetch and cache.
    assert sources.stdout_bytes >= len(f"{'0' * 64} blob {sources.file_bytes}\n") + sources.file_bytes + 1
    assert sources.cache_bytes >= sources.acquired_bytes >= ceiling.max_total_bytes


def test_the_file_check_uses_its_configured_limit():
    assert SourceSafety().max_file_bytes == 25 * MB
    safety = SourceSafety(max_file_bytes=MB)
    assert safety.check_file(b"\x00" * (MB + 1), path="a.bin").code == "file_size_limit"
    assert safety.check_file(b"\x00" * MB, path="a.bin").code == "binary_structure_only"


@pytest.mark.parametrize("value", [0, -1, 2.5, True])
def test_the_file_check_limit_is_a_positive_whole_number(value):
    with pytest.raises(ValueError):
        SourceSafety(max_file_bytes=value)


def test_the_package_screen_uses_the_configured_file_limit(tmp_path):
    snapshot = _snapshot(tmp_path, {"assets/deck.pptx": b"\x00" * 2_000})
    blobs = FileBlobStore(tmp_path)
    assert screen_snapshot(snapshot, blobs).state == "passed"
    held = screen_snapshot(snapshot, blobs, SourceSafety(max_file_bytes=1_000))
    assert held.code == "file_size_limit"
    assert [finding.path for finding in held.findings] == ["assets/deck.pptx"]


def test_the_size_hold_does_not_name_a_number_a_tenant_can_change():
    assert _label("file_size") == "is larger than the size limit for one file"
