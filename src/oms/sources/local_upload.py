"""Staged local uploads: the preview a reviewer saw, the targets they chose, and the receipt a retry gets.

A staged package is kept with the guards its preview was built on, so approving it
later applies exactly what was shown, and once it is approved and discarded a retry
of the same key is answered from the recorded receipt.
"""
from dataclasses import asdict
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from typing import Self

from pydantic import Field, TypeAdapter, model_validator

from oms.domain.identity import SkillRef
from oms.domain.ids import slug
from oms.import_skills.importer import ImportState
from oms.sources import operations
from oms.sources.errors import SourceConflict
from oms.sources.local_package import retain_local_packages
from oms.sources.models import (
    ActionContext, Generations, LocalBatchRequest, LocalImportRequest, OriginRef, UploadReplay,
)
from oms.sources.primitives import NonEmpty, Record, exact_digest


class LocalSelection(Record):
    package_path: str
    target_skill_id: NonEmpty | None = None
    local_name: NonEmpty | None = None
    domain: NonEmpty | None = None
    removal_consents: tuple[str, ...] = ()

    @model_validator(mode="after")
    def explicit_target(self) -> Self:
        if self.target_skill_id is not None:
            if self.local_name is not None or self.domain is not None:
                raise ValueError("Existing targets preserve their name and domain")
        elif not self.local_name or not self.domain:
            raise ValueError("A new skill requires its name and domain")
        return self


class LocalUploadRequest(Record):
    upload_id: NonEmpty
    selections: tuple[LocalSelection, ...] = Field(min_length=1, max_length=100)


def describe_packages(service, packages, tenant_id):
    from oms.skills.upload import SkillDiff
    result = []
    for index, package in enumerate(packages):
        origin = OriginRef(skill=SkillRef(tenant_id, f"preview-{index}"), origin_id="local-preview",
                           kind="local", generation=0)
        snapshot = service.builder.build(package, origin, snapshot_id=f"preview-{index}")
        fields = {part.part_id: part.evidence.value for part in snapshot.effective_projection
                  if part.kind.value == "field"}
        name, domain = fields["field:name"], fields["field:domain"]
        result.append(SkillDiff(skill_id=slug(name), name=name, domain=domain or "general", is_new=True,
            rules_created=[part.evidence.value["body"] for part in snapshot.effective_projection
                           if part.kind.value == "rule"],
            sections_added=[part.evidence.value["heading"] for part in snapshot.effective_projection
                            if part.kind.value == "section"],
            artefacts=[(entry.path, entry.size) for entry in package.manifest if entry.path != "SKILL.md"],
            source_ref=(package.package_path + "/" if package.package_path else "") + "SKILL.md"))
    return result


def stage_source(uploads, root: Path, upload_id: str, tenant_id: str, filename: str, *, purpose, queue_upload_id):
    from oms.skills.upload import StagedUpload
    service = uploads._source_service
    state = uploads._importer.capture_state(tenant_id)
    packages = retain_local_packages(root, service.builder.blobs)
    staged = StagedUpload(id=upload_id, tenant_id=tenant_id, root=root,
        skills=describe_packages(service, packages, tenant_id), warnings=[], filename=filename,
        source_packages=packages,
        purpose=purpose, queue_upload_id=queue_upload_id)
    payload = {"staged": TypeAdapter(StagedUpload).dump_python(staged, mode="json"),
               "state": TypeAdapter(ImportState).dump_python(state, mode="json")}
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    temporary = NamedTemporaryFile(dir=uploads._staging_root, prefix=".source-stage-", delete=False)
    try:
        with temporary:
            temporary.write(encoded)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary.name, uploads._staging_root / (upload_id + ".json"))
    finally:
        Path(temporary.name).unlink(missing_ok=True)
    uploads._staged[upload_id], uploads._states[upload_id] = staged, state
    return staged


def restore_stage(uploads, upload_id: str, tenant_id: str):
    from oms.skills.upload import StagedUpload, UploadNotFound
    if not re.fullmatch(r"upload-[0-9a-f]{32}", upload_id):
        raise UploadNotFound("staged upload not found")
    path = uploads._staging_root / (upload_id + ".json")
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise UploadNotFound("staged upload not found")
    payload = json.loads(path.read_bytes())
    staged = TypeAdapter(StagedUpload).validate_python(payload["staged"])
    state = TypeAdapter(ImportState).validate_python(payload["state"])
    if (staged.id != upload_id or staged.tenant_id != tenant_id or state.tenant_id != tenant_id
            or staged.root != uploads._staging_root / upload_id or not staged.source_packages):
        raise UploadNotFound("staged upload not found")
    uploads._staged[upload_id], uploads._states[upload_id] = staged, state
    return staged


def batch_request(packages, selections, state: ImportState, *, filename, transport) -> LocalBatchRequest:
    available = {package.package_path: package for package in packages}
    chosen = [row.package_path for row in selections]
    if len(chosen) != len(set(chosen)) or any(path not in available for path in chosen):
        raise SourceConflict("invalid_package_selection", "invalid local package selection")
    guards = {row.skill: row for row in state.generations}
    requests = []
    for selection in selections:
        ref = SkillRef(state.tenant_id, selection.target_skill_id or slug(selection.local_name))
        guard = guards.get(ref, Generations(skill=ref, content=0, binding=0))
        requests.append(LocalImportRequest(skill=ref, package=available[selection.package_path],
            expected_content_generation=guard.content, expected_binding_generation=guard.binding,
            create=selection.target_skill_id is None, local_name=selection.local_name, domain=selection.domain,
            transport=transport, filename=filename, approve=True, removal_consents=selection.removal_consents))
    return LocalBatchRequest(requests=tuple(requests))


def _selections_digest(selections) -> str:
    return exact_digest([row.model_dump(mode="json") for row in selections])


def upload_replay(upload_id: str, selections) -> UploadReplay:
    """The request fields a retry restates without the staged bytes."""
    return UploadReplay(upload_id=upload_id, selections_digest=_selections_digest(selections))


def recorded_receipt(service, context: ActionContext, selections, idempotency_key: str, *,
                     upload_id: str | None = None, operation_id: str | None = None):
    """The receipt a settled approval left for this key, once its preview is gone.

    Retries normally replay through the preview, which proves the request is
    unchanged. Without it the batch members must name the same skills in the
    same order and, where the operation recorded them, the same selections and
    upload, or the key belongs to another request. A caller that re-staged the
    preview passes the operation it recorded instead: only that operation
    answers (another one under the key is not its receipt), whatever its upload.
    """
    skills = tuple(SkillRef(context.tenant_id, row.target_skill_id or slug(row.local_name)) for row in selections)

    def read(bound):
        previous = bound.sources.operation_for_key(idempotency_key, tenant_id=context.tenant_id,
                                                   actor_id=context.actor_id)
        if previous is not None:
            service.policy.admit_operation(context, previous, bound.graph)
        return previous
    previous = service._read(context, "local_import", skills, read)
    if previous is None or not previous.batch_members:
        return None
    if operation_id is not None and previous.operation_id != operation_id:
        return None
    recorded = previous.upload_replay
    if (tuple(member.skill for member in previous.batch_members) != skills
            or recorded is not None and (recorded.selections_digest != _selections_digest(selections)
                                         or operation_id is None and upload_id is not None
                                         and recorded.upload_id != upload_id)):
        raise SourceConflict("idempotency_key_reused")
    from oms.sources.operations import admitted_result, refresh
    return admitted_result(service, context, refresh(service, context, previous, action="local_import", skills=skills))


def release_stage(uploads, staged, result) -> None:
    """Discard a preview once its receipt answers every retry.

    A failed outcome keeps the preview, so the same package can be submitted
    again with corrected selections; the staging sweep bounds what it costs.
    """
    from oms.skills.upload import UploadNotFound
    if result.state in operations.TERMINAL and result.state != "failed":
        try:
            uploads.discard(staged.id, staged.tenant_id)
        except UploadNotFound:
            pass


def apply_source_upload(uploads, context: ActionContext, request: LocalUploadRequest, *, idempotency_key: str):
    from oms.skills.upload import UploadNotFound
    try:
        staged = uploads.get(request.upload_id, context.tenant_id)
    except UploadNotFound:
        receipt = (recorded_receipt(uploads._source_service, context, request.selections, idempotency_key,
                                    upload_id=request.upload_id) if uploads._source_service is not None else None)
        if receipt is None:
            raise
        return receipt
    if staged.purpose != "direct" or staged.queue_upload_id is not None:
        raise SourceConflict("upload_requires_owning_approval",
                             "Refresh the preview and use its original approval flow")
    if uploads._source_service is None or not staged.source_packages:
        raise SourceConflict("record_unavailable", "source upload not available")
    batch = batch_request(staged.source_packages, request.selections, uploads._states[staged.id],
                          filename=staged.filename, transport="zip")
    result = uploads._source_service.submit_local_batch(context, batch, idempotency_key=idempotency_key,
        upload_replay=upload_replay(request.upload_id, request.selections))
    release_stage(uploads, staged, result)
    return result


def stage_view(staged):
    return {"upload_id": staged.id, "filename": staged.filename, "warnings": staged.warnings,
            "skills": [asdict(skill) for skill in staged.skills],
            "packages": [{"package_path": package.package_path, "revision": package.revision}
                         for package in staged.source_packages]}
