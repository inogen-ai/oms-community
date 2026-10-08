from datetime import timedelta

import pytest

from oms.sources.discovery import DiscoveryAccessError
from oms.sources.errors import SourceConflict
from oms.sources.models import DiscoveryRequest, RefRequest


def request():
    return DiscoveryRequest(actor_id="reviewer", ref=RefRequest(tenant_id="acme", url="https://github.com/example/skills"))


def test_discovery_exact_replay_retains_original_ref_without_fetch(source_world):
    first = source_world.service.discover(source_world.context, request(), idempotency_key="discover")
    source_world.incoming = source_world.package("c", "Later upstream")
    replay = source_world.service.discover(source_world.context, request(), idempotency_key="discover")
    assert replay == first
    assert source_world.reader.discovery_reads == 1


def test_expired_discovery_replay_refuses_without_refresh(source_world):
    source_world.service.discover(source_world.context, request(), idempotency_key="discover")
    source_world.now += timedelta(hours=2)
    with pytest.raises(DiscoveryAccessError):
        source_world.service.discover(source_world.context, request(), idempotency_key="discover")
    assert source_world.reader.discovery_reads == 1


def test_discovery_changed_request_cannot_reuse_key(source_world):
    source_world.service.discover(source_world.context, request(), idempotency_key="discover")
    changed = request().model_copy(update={"discovery_root": "elsewhere"})
    with pytest.raises(SourceConflict):
        source_world.service.discover(source_world.context, changed, idempotency_key="discover")
    assert source_world.reader.discovery_reads == 1


def test_personal_profile_admission_precedes_acquisition(source_world):
    def denied(*args):
        raise PermissionError("profile access denied")
    source_world.discoveries.authorise = denied
    with pytest.raises(PermissionError):
        source_world.service.discover(source_world.context, request(), idempotency_key="discover")
    assert source_world.reader.discovery_reads == 0


def test_personal_profile_revocation_prevents_discovery_commit(source_world):
    frozen = source_world.discoveries.get("discovery", tenant_id="acme", actor_id="reviewer").model_copy(update={"discovery_id": "revoked"})

    def revoked(request):
        def deny(*args):
            raise PermissionError("profile revoked")
        source_world.discoveries.authorise = deny
        return frozen
    source_world.reader.discover = revoked
    with pytest.raises(PermissionError):
        source_world.service.discover(source_world.context, request(), idempotency_key="discover")
    assert source_world.sources.get_discovery("revoked", tenant_id="acme", actor_id="reviewer", now=source_world.now) is None
