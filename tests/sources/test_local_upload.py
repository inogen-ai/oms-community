from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest

from oms.import_skills.importer import SkillImporter
from oms.ingestion.payload_store import FilePayloadStore
from oms.skills.upload import UploadRefused, UploadService
from oms.sources.local_upload import LocalSelection, LocalUploadRequest, apply_source_upload, stage_view


class ForbiddenModels:
    def sanitise(self, *args, **kwargs):
        raise AssertionError("Local source intake invoked the ordinary sanitiser")

    def __call__(self, *args, **kwargs):
        raise AssertionError("Local source intake invoked enrichment")


def uploads_for(world, root):
    forbidden = ForbiddenModels()
    importer = SkillImporter(world.store, forbidden, FilePayloadStore(root / "payloads"), world.queue,
        blob_store=world.blobs, repository=world.repository, prepare_extension=forbidden)
    return UploadService(store=world.store, importer=importer, staging_root=root / "uploads",
                         sanitiser=forbidden, source_service=world.service)


def archive(world):
    result = BytesIO()
    with ZipFile(result, "w") as file:
        file.writestr("skills/expenses/SKILL.md", world.blobs.get(world.package("c", "Local description").manifest[0].blob_ref))
        executable = ZipInfo("skills/expenses/scripts/check.sh")
        executable.create_system = 3
        executable.external_attr = 0o100755 << 16
        file.writestr(executable, b"#!/bin/sh\nexit 0\n")
    return result.getvalue()


@pytest.mark.parametrize("filename, body", [("notes.txt", b"No skill package"), ("SKILL.md", b"invalid skill")])
def test_rejected_source_stage_does_not_leave_extracted_upload(source_world, tmp_path, filename, body):
    uploads = uploads_for(source_world, tmp_path)
    output = BytesIO()
    with ZipFile(output, "w") as file:
        file.writestr(filename, body)
    with pytest.raises(UploadRefused):
        uploads.stage(output.getvalue(), "acme")
    assert list((tmp_path / "uploads").iterdir()) == []
    assert not uploads._staged and not uploads._states


def test_source_stage_write_failure_removes_partial_metadata(source_world, tmp_path, monkeypatch):
    from oms.sources import local_upload
    uploads = uploads_for(source_world, tmp_path)
    original = local_upload.os.replace

    def failed_replace(source, destination):
        if Path(destination).suffix == ".json":
            raise OSError("stage persistence unavailable")
        return original(source, destination)
    monkeypatch.setattr(local_upload.os, "replace", failed_replace)
    with pytest.raises(OSError, match="stage persistence unavailable"):
        uploads.stage(archive(source_world), "acme")
    assert list((tmp_path / "uploads").iterdir()) == []


def test_local_upload_stage_is_model_free_and_restores_exact_request_after_restart(source_world, tmp_path):
    world = source_world
    uploads = uploads_for(world, tmp_path)
    staged = uploads.stage(archive(world), "acme", filename="receipts.zip")
    public = stage_view(staged)
    assert public["packages"][0]["package_path"] == "skills/expenses"
    assert "root" not in public and "source_packages" not in public
    assert world.store.get_skill("expenses", tenant_id="acme") is None
    restarted = uploads_for(world, tmp_path)
    assert restarted.get(staged.id, "acme") == staged
    request = LocalUploadRequest(upload_id=staged.id, selections=(LocalSelection(
        package_path="skills/expenses", local_name="expenses", domain="finance"),))
    result = apply_source_upload(restarted, world.context, request, idempotency_key="approve-local-upload")
    assert result.state == "complete" and result.committed
    again = uploads_for(world, tmp_path)
    assert apply_source_upload(again, world.context, request, idempotency_key="approve-local-upload") == result
    file, path = world.store.artefacts_for_skill("expenses", tenant_id="acme")[0]
    assert path == "scripts/check.sh" and file.mode == 0o100755
    assert world.blobs.get(file.content_ref) == b"#!/bin/sh\nexit 0\n"


def test_source_configured_upload_cannot_use_legacy_approval_or_guess_existing_target(source_world, tmp_path):
    world = source_world
    world.install()
    uploads = uploads_for(world, tmp_path)
    staged = uploads.stage(archive(world), "acme")
    with pytest.raises(UploadRefused):
        uploads.apply(staged.id, "acme")
    with pytest.raises(UploadRefused):
        uploads.prepare_apply(staged.id, "acme")
    request = LocalUploadRequest(upload_id=staged.id, selections=(LocalSelection(
        package_path="skills/expenses", local_name="expenses", domain="finance"),))
    refused = apply_source_upload(uploads, world.context, request, idempotency_key="guessed-target")
    assert refused.state == "failed" and not refused.committed
    assert refused.outcomes[0].code == "explicit_local_target_required"
    assert world.description() == "First description"
    explicit = request.model_copy(update={"selections": (LocalSelection(package_path="skills/expenses", target_skill_id="expenses"),)})
    waiting = apply_source_upload(uploads, world.context, explicit, idempotency_key="explicit-target")
    assert waiting.state == "awaiting_review" and not waiting.committed


def test_staged_local_preview_does_not_refresh_revision_on_apply(source_world, tmp_path):
    world = source_world
    world.install()
    uploads = uploads_for(world, tmp_path)
    staged = uploads.stage(archive(world), "acme")
    world.edit_description("Newer local editing")
    request = LocalUploadRequest(upload_id=staged.id, selections=(LocalSelection(package_path="skills/expenses", target_skill_id="expenses"),))
    outcome = apply_source_upload(uploads, world.context, request, idempotency_key="stale-upload")
    assert outcome.state == "failed" and not outcome.committed
    assert world.description() == "Newer local editing"


def test_unclassified_legacy_stage_requires_a_fresh_preview(source_world, tmp_path):
    import json
    from oms.sources.errors import SourceConflict
    world = source_world
    uploads = uploads_for(world, tmp_path)
    staged = uploads.stage(archive(world), "acme")
    path = uploads._staging_root / (staged.id + ".json")
    payload = json.loads(path.read_bytes())
    payload["staged"].pop("purpose", None)
    payload["staged"].pop("queue_upload_id", None)
    path.write_text(json.dumps(payload))
    request = LocalUploadRequest(upload_id=staged.id, selections=(LocalSelection(
        package_path="skills/expenses", local_name="expenses", domain="finance"),))
    with pytest.raises(SourceConflict):
        apply_source_upload(uploads_for(world, tmp_path), world.context, request, idempotency_key="legacy-stage")
    assert world.store.get_skill("expenses", tenant_id="acme") is None


def test_local_http_refusal_reports_no_receipt_when_the_batch_was_never_reserved(source_world, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from oms.sources.api import source_routes
    from oms.sources.reads import SourceReads
    world = source_world
    uploads = uploads_for(world, tmp_path)
    staged = uploads.stage(archive(world), 'acme', purpose='queue', queue_upload_id='owned-queue-item')
    app = FastAPI()
    app.include_router(source_routes(world.service, SourceReads(world.service, world.blobs),
        lambda request, action: world.context, uploads=uploads).router)
    client = TestClient(app)
    response = client.post('/api/skill-local-imports', headers={'Idempotency-Key': 'never-reserved'}, json={
        'upload_id': staged.id, 'selections': [{'package_path': 'skills/expenses', 'local_name': 'expenses', 'domain': 'finance'}]})
    assert response.status_code == 409, response.text
    assert response.json()['code'] == 'upload_requires_owning_approval'
    assert response.json()['operation_id'] is None
    assert world.sources.operation_for_key('never-reserved', tenant_id='acme', actor_id='reviewer') is None
    assert client.get('/api/skill-source-operations', params={'request_key': 'never-reserved'}).status_code == 404
    assert world.store.get_skill('expenses', tenant_id='acme') is None


def test_settled_source_apply_discards_its_preview_and_replays_from_the_receipt(source_world, tmp_path):
    from oms.skills.upload import UploadNotFound
    from oms.sources.errors import SourceConflict
    world = source_world
    uploads = uploads_for(world, tmp_path)
    staged = uploads.stage(archive(world), "acme")
    request = LocalUploadRequest(upload_id=staged.id, selections=(LocalSelection(
        package_path="skills/expenses", local_name="expenses", domain="finance"),))
    result = apply_source_upload(uploads, world.context, request, idempotency_key="settled")
    assert result.state == "complete" and result.committed
    assert list((tmp_path / "uploads").iterdir()) == []
    assert not uploads._staged and not uploads._states
    assert apply_source_upload(uploads_for(world, tmp_path), world.context, request, idempotency_key="settled") == result
    renamed = request.model_copy(update={"selections": (LocalSelection(
        package_path="skills/expenses", local_name="renamed", domain="finance"),)})
    with pytest.raises(SourceConflict) as reused:
        apply_source_upload(uploads, world.context, renamed, idempotency_key="settled")
    assert reused.value.code == "idempotency_key_reused"
    with pytest.raises(UploadNotFound):
        apply_source_upload(uploads, world.context, request, idempotency_key="never-submitted")


@pytest.mark.parametrize("change", ["package_path", "domain", "removal_consents", "upload_id"])
def test_settled_receipt_refuses_a_reused_key_whose_request_changed(source_world, tmp_path, change):
    from oms.sources.errors import SourceConflict
    world = source_world
    uploads = uploads_for(world, tmp_path)
    staged = uploads.stage(archive(world), "acme")
    selection = LocalSelection(package_path="skills/expenses", local_name="expenses", domain="finance")
    request = LocalUploadRequest(upload_id=staged.id, selections=(selection,))
    result = apply_source_upload(uploads, world.context, request, idempotency_key="settled")
    assert result.state == "complete"
    revised = {"package_path": {"package_path": "skills/other"}, "domain": {"domain": "legal"},
               "removal_consents": {"removal_consents": ("file:scripts/check.sh",)}}.get(change)
    changed = (request.model_copy(update={"selections": (selection.model_copy(update=revised),)}) if revised
               else request.model_copy(update={"upload_id": "upload-" + "0" * 32}))
    with pytest.raises(SourceConflict) as reused:
        apply_source_upload(uploads, world.context, changed, idempotency_key="settled")
    assert reused.value.code == "idempotency_key_reused"
    assert apply_source_upload(uploads, world.context, request, idempotency_key="settled") == result


def test_restaged_preview_replays_only_through_its_recorded_operation(source_world, tmp_path):
    from oms.sources.errors import SourceConflict
    from oms.sources.local_upload import recorded_receipt
    world = source_world
    uploads = uploads_for(world, tmp_path)
    staged = uploads.stage(archive(world), "acme")
    request = LocalUploadRequest(upload_id=staged.id, selections=(LocalSelection(
        package_path="skills/expenses", local_name="expenses", domain="finance"),))
    result = apply_source_upload(uploads, world.context, request, idempotency_key="settled")
    restaged = "upload-" + "1" * 32
    assert recorded_receipt(world.service, world.context, request.selections, "settled",
                            upload_id=restaged, operation_id=result.operation_id) == result
    assert recorded_receipt(world.service, world.context, request.selections, "settled",
                            upload_id=restaged, operation_id="source-operation-other") is None
    with pytest.raises(SourceConflict) as reused:
        recorded_receipt(world.service, world.context, request.selections, "settled", upload_id=restaged)
    assert reused.value.code == "idempotency_key_reused"
    # The positional form the queue path already uses keeps answering by key.
    assert recorded_receipt(world.service, world.context, request.selections, "settled") == result


def test_failed_source_apply_keeps_its_preview_for_corrected_selections(source_world, tmp_path):
    world = source_world
    world.install()
    uploads = uploads_for(world, tmp_path)
    staged = uploads.stage(archive(world), "acme")
    request = LocalUploadRequest(upload_id=staged.id, selections=(LocalSelection(
        package_path="skills/expenses", local_name="expenses", domain="finance"),))
    assert apply_source_upload(uploads, world.context, request, idempotency_key="guessed").state == "failed"
    assert uploads_for(world, tmp_path).get(staged.id, "acme").root.is_dir()


def test_abandoned_previews_are_swept_when_another_package_is_staged(source_world, tmp_path):
    import os
    import time
    from oms.skills.upload import UploadNotFound
    world = source_world
    uploads = uploads_for(world, tmp_path)
    abandoned = uploads.stage(archive(world), "acme")
    crashed = uploads._staging_root / ".source-stage-crashed"
    crashed.write_bytes(b"{}")
    unrelated = uploads._staging_root / "operator-notes.txt"
    unrelated.write_text("not a staged upload")
    old = time.time() - 2 * 86_400
    for path in (abandoned.root, uploads._staging_root / (abandoned.id + ".json"), crashed, unrelated):
        os.utime(path, (old, old))
    fresh = uploads.stage(archive(world), "acme")
    assert sorted(path.name for path in uploads._staging_root.iterdir()) == sorted(
        [fresh.id, fresh.id + ".json", "operator-notes.txt"])
    assert abandoned.id not in uploads._staged and abandoned.id not in uploads._states
    with pytest.raises(UploadNotFound):
        uploads_for(world, tmp_path).get(abandoned.id, "acme")
    assert uploads.get(fresh.id, "acme") == fresh


def test_a_preview_still_being_read_survives_the_staging_sweep(source_world, tmp_path):
    import os
    import time
    world = source_world
    uploads = uploads_for(world, tmp_path)
    viewed = uploads.stage(archive(world), "acme", purpose="queue")
    old = time.time() - 2 * 86_400
    for path in (viewed.root, uploads._staging_root / (viewed.id + ".json")):
        os.utime(path, (old, old))
    assert uploads_for(world, tmp_path).get(viewed.id, "acme") == viewed
    uploads.stage(archive(world), "acme")
    assert uploads_for(world, tmp_path).get(viewed.id, "acme") == viewed
