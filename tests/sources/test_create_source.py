from datetime import timedelta

from oms.sources.models import CreateSourceRequest, SourceStatus


def test_create_source_uses_retained_discovery_and_exact_replay_without_fetch(source_world):
    result = source_world.service.create_source(source_world.context, CreateSourceRequest(discovery_id="discovery"), idempotency_key="create-source")
    assert result.source_id
    assert not result.committed
    source = source_world.sources.get_source(result.source_id, tenant_id="acme")
    assert source.canonical_url == "https://github.com/example/skills.git"
    source_world.now += timedelta(hours=2)
    assert source_world.service.create_source(source_world.context, CreateSourceRequest(discovery_id="discovery"), idempotency_key="create-source") == result
    assert source_world.reader.reads == source_world.reader.discovery_reads == 0
    assert source_world.store.skills_for_tenant("acme") == []


def test_create_source_reuses_configured_record_without_metadata_exposure(source_world):
    source_world.install()
    source_id = source_world.sources.get_binding(source_world.skill).source_id
    source = source_world.sources.get_source(source_id, tenant_id="acme")
    configured = source.model_copy(update={"scheduled": True, "generation": 3, "discovery_root": "private-path",
        "status": SourceStatus(state="failed", checked_at=source_world.now)})
    source_world.sources.put_source(configured)
    result = source_world.service.create_source(source_world.context, CreateSourceRequest(discovery_id="discovery"), idempotency_key="create-source")
    assert result.source_id == source_id
    assert source_world.sources.get_source(source_id, tenant_id="acme") == configured
    assert "private-path" not in result.model_dump_json()


def test_create_source_rechecks_expiry_after_commit_profile_admission(source_world, monkeypatch):
    import pytest
    from oms.sources.discovery import DiscoveryAccessError
    from oms.sources.installation import source_from_discovery
    discovery = source_world.discoveries.get("discovery", tenant_id="acme", actor_id="reviewer")
    source_id = source_from_discovery(discovery).source_id
    discovery = discovery.model_copy(update={"discovery_id": "short-lived", "expires_at": source_world.now + timedelta(minutes=1)})
    source_world.discoveries.save(discovery)
    events = source_world.sources.pending_events(tenant_id="acme", now=source_world.now)
    calls = 0

    def admit(context, profile):
        nonlocal calls
        calls += 1
        if calls == 2:
            source_world.now += timedelta(minutes=2)
    monkeypatch.setattr(source_world.policy, "admit_profile", admit)
    with pytest.raises(DiscoveryAccessError):
        source_world.service.create_source(source_world.context, CreateSourceRequest(discovery_id=discovery.discovery_id), idempotency_key="expired")
    assert source_world.sources.get_source(source_id, tenant_id="acme") is None
    assert source_world.sources.pending_events(tenant_id="acme", now=source_world.now) == events
