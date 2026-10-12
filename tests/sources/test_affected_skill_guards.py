"""Writes fingerprint, lock and advance only the skills they reach."""
from functools import wraps
from threading import Thread

import pytest

from oms.domain.identity import SkillRef
from oms.domain.models import Edge, Rule, Section, Skill
from oms.domain.types import EdgeType, Mutability, SectionKind
from oms.skills.service import SkillAdminService
from oms.sources.errors import StaleMutation


def _skill(store, skill_id, *, domain="finance"):
    store.upsert_skill(Skill(id=skill_id, name=skill_id.title(), description="Unrelated",
                             domain=domain, tenant_id="acme"))
    return SkillRef("acme", skill_id)


def _generation(case, skill_id):
    return case.sources.get_generations(SkillRef("acme", skill_id))


def _counting(monkeypatch, store, name):
    original = getattr(type(store), name)
    calls = []

    @wraps(original)
    def counted(self, *args, **kwargs):
        calls.append(args[:1])
        return original(self, *args, **kwargs)
    monkeypatch.setattr(type(store), name, counted)
    return calls


def test_unrelated_write_neither_advances_nor_stales_another_skill(mutation_case):
    _skill(mutation_case.store, "other")
    guard = mutation_case.capture_generation()
    other = _generation(mutation_case, "other")
    mutation_case.repository.atomic("other-edit", "acme", lambda graph, queue:
        SkillAdminService(store=graph).update_metadata("other", "acme", description="Changed elsewhere"))
    assert _generation(mutation_case, "other").content == other.content + 1
    assert mutation_case.capture_generation() == guard
    mutation_case.apply_prepared(guard, "prepared before the unrelated edit")
    assert mutation_case.description() == "prepared before the unrelated edit"


def test_shared_rule_edit_advances_every_including_skill_and_nothing_else(mutation_case):
    store = mutation_case.store
    _skill(store, "placing")
    _skill(store, "bystander")
    store.upsert_rule(Rule(id="shared", body="Keep receipts", tenant_id="acme"))
    store.attach_edge(Edge(EdgeType.BELONGS_TO, "shared", "expenses"), tenant_id="acme")
    store.upsert_section(Section(id="placing-rules", skill_id="placing", kind=SectionKind.RULES,
        heading="Rules", order=0, mutability=Mutability.SYSTEM_AGGREGATED, tenant_id="acme"))
    store.attach_rule(store.get_rule("shared"), "placing-rules", order=0, tenant_id="acme")
    before = {name: _generation(mutation_case, name) for name in ("expenses", "placing", "bystander")}

    def reword(graph, queue):
        rule = graph.get_rule("shared")
        rule.body = "Keep every receipt"
        graph.upsert_rule(rule)
    mutation_case.repository.atomic("reword", "acme", reword)
    after = {name: _generation(mutation_case, name) for name in before}
    assert after["expenses"].content == before["expenses"].content + 1
    assert after["placing"].content == before["placing"].content + 1
    assert after["bystander"] == before["bystander"]
    with pytest.raises(StaleMutation):
        mutation_case.apply_prepared(before["expenses"], "stale shared wording")


def test_deleting_a_co_owner_advances_the_remaining_owner(mutation_case):
    store = mutation_case.store
    _skill(store, "other")
    store.upsert_rule(Rule(id="shared", body="Keep receipts", tenant_id="acme"))
    for owner in ("expenses", "other"):
        store.attach_edge(Edge(EdgeType.BELONGS_TO, "shared", owner), tenant_id="acme")
    before = mutation_case.capture_generation()
    mutation_case.repository.atomic("delete-other", "acme", lambda graph, queue:
        SkillAdminService(store=graph).delete_skill("other", "acme"))
    assert mutation_case.capture_generation().content == before.content + 1


@pytest.mark.parametrize("unrelated", [2, 30])
def test_write_cost_does_not_scale_with_unrelated_skills(mutation_case, monkeypatch, unrelated):
    for index in range(unrelated):
        _skill(mutation_case.store, f"library-{index}")
    calls = _counting(monkeypatch, mutation_case.store, "sections_for_skill")

    def edit(graph, queue):
        SkillAdminService(store=graph).update_metadata("expenses", "acme", description="Scoped")
    _, generations, skills = mutation_case.repository.atomic_with_receipt("scoped", "acme", edit)
    assert skills == (mutation_case.skill,)
    assert generations == (mutation_case.capture_generation(),)
    assert 0 < len(calls) <= 6, f"{len(calls)} section reads for one skill edit"


def test_one_skill_digest_reads_only_that_skill(mutation_case, monkeypatch):
    from oms.sources.mutation import content_digest
    for index in range(30):
        _skill(mutation_case.store, f"library-{index}")
    calls = _counting(monkeypatch, mutation_case.store, "sections_for_skill")
    assert content_digest(mutation_case.store, mutation_case.skill) is not None
    assert set(calls) == {("expenses",)}


def test_memory_metadata_does_not_copy_skill_content(mutation_case):
    if hasattr(mutation_case.store, "_driver"):
        pytest.skip("Memory snapshot only")

    class Witness:
        def __deepcopy__(self, memo):
            raise AssertionError("metadata copied unrelated graph state")
    mutation_case.store.rules["witness"] = Witness()
    try:
        assert mutation_case.repository.metadata("read", "acme", lambda graph, queue: "read") == "read"
    finally:
        del mutation_case.store.rules["witness"]


@pytest.mark.integration
def test_source_reads_do_not_wait_for_the_workflow_lock(source_world):
    if not hasattr(source_world.store, "_driver"):
        pytest.skip("Neo4j lock only")
    from oms.sources.reads import SourceReads
    reads = SourceReads(source_world.service, source_world.blobs)
    workspace = reads.workspace(source_world.context)
    answers = []
    with source_world.store._driver.session() as session:
        transaction = session.begin_transaction()
        try:
            transaction.run("MERGE (w:WorkflowLock {tenant_id:'acme'}) SET w.locked=true REMOVE w.locked").consume()
            reader = Thread(target=lambda: answers.append(reads.workspace(source_world.context)), daemon=True)
            reader.start()
            reader.join(timeout=10)
        finally:
            transaction.rollback()
    assert answers == [workspace]


def test_import_progress_is_reported_immediately_before_each_skill(tmp_path):
    from oms.adapters.memory.review_queue import InMemoryReviewQueue
    from oms.adapters.memory.store import InMemoryGraphStore
    from oms.community.repository import MemoryWorkflowRepository
    from oms.import_skills.importer import SkillImporter
    from oms.ingestion.payload_store import FilePayloadStore
    from oms.ingestion.sanitiser import RegexSanitiser
    store, queue = InMemoryGraphStore(), InMemoryReviewQueue()
    importer = SkillImporter(store, RegexSanitiser(), FilePayloadStore(root=tmp_path / "payloads"), queue,
                             repository=MemoryWorkflowRepository(store, queue))
    for name in ("alpha", "beta"):
        package = tmp_path / "skills" / name
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Use for {name}.\n---\n\n"
                                          f"# {name}\n\n* Keep the {name} log.\n")
    seen = []
    importer.import_directory(tmp_path / "skills", "acme", on_skill=lambda index, total, name:
        seen.append((index, total, len(store.skills_for_tenant("acme")))))
    assert seen == [(1, 2, 0), (2, 2, 1)]


def test_import_progress_stays_monotonic_when_the_write_transaction_retries(tmp_path):
    from oms.adapters.memory.review_queue import InMemoryReviewQueue
    from oms.adapters.memory.store import InMemoryGraphStore
    from oms.community.repository import MemoryWorkflowRepository
    from oms.import_skills.importer import SkillImporter
    from oms.ingestion.payload_store import FilePayloadStore
    from oms.ingestion.sanitiser import RegexSanitiser

    class Transient(Exception):
        pass

    class Retrying:
        """Replays the unit of work once, as a driver does after a transient failure."""
        def __init__(self, inner):
            self.inner, self.attempts = inner, 0

        def atomic(self, name, tenant_id, fn, **kwargs):
            def once(store, queue):
                result = fn(store, queue)
                self.attempts += 1
                if self.attempts == 1:
                    raise Transient()
                return result
            try:
                return self.inner.atomic(name, tenant_id, once, **kwargs)
            except Transient:
                return self.inner.atomic(name, tenant_id, once, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    store, queue = InMemoryGraphStore(), InMemoryReviewQueue()
    repository = Retrying(MemoryWorkflowRepository(store, queue))
    importer = SkillImporter(store, RegexSanitiser(), FilePayloadStore(root=tmp_path / "payloads"), queue,
                             repository=repository)
    for name in ("alpha", "beta"):
        package = tmp_path / "skills" / name
        package.mkdir(parents=True)
        (package / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Use for {name}.\n---\n\n"
                                          f"# {name}\n\n* Keep the {name} log.\n")
    seen = []
    importer.import_directory(tmp_path / "skills", "acme", on_skill=lambda index, total, name: seen.append((index, total)))
    assert repository.attempts == 2
    assert seen == [(1, 2), (2, 2)]
    assert sorted(skill.id for skill in store.skills_for_tenant("acme")) == ["alpha", "beta"]
