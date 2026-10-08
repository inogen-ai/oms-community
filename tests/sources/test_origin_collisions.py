from datetime import timedelta


def test_refused_retarget_is_not_completed_by_old_ref_check(source_world):
    source_world.open_local_review()
    before = source_world.sources.get_local_stream(source_world.skill)
    refused = source_world.retarget("tag", "v2")
    assert refused.state == "blocked"
    assert source_world.sources.get_binding(source_world.skill).ref.kind == "branch"
    assert source_world.sources.get_local_stream(source_world.skill) == before
    assert source_world.retry_needed("retarget")
    source_world.resolve_current()
    source_world.check()
    assert source_world.sources.get_binding(source_world.skill).ref.kind == "branch"
    assert source_world.retry_needed("retarget")


def test_second_local_submission_refuses_and_retains_original_card(source_world):
    first = source_world.open_local_review()
    card = source_world.sources.open_update(source_world.skill)
    base = source_world.sources.get_local_stream(source_world.skill).baseline
    refused = source_world.submit_local(b"---\nname: expenses\ndescription: Another local proposal\n---\n", key="second-local")
    assert refused.state == "blocked"
    assert source_world.sources.open_update(source_world.skill) == card
    assert source_world.sources.get_local_stream(source_world.skill).baseline == base


def test_github_check_refuses_open_local_origin_without_fetching(source_world):
    source_world.open_local_review()
    reads = source_world.reader.reads
    card = source_world.sources.open_update(source_world.skill)
    result = source_world.check()
    assert result.state == "blocked"
    assert source_world.reader.reads == reads
    assert source_world.sources.open_update(source_world.skill) == card
    assert source_world.retry_needed("check")


def test_explicit_first_link_retry_clears_only_its_pending_action(source_world):
    from oms.domain.models import Skill
    from oms.sources.models import LinkRequest, Source
    source_world.store.upsert_skill(Skill(id="expenses", name="Expenses", description="Local", domain="finance",
        tenant_id="acme", import_source_ref="legacy/expenses/SKILL.md"))
    source_world.submit_local(b"---\nname: expenses\ndescription: Local proposal\n---\n")
    source_world.sources.put_source(Source(source_id="target", tenant_id="acme", canonical_url=source_world.incoming.resolved_ref.canonical_url))
    guard = source_world.sources.get_generations(source_world.skill)
    request = LinkRequest(skill=source_world.skill, source_id="target", package_path="", ref=source_world.incoming.resolved_ref,
        expected_content_generation=guard.content, expected_binding_generation=guard.binding)
    refused = source_world.service.link(source_world.context, request, idempotency_key="first-link")
    assert refused.state == "blocked"
    assert source_world.retry_needed("link")
    source_world.resolve_current()
    result = source_world.service.link(source_world.context, request, idempotency_key="retry-link")
    assert result.state == "awaiting_review"
    assert not source_world.retry_needed("link")
