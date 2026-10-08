from dataclasses import replace

import pytest

from oms.sources.errors import SourceConflict
from oms.sources.models import LinkRequest, Source


def test_first_link_does_not_invent_baseline_from_local_content(source_world):
    from oms.domain.models import Skill
    source_world.store.upsert_skill(Skill(id="expenses", name="Expenses", description="Local correction", domain="finance",
        tenant_id="acme", import_source_ref="legacy/expenses/SKILL.md"))
    source = Source(source_id="existing", tenant_id="acme", canonical_url=source_world.incoming.resolved_ref.canonical_url)
    source_world.sources.put_source(source)
    guard = source_world.sources.get_generations(source_world.skill)
    request = LinkRequest(skill=source_world.skill, source_id=source.source_id, package_path="",
        ref=source_world.incoming.resolved_ref, expected_content_generation=guard.content, expected_binding_generation=guard.binding)
    result = source_world.service.link(source_world.context, request, idempotency_key="link")
    binding = source_world.sources.get_binding(source_world.skill)
    assert binding.baseline is None
    assert binding.first_reconciliation
    assert source_world.description() == "Local correction"
    assert result.outcomes[0].state == "awaiting_review"


def test_native_skill_without_import_origin_cannot_link(source_world):
    from oms.domain.models import Skill
    source_world.store.upsert_skill(Skill(id="expenses", name="Expenses", description="Local", domain="finance", tenant_id="acme"))
    source = Source(source_id="existing", tenant_id="acme", canonical_url=source_world.incoming.resolved_ref.canonical_url)
    source_world.sources.put_source(source)
    request = LinkRequest(skill=source_world.skill, source_id=source.source_id, package_path="", ref=source_world.incoming.resolved_ref,
                          expected_content_generation=0, expected_binding_generation=0)
    with pytest.raises(SourceConflict):
        source_world.service.link(source_world.context, request, idempotency_key="link")
    assert source_world.reader.reads == 0


def test_unlink_preserves_local_stream_content_and_source_evidence(source_world):
    from oms.sources.models import LocalStream, OriginRef, UnlinkRequest
    candidate = source_world.open_update()
    original = source_world.sources.get_binding(source_world.skill)
    local_origin = OriginRef(skill=source_world.skill, kind="local", origin_id="local", generation=0)
    stream = LocalStream(origin=local_origin)
    source_world.sources.put_local_stream(stream)
    guard = source_world.sources.get_generations(source_world.skill)
    request = UnlinkRequest(skill=source_world.skill, expected_content_generation=guard.content, expected_binding_generation=guard.binding)
    source_world.service.unlink(source_world.context, request, idempotency_key="unlink")
    assert source_world.description() == "First description"
    assert source_world.sources.get_local_stream(source_world.skill) == stream
    binding = source_world.sources.get_binding(source_world.skill)
    assert not binding.active
    assert binding.origin.generation == guard.binding + 1
    assert binding.baseline == original.baseline
    assert source_world.sources.get_snapshot(original.baseline) is not None
    assert source_world.sources.get_update(candidate.update.update_id, tenant_id="acme").status == "closed"


def test_unlink_fences_check_already_fetching(source_world):
    from oms.sources.models import UnlinkRequest
    from oms.sources.errors import StaleMutation
    source_world.install()
    guard = source_world.sources.get_generations(source_world.skill)
    request = UnlinkRequest(skill=source_world.skill, expected_content_generation=guard.content, expected_binding_generation=guard.binding)
    source_world.reader.before_read = lambda: source_world.service.unlink(source_world.context, request, idempotency_key="unlink")
    with pytest.raises(StaleMutation):
        source_world.check()
    assert source_world.sources.open_update(source_world.skill) is None
    assert not source_world.sources.get_binding(source_world.skill).active


def test_local_stream_revisions_count_as_one_recorded_import_origin(source_world):
    from datetime import datetime, timezone
    from oms.domain.models import Edge, Rule, Skill, Transaction
    from oms.domain.types import EdgeType, SignalType, SourceRuntime
    from oms.sources.models import LocalStream, OriginRef
    graph = source_world.store
    graph.upsert_skill(Skill(id="expenses", name="Expenses", description="Local", domain="finance", tenant_id="acme", import_source_ref="source:local:stream"))
    source_world.sources.put_local_stream(LocalStream(origin=OriginRef(skill=source_world.skill, kind="local", origin_id="local:stream", generation=0)))
    graph.upsert_rule(Rule(id="existing", body="Keep receipts", tenant_id="acme"))
    graph.attach_edge(Edge(EdgeType.BELONGS_TO, "existing", "expenses"), tenant_id="acme")
    for index, revision in enumerate(("a" * 64, "b" * 64)):
        txn = Transaction(id=f"import-{index}", tenant_id="acme", signal_type=SignalType.SKILL_IMPORT,
            source_runtime=SourceRuntime.MANUAL, sanitised_payload_ref="retained", timestamp=datetime.now(timezone.utc),
            source_ref="source:local:stream:" + revision, workflow_state="applied")
        graph.upsert_transaction(txn)
        graph.attach_edge(Edge(EdgeType.DERIVED_FROM, "existing", txn.id), tenant_id="acme")
    source_world.sources.put_source(Source(source_id="target", tenant_id="acme", canonical_url=source_world.incoming.resolved_ref.canonical_url))
    result = source_world.service.link(source_world.context, LinkRequest(skill=source_world.skill, source_id="target",
        package_path="", ref=source_world.incoming.resolved_ref, expected_content_generation=0, expected_binding_generation=0), idempotency_key="link")
    assert result.state == "awaiting_review"


def test_source_removal_requires_every_linked_skill_guard(source_world):
    from datetime import timedelta
    from oms.sources.errors import StaleMutation
    from oms.sources.models import DiscoveryResult, DiscoveredPackage, InstallRequest, InstallSelection, RemoveSourceRequest
    source_world.install()
    package = source_world.package("a", "Other").model_copy(update={"package_path": "other"})
    source_world.discoveries.save(DiscoveryResult(discovery_id="other", tenant_id="acme", actor_id="reviewer",
        resolved_ref=package.resolved_ref, expires_at=source_world.now + timedelta(hours=1),
        packages=(DiscoveredPackage(path="other", valid=True),), acquired_packages=(package,)))
    source_world.service.install(source_world.context, InstallRequest(discovery_id="other", selections=(
        InstallSelection(package_path="other", local_name="other", domain="finance"),)), idempotency_key="install-other")
    source = source_world.sources.get_source(source_world.sources.get_binding(source_world.skill).source_id, tenant_id="acme")
    request = RemoveSourceRequest(source_id=source.source_id, expected_source_generation=source.generation,
        expected_generations=(source_world.sources.get_generations(source_world.skill),))
    with pytest.raises(StaleMutation):
        source_world.service.remove_source(source_world.context, request, idempotency_key="omit-other")
    assert source_world.sources.get_binding(source_world.skill).active
    request = request.model_copy(update={"expected_generations": source_world.sources.generations_for_tenant(tenant_id="acme")})
    result = source_world.service.remove_source(source_world.context, request, idempotency_key="remove-all")
    assert len(result.outcomes) == 2
    assert len(source_world.store.skills_for_tenant("acme")) == 2
    assert not source_world.sources.get_binding(source_world.skill).active


def test_new_skill_lifetime_links_without_inheriting_retired_binding(source_world):
    from oms.domain.models import Skill
    from oms.skills.service import SkillAdminService
    from oms.sources.errors import SourceNotFound
    candidate = source_world.open_update()
    old = source_world.sources.get_binding(source_world.skill)
    SkillAdminService(store=source_world.store, repository=source_world.repository).delete_skill("expenses", "acme")

    def recreate(graph, queue):
        graph.upsert_skill(Skill(id="expenses", name="New expenses", description="New local content", domain="finance", tenant_id="acme",
            import_source_ref="new/expenses/SKILL.md"))
    source_world.repository.atomic("recreate", "acme", recreate)
    with pytest.raises(SourceNotFound):
        source_world.service.get_update(source_world.context, candidate.update.update_id)
    guard = source_world.sources.get_generations(source_world.skill)
    request = LinkRequest(skill=source_world.skill, source_id=old.source_id, package_path="", ref=source_world.incoming.resolved_ref,
        expected_content_generation=guard.content, expected_binding_generation=guard.binding)
    source_world.service.link(source_world.context, request, idempotency_key="new-link")
    new = source_world.sources.get_binding(source_world.skill)
    assert new.origin.origin_id != old.origin.origin_id
    assert new.baseline is None
    assert source_world.description() == "New local content"
