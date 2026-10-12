"""Existing public writers invalidate prepared changes through their real boundary."""
import pytest

from oms.domain.models import ContentBlock, Edge, Rule, Section
from oms.domain.types import EdgeType, Mutability, SectionKind
from oms.skills.service import SkillAdminService
from oms.sources.errors import StaleMutation


def prepared_content(case):
    store = case.store
    store.upsert_rule(Rule(id="receipt", body="Keep receipts", tenant_id="acme"))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "receipt", "expenses"), tenant_id="acme")
    store.upsert_section(Section(id="prose", skill_id="expenses", tenant_id="acme",
        kind=SectionKind.PROSE, heading="Procedure", order=0, mutability=Mutability.AUTHORIAL_PASSTHROUGH))
    block = ContentBlock(id="original", content_ref="original", kind=SectionKind.PROSE,
                         tenant_id="acme", source_ref="fixture", body="Original procedure")
    store.upsert_content_block(block)
    store.attach_block(block, "prose", tenant_id="acme")


@pytest.mark.parametrize("category", ["metadata", "tags", "rule", "prose", "heading", "delete"])
def test_public_editor_invalidates_prepared_source_change(mutation_case, category):
    prepared_content(mutation_case)
    generation = mutation_case.capture_generation()
    service = SkillAdminService(store=mutation_case.store, repository=mutation_case.repository)
    operations = {
        "metadata": lambda: service.update_metadata("expenses", "acme", description="Edited"),
        "tags": lambda: service.update_metadata("expenses", "acme", tags=["reviewed"]),
        "rule": lambda: service.edit_rule("expenses", "receipt", "acme", "Keep itemized receipts"),
        "prose": lambda: service.revise_section("expenses", "prose", "acme", "Reviewed procedure"),
        "heading": lambda: service.rename_section("expenses", "prose", "acme", "Reviewed procedure"),
        "delete": lambda: service.delete_skill("expenses", "acme"),
    }
    operations[category]()
    with pytest.raises(StaleMutation):
        mutation_case.apply_prepared(generation, "Stale source content")
    assert mutation_case.capture_generation().content == generation.content + 1


def test_direct_creation_does_not_reuse_deleted_skill_generation(mutation_case):
    service = SkillAdminService(store=mutation_case.store, repository=mutation_case.repository)
    generation = mutation_case.capture_generation()
    service.delete_skill("expenses", "acme")
    service.create_skill("acme", name="Expenses", description="New skill", domain="finance")
    with pytest.raises(StaleMutation):
        mutation_case.apply_prepared(generation, "Old installation")
    assert mutation_case.description() == "New skill"
    assert mutation_case.capture_generation().content == generation.content + 2


def test_bound_service_preserves_subclass_without_nested_generation(mutation_case):
    class EditionService(SkillAdminService):
        marker = "edition"
    service = EditionService(store=mutation_case.store, repository=mutation_case.repository)
    generation = mutation_case.capture_generation()

    def edit(store, queue):
        bound = service.using(store, queue)
        assert isinstance(bound, EditionService)
        bound.update_metadata("expenses", "acme", description="First")
        bound.update_metadata("expenses", "acme", name="Updated expenses")
    mutation_case.repository.atomic("document-edit", "acme", edit)
    assert mutation_case.capture_generation().content == generation.content + 1


def test_deletion_closes_only_its_source_card_and_keeps_retained_evidence(mutation_case):
    from oms.domain.models import ReviewItem
    from oms.domain.types import Verdict
    from oms.sources.models import Binding, MergePlan, PlanFingerprint, ResolvedRef, Source, Update
    from tests.sources.test_store_contract import make_snapshot

    sources = mutation_case.sources
    snapshot = make_snapshot()
    sources.put_snapshot(snapshot)
    sources.put_source(Source(source_id="source", tenant_id="acme", canonical_url="https://github.com/example/skills"))
    sources.put_binding(Binding(origin=snapshot.ref.origin, source_id="source", package_path="expenses",
        baseline=snapshot.ref, ref=ResolvedRef(canonical_url="https://github.com/example/skills", kind="branch",
                                             name="main", commit="a" * 40)))
    plan = MergePlan(skill=mutation_case.skill, origin=snapshot.ref.origin, base=snapshot.ref,
        incoming=snapshot.ref, fingerprint=PlanFingerprint(content_generation=0, binding_generation=1,
            update_generation=1, policy_version="1", local_digest="local", candidate_digest="candidate"))
    sources.put_update(Update(update_id="card", plan=plan, generation=1, status="open", review_item_id="source-review"))
    for identifier, kind in (("source-review", "skill_update"), ("unrelated-review", "manual_learning")):
        mutation_case.queue.enqueue(ReviewItem(id=identifier, kind=kind, subject_id="card" if kind == "skill_update" else "expenses", other_id=None,
                                              verdict=Verdict.AMBIGUOUS, reason="Review", tenant_id="acme"))
    generation = mutation_case.capture_generation()
    SkillAdminService(store=mutation_case.store, repository=mutation_case.repository).delete_skill("expenses", "acme")
    assert sources.get_update("card", tenant_id="acme").status == "closed"
    assert not sources.get_binding(mutation_case.skill).active
    assert sources.get_snapshot(snapshot.ref) == snapshot
    assert mutation_case.queue.get("source-review").resolved
    assert not mutation_case.queue.get("unrelated-review").resolved
    with pytest.raises(StaleMutation):
        mutation_case.apply_prepared(generation, "Old installation")


def importer_for(case, tmp_path, sanitiser=None):
    from oms.adapters.blobs.file_blob_store import FileBlobStore
    from oms.import_skills.importer import SkillImporter
    from oms.ingestion.payload_store import FilePayloadStore
    from oms.ingestion.sanitiser import RegexSanitiser
    return SkillImporter(case.store, sanitiser or RegexSanitiser(), FilePayloadStore(tmp_path / "payloads"),
                         case.queue, blob_store=FileBlobStore(tmp_path / "blobs"), repository=case.repository)


def skill_files(tmp_path):
    root = tmp_path / "incoming"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: expenses\ndescription: Incoming description\n---\n# Expenses\n\n## Rules\n\n- Keep receipts.\n")
    return root


def test_import_preparation_cannot_overwrite_an_intervening_edit(mutation_case, tmp_path):
    importer = importer_for(mutation_case, tmp_path)
    prepared = importer.prepare_import_directory(skill_files(tmp_path), "acme")
    mutation_case.edit_description("Local edit after preparation")
    with pytest.raises(StaleMutation):
        importer.apply_prepared(prepared)
    assert mutation_case.description() == "Local edit after preparation"
    assert mutation_case.store.rules_for_skill("expenses", tenant_id="acme") == []


def test_import_external_screening_happens_before_the_write_transaction(mutation_case, tmp_path, monkeypatch):
    from oms.ingestion.sanitiser import RegexSanitiser
    active = False
    calls = []

    class Screening:
        def sanitise(self, context):
            calls.append(active)
            return RegexSanitiser().sanitise(context)
    importer = importer_for(mutation_case, tmp_path, sanitiser=Screening())
    atomic = mutation_case.repository.atomic

    def boundary(*args, **kwargs):
        operation = args[2]

        def guarded(store, queue):
            nonlocal active
            active = True
            try:
                return operation(store, queue)
            finally:
                active = False
        return atomic(*args[:2], guarded, **kwargs)
    monkeypatch.setattr(mutation_case.repository, "atomic", boundary)
    before = mutation_case.capture_generation()
    importer.import_directory(skill_files(tmp_path), "acme")
    assert calls and calls == [False]
    assert mutation_case.capture_generation().content == before.content + 1


def test_upload_preview_cannot_apply_after_an_intervening_edit(mutation_case, tmp_path):
    from io import BytesIO
    from zipfile import ZipFile
    from oms.skills.upload import UploadRefused, UploadService
    from oms.ingestion.sanitiser import RegexSanitiser

    root = skill_files(tmp_path)
    archive = BytesIO()
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("expenses/SKILL.md", (root / "SKILL.md").read_bytes())
    uploads = UploadService(store=mutation_case.store, importer=importer_for(mutation_case, tmp_path),
                            staging_root=tmp_path / "staging", sanitiser=RegexSanitiser())
    staged = uploads.stage(archive.getvalue(), "acme")
    mutation_case.edit_description("New local decision")
    with pytest.raises(UploadRefused):
        uploads.apply(staged.id, "acme")
    assert mutation_case.description() == "New local decision"
    assert mutation_case.store.rules_for_skill("expenses", tenant_id="acme") == []


@pytest.mark.parametrize("category", ["rule_status", "document", "restore", "import_review", "membership", "placement"])
def test_public_workflow_categories_invalidate_prepared_changes(mutation_case, tmp_path, category):
    from fastapi.testclient import TestClient
    from oms.adapters.memory.settings import InMemorySettingsStore
    from oms.community.app import _compose
    from oms.domain.models import ReviewItem, Skill
    from oms.domain.types import SkillVersionCause, Verdict
    from oms.settings.core import CoreSettings
    from oms.web.api import create_app

    prepared_content(mutation_case)
    services = _compose(mutation_case.store, mutation_case.queue, mutation_case.repository,
                        InMemorySettingsStore(), CoreSettings(data_dir=tmp_path, tenant_id="acme"))
    headers = {"Origin": "http://127.0.0.1:4317"}
    if category == "restore":
        version = services.history.capture_required("expenses", "acme", cause=SkillVersionCause.CREATED)
        services.skills.update_metadata("expenses", "acme", description="Newer local description")
    if category == "import_review":
        mutation_case.queue.enqueue(ReviewItem(id="legacy-review", kind="removal", subject_id="receipt",
            other_id=None, verdict=Verdict.AMBIGUOUS, reason="Removed upstream", tenant_id="acme"))
    if category == "membership":
        mutation_case.store.upsert_skill(Skill(id="other", name="Other", description="Other", domain="finance", tenant_id="acme"))
    if category == "placement":
        mutation_case.store.upsert_section(Section(id="rules", skill_id="expenses", tenant_id="acme",
            kind=SectionKind.RULES, heading="Rules", order=1, mutability=Mutability.SYSTEM_AGGREGATED))
    with TestClient(create_app(services), base_url="http://127.0.0.1:4317") as client:
        generation = mutation_case.capture_generation()
        if category == "rule_status":
            response = client.patch("/api/rules/receipt", headers=headers, json={"action": "retract"})
        elif category == "document":
            document = client.get("/api/skills/expenses/document").json()
            response = client.put("/api/skills/expenses/document", headers=headers, json={
                "revision": document["revision"], "parts": [{"anchor": "description", "text": "Reviewed description"}]})
        elif category == "restore":
            comparison = client.get(f"/api/skills/expenses/versions/{version.id}/compare").json()
            response = client.post(f"/api/skills/expenses/versions/{version.id}/restore", headers=headers,
                                   json={"revision": comparison["revision"], "anchors": ["description"]})
        elif category == "import_review":
            response = client.post("/api/import-review/legacy-review/decision", headers=headers, json={"action": "accept"})
        else:
            body = "Keep receipts" if category == "membership" else "Verify approval before reimbursement."
            response = client.post("/api/ingest", headers=headers,
                                   json={"transaction_id": "writer-correction", "correction": body})
            assert response.status_code == 202, response.text
            decision = {"action": "reinforce" if category == "membership" else "create",
                        "body": body, "skill_ids": ["expenses"]}
            if category == "membership":
                decision.update(rule_id="receipt", skill_ids=["expenses", "other"])
            else:
                document = client.get("/api/skills/expenses/document").json()
                decision["placements"] = [{"skill_id": "expenses", "section_id": "rules",
                                            "revision": document["revision"], "position": "end"}]
            response = client.post("/api/review/writer-correction/decision", headers=headers, json=decision)
        assert response.status_code == 200, response.text
        with pytest.raises(StaleMutation):
            mutation_case.apply_prepared(generation, "Stale source content")
        assert mutation_case.capture_generation().content == generation.content + 1


def test_guarded_editor_rolls_back_external_audit_state(mutation_case):
    class Events:
        def __init__(self):
            self.rows = []

        def snapshot_state(self):
            return list(self.rows)

        def restore_state(self, state):
            self.rows = list(state)

        def record_admin_event(self, event):
            self.rows.append(event)
            raise OSError("Audit unavailable")
    events = Events()
    before = mutation_case.snapshot()
    service = SkillAdminService(store=mutation_case.store, repository=mutation_case.repository,
                                admin_events=events)
    with pytest.raises(OSError, match="Audit unavailable"):
        service.update_metadata("expenses", "acme", description="Uncommitted")
    assert mutation_case.snapshot() == before
    assert events.rows == []


def test_required_editor_history_failure_rolls_back_metadata(mutation_case):
    from oms.publish.publisher import Publisher
    from oms.skills.history import SkillHistory

    def factory(store):
        history = SkillHistory(store=store, publisher=Publisher(store))
        capture = history.capture_required

        def fail(*args, **kwargs):
            capture(*args, **kwargs)
            raise OSError("History unavailable")
        history.capture_required = fail
        return history
    before = mutation_case.snapshot()
    service = SkillAdminService(store=mutation_case.store, repository=mutation_case.repository,
                                history_factory=factory)
    with pytest.raises(OSError, match="History unavailable"):
        service.update_metadata("expenses", "acme", description="Uncommitted")
    assert mutation_case.snapshot() == before


def shared_import(mutation_case, tmp_path):
    from oms.domain.models import Skill
    from oms.domain.types import SkillVersionCause
    from oms.publish.publisher import Publisher
    from oms.skills.history import SkillHistory
    importer = importer_for(mutation_case, tmp_path)
    importer._history = SkillHistory(store=mutation_case.store, publisher=Publisher(mutation_case.store))
    root = skill_files(tmp_path)
    importer.import_directory(root, "acme")
    mutation_case.store.upsert_skill(Skill(id="other", name="Other", description="Other", domain="finance", tenant_id="acme"))
    rule = mutation_case.store.rules_for_skill("expenses", tenant_id="acme")[0]
    mutation_case.store.attach_edge(Edge(EdgeType.BELONGS_TO, rule.id, "other"), tenant_id="acme")
    importer._history.capture_required("other", "acme", cause=SkillVersionCause.CREATED)
    with (root / "SKILL.md").open("a") as file:
        file.write("  Example: Keep an itemized receipt.\n")
    return importer, root, rule


def test_import_captures_history_for_other_owners_of_a_changed_shared_rule(mutation_case, tmp_path):
    importer, root, rule = shared_import(mutation_case, tmp_path)
    before = mutation_case.store.latest_skill_version("acme", "other")
    importer.import_directory(root, "acme")
    after = mutation_case.store.latest_skill_version("acme", "other")
    assert after.id != before.id
    assert "Keep an itemized receipt." in after.parts_json
    assert len(mutation_case.store.examples_for_rule(rule.id, tenant_id="acme")) == 1


def test_shared_owner_history_failure_rolls_back_the_entire_import(mutation_case, tmp_path, monkeypatch):
    from oms.skills.history import SkillHistory
    importer, root, rule = shared_import(mutation_case, tmp_path)
    before = mutation_case.snapshot()
    versions = mutation_case.store.skill_versions("acme", "other")
    capture = SkillHistory.capture_required

    def fail(self, skill_id, tenant_id, **kwargs):
        result = capture(self, skill_id, tenant_id, **kwargs)
        if skill_id == "other":
            raise OSError("Shared history unavailable")
        return result
    monkeypatch.setattr(SkillHistory, "capture_required", fail)
    with pytest.raises(OSError, match="Shared history unavailable"):
        importer.import_directory(root, "acme")
    assert mutation_case.snapshot() == before
    assert mutation_case.store.skill_versions("acme", "other") == versions
    assert mutation_case.store.examples_for_rule(rule.id, tenant_id="acme") == []


@pytest.mark.parametrize("collision", ["kind", "source"])
def test_import_refuses_preoccupied_transaction_provenance(mutation_case, tmp_path, collision):
    from oms.domain.types import SignalType
    from oms.import_skills.identity import ImportIdentityConflict
    importer = importer_for(mutation_case, tmp_path)
    root = skill_files(tmp_path)
    prepared = importer.prepare_import_directory(root, "acme")
    transaction = next(iter(prepared.transactions.values()))
    if collision == "source":
        transaction.source_ref = "another/package/SKILL.md"
    else:
        transaction.signal_type = next(value for value in SignalType if value is not SignalType.SKILL_IMPORT)
    mutation_case.store.upsert_transaction(transaction)
    generation = mutation_case.capture_generation()
    with pytest.raises(ImportIdentityConflict):
        importer.prepare_import_directory(root, "acme")
    assert mutation_case.capture_generation() == generation
    assert mutation_case.store.rules_for_skill("expenses", tenant_id="acme") == []
