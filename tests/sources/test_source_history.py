import json

from oms.adapters.blobs.file_blob_store import FileBlobStore
from oms.domain.models import Artefact
from oms.domain.types import ArtefactKind, SkillVersionCause
from oms.publish.publisher import Publisher
from oms.skills.history import SkillHistory


def test_file_only_update_creates_required_history_with_exact_modes(mutation_case, tmp_path):
    store = mutation_case.store
    blobs = FileBlobStore(tmp_path)
    history = SkillHistory(store=store, publisher=Publisher(store, blob_store=blobs))
    first = Artefact(id="file", content_ref=blobs.put(b"first"), kind=ArtefactKind.OTHER,
                     name="check", size=5, tenant_id="acme", source_ref="source:origin/check", mode=0o100755)
    store.upsert_artefact(first, "expenses", "bin/check", tenant_id="acme")
    before = history.capture_required("expenses", "acme", cause=SkillVersionCause.SOURCE_INSTALL,
                                      source_operation_id="install", source_origin_id="origin", source_revision="a" * 40)
    first.content_ref = blobs.put(b"second")
    first.size = 6
    store.upsert_artefact(first, "expenses", "bin/check", tenant_id="acme")
    after = history.capture_required("expenses", "acme", cause=SkillVersionCause.SOURCE_UPDATE,
                                     source_operation_id="update", source_origin_id="origin", source_revision="b" * 40)
    assert after is not None and before.revision == after.revision
    loaded = store.get_skill_version(after.id, tenant_id="acme")
    assert loaded.source_operation_id == "update"
    assert json.loads(loaded.files_json)[0]["mode"] == 0o100755
    assert json.loads(loaded.files_json)[0]["content_ref"] == first.content_ref
    assert history.capture_required("expenses", "acme", cause=SkillVersionCause.SOURCE_UPDATE) is None


def test_declared_licence_roundtrips_through_shared_renderer_and_history(mutation_case):
    store = mutation_case.store
    skill = store.get_skill("expenses", tenant_id="acme")
    skill.declared_license = 'Example: "terms"\nSecond line'
    store.upsert_skill(skill)
    history = SkillHistory(store=store, publisher=Publisher(store))
    version = history.capture_required("expenses", "acme", cause=SkillVersionCause.SOURCE_UPDATE)
    loaded = store.get_skill("expenses", tenant_id="acme")
    assert loaded.declared_license == skill.declared_license
    parts, _ = Publisher(store).outline_skill("expenses", "acme")
    line = next(line for part in parts for line in part.lines if line.startswith("license:"))
    assert json.loads(line.partition(": ")[2]) == skill.declared_license
    assert json.loads(version.metadata_json)["declared_license"] == skill.declared_license
