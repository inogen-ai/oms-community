"""Repository facts: fetched at discovery, kept on the source, refreshed hourly on checks (spec section 6)."""
from datetime import timedelta

import httpx
import pytest

from oms.sources.errors import SourceNotFound
from oms.sources.git_reader import GitReader
from oms.sources.github_api import FactsFetch
from oms.sources.links import repository_name
from oms.sources.models import (CheckRequest, DiscoveryRequest, InstallRequest, InstallSelection, RefRequest,
                                UpdateRequest)
from oms.sources.mutation import source_repository
from oms.sources.reads import SourceReads
from oms.sources.service import SourceService
from oms.sources.settings import CredentialProfile, SourceSettings
from tests.sources.test_github_api import NOW, REPOSITORY_JSON, facts

URL = "https://github.com/example/skills"


def _fetcher(world, value=None, calls=None):
    def repository_facts(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if value is None:
            return FactsFetch(None, "not_found")
        return FactsFetch(value.model_copy(update={"fetched_at": kwargs["now"]}))
    world.service.facts = repository_facts


def _broken_fetcher(world):
    def repository_facts(**kwargs):
        raise RuntimeError("api down")
    world.service.facts = repository_facts


def _discover(world, key="discover"):
    return world.service.discover(world.context, DiscoveryRequest(
        actor_id="reviewer", ref=RefRequest(tenant_id="acme", url=URL)), idempotency_key=key)


def _source(world):
    return world.sources.get_source(world.sources.get_binding(world.skill).source_id, tenant_id="acme")


def test_repository_names_are_read_from_the_canonical_url():
    assert repository_name("https://github.com/Obra/SuperPowers.git") == ("obra", "superpowers")
    assert repository_name("https://github.com/obra/superpowers/tree/main/skills") == ("obra", "superpowers")


def test_discovery_carries_the_repository_facts(source_world):
    world = source_world
    calls = []
    _fetcher(world, facts(full_name="example/skills", stars=3), calls)
    result = _discover(world)
    assert result.repository.stars == 3
    assert calls == [{"tenant_id": "acme", "credential_profile_id": None, "canonical_url": URL, "now": world.now}]
    view = SourceReads(world.service, world.blobs).discovery(world.context, result.discovery_id)
    assert (view.repository.name, view.repository.url) == ("example/skills", URL)
    assert [row.code for row in view.repository.concerns] == ["few_stars"]


def test_a_failed_fetch_never_stops_discovery(source_world):
    world = source_world
    _broken_fetcher(world)
    result = _discover(world)
    assert result.repository is None
    view = SourceReads(world.service, world.blobs).discovery(world.context, result.discovery_id)
    assert view.repository.facts is None and view.repository.concerns == ()


def test_install_keeps_the_discovery_facts_on_the_source(source_world):
    world = source_world
    known = facts(full_name="example/skills")
    retained = world.discoveries.get("discovery", tenant_id="acme", actor_id="reviewer")
    world.discoveries.save(retained.model_copy(update={"discovery_id": "with-facts", "repository": known}))
    world.service.install(world.context, InstallRequest(discovery_id="with-facts", selections=(
        InstallSelection(package_path="", local_name="expenses", domain="finance"),)), idempotency_key="install-facts")
    assert _source(world).repository == known


def test_checks_refresh_facts_at_most_hourly(source_world):
    world = source_world
    world.install()
    calls = []
    _fetcher(world, facts(full_name="example/skills"), calls)
    world.check(key="first")
    assert len(calls) == 1 and _source(world).repository.fetched_at == world.now
    world.now += timedelta(minutes=2)
    world.check(key="second")
    assert len(calls) == 1
    world.now += timedelta(hours=1)
    world.check(key="third")
    assert len(calls) == 2 and _source(world).repository.fetched_at == world.now


def test_a_failed_refresh_keeps_the_old_facts_and_the_check_still_runs(source_world):
    world = source_world
    world.install()
    _fetcher(world, facts(full_name="example/skills"))
    world.check(key="first")
    kept = _source(world).repository
    world.now += timedelta(hours=2)
    _broken_fetcher(world)
    assert world.check(key="later").state in {"awaiting_review", "complete"}
    assert _source(world).repository == kept


def test_a_check_naming_a_source_its_skills_are_not_bound_to_fetches_nothing(source_world):
    # Ruling F2: the facts are refreshed only for the source the check's bindings were matched to.
    world = source_world
    world.install()
    calls = []
    _fetcher(world, facts(full_name="other/private"), calls)
    other = _source(world).model_copy(update={"source_id": "other-source", "canonical_url": "https://github.com/other/private",
                                              "credential_profile_id": "private-profile", "repository": None})
    world.repository.metadata("test:other-source", "acme", lambda graph, queue: source_repository(graph).put_source(other))
    with pytest.raises(SourceNotFound):
        world.service.check(world.context, CheckRequest(source_id=other.source_id,
            expected_source_generation=other.generation, skills=(world.skill,)), idempotency_key="foreign-source")
    assert calls == []
    assert world.sources.get_source(other.source_id, tenant_id="acme").repository is None


@pytest.mark.parametrize("failure", ["raises", "refused"])
def test_a_failed_refresh_is_not_tried_again_within_the_hour(source_world, failure):
    # Ruling F3: an unreachable or refusing API is asked at most hourly, not on every check.
    world = source_world
    world.install()
    calls = []

    def repository_facts(**kwargs):
        calls.append(kwargs)
        if failure == "raises":
            raise RuntimeError("api down")
        return FactsFetch(None, "rate_limited")
    world.service.facts = repository_facts
    world.check(key="first")
    assert len(calls) == 1
    world.now += timedelta(minutes=5)
    world.check(key="second")
    assert len(calls) == 1
    world.now += timedelta(hours=1)
    world.check(key="third")
    assert len(calls) == 2


def test_a_recheck_refreshes_stale_facts_through_the_same_throttle(source_world):
    # Ruling F4: "Check safety again" refreshes the facts too, once its binding is matched.
    world = source_world
    calls = []
    _fetcher(world, facts(full_name="example/skills"), calls)
    world.open_update()                      # install, then a check that fetches the facts
    assert len(calls) == 1

    def recheck(key):
        update = world.sources.open_update(world.skill)
        world.service.recheck(world.context, UpdateRequest(update_id=update.update_id, skill=world.skill,
                                                           fingerprint=update.plan.fingerprint), idempotency_key=key)
    recheck("fresh")
    assert len(calls) == 1
    world.now += timedelta(hours=2)
    recheck("stale")
    assert len(calls) == 2 and _source(world).repository.fetched_at == world.now
    # A failed attempt counts, so a second recheck within the hour asks nobody.
    world.service.facts = lambda **kwargs: calls.append(kwargs) or FactsFetch(None, "rate_limited")
    world.now += timedelta(hours=2)
    recheck("refused")
    world.now += timedelta(minutes=5)
    recheck("refused-again")
    assert len(calls) == 3


def test_a_recheck_of_a_local_card_fetches_nothing(source_world):
    # A local upload's card has no repository, so its recheck asks GitHub for nothing.
    world = source_world
    calls = []
    _fetcher(world, facts(full_name="example/skills"), calls)
    world.open_local_review()
    update = world.sources.open_update(world.skill)
    assert update.plan.origin.kind != "github"
    world.now += timedelta(hours=2)
    world.service.recheck(world.context, UpdateRequest(update_id=update.update_id, skill=world.skill,
                                                       fingerprint=update.plan.fingerprint), idempotency_key="recheck-local")
    assert calls == []


def test_facts_written_during_a_check_do_not_make_it_stale(source_world):
    world = source_world
    world.install()
    _fetcher(world)
    source_id = _source(world).source_id

    def refreshed_elsewhere():
        def write(graph, queue):
            sources = source_repository(graph)
            current = sources.get_source(source_id, tenant_id="acme")
            sources.put_source(current.model_copy(update={"repository": facts(full_name="example/skills",
                                                                              fetched_at=world.now)}))
        world.repository.metadata("test:facts", "acme", write)
    world.reader.before_read = refreshed_elsewhere
    assert world.check(key="concurrent").state in {"awaiting_review", "complete"}
    assert _source(world).repository is not None


def test_the_update_view_carries_repository_facts_and_concerns(source_world):
    world = source_world
    _fetcher(world, facts(full_name="example/skills", stars=3))
    candidate = world.open_update()          # install, then a check that refreshes the facts
    view = SourceReads(world.service, world.blobs).update(world.context, candidate.update.update_id)
    assert view.repository.name == "example/skills" and view.repository.facts.stars == 3
    dumped = view.model_dump(mode="json")["repository"]
    assert dumped["facts"]["licence"] == "MIT"
    assert dumped["concerns"] == [{"format_version": 1, "code": "few_stars", "label": "Fewer than 10 stars"}]


def test_an_update_view_without_facts_still_names_the_repository(source_world):
    world = source_world
    candidate = world.open_update()          # the world's default fetcher returns no facts
    view = SourceReads(world.service, world.blobs).update(world.context, candidate.update.update_id)
    assert view.repository.name == "example/skills" and view.repository.facts is None


def test_a_test_world_never_reaches_github(source_world, tmp_path):
    # Ruling P9: a world's fetcher answers without the network, and a test that swaps in a
    # real GitReader afterwards (as test_canonical_ref_lifecycle.py does) keeps that fetcher.
    world = source_world
    fetcher = world.service.facts
    assert fetcher(tenant_id="acme", credential_profile_id=None, canonical_url=URL, now=world.now) == FactsFetch(
        None, "not_configured")
    world.service.reader = GitReader(SourceSettings(tmp_path / "cache"))
    assert world.service.facts is fetcher


def test_the_service_uses_the_readers_fetcher_unless_it_is_given_one(source_world, tmp_path):
    world = source_world
    reader = GitReader(SourceSettings(tmp_path / "cache"))
    built = SourceService(world.repository, world.service.factory_for, reader, world.service.builder, world.policy,
                          world.discoveries)
    assert built.facts == reader.repository_facts

    def stub(**request):
        return FactsFetch(None, "stub")
    given = SourceService(world.repository, world.service.factory_for, reader, world.service.builder, world.policy,
                          world.discoveries, facts=stub)
    assert given.facts is stub


def test_the_git_reader_sends_the_profile_token_only_for_a_source_with_one(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_SOURCE_TOKEN", "fixture-token")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=REPOSITORY_JSON)
    reader = GitReader(SourceSettings(tmp_path, profiles=(CredentialProfile("team", "FIXTURE_SOURCE_TOKEN"),)),
                       profile_grants=lambda tenant: frozenset({"team"}) if tenant == "acme" else frozenset(),
                       api_transport=httpx.MockTransport(handler), api_resolve=lambda host: ["140.82.112.6"])
    fetched = reader.repository_facts(tenant_id="acme", credential_profile_id="team",
                                      canonical_url="https://github.com/obra/superpowers", now=NOW)
    assert fetched.facts.full_name == "obra/superpowers"
    assert seen[-1].headers["authorization"] == "Bearer fixture-token"
    reader.repository_facts(tenant_id="acme", credential_profile_id=None,
                            canonical_url="https://github.com/obra/superpowers", now=NOW)
    assert "authorization" not in seen[-1].headers
    refused = reader.repository_facts(tenant_id="other", credential_profile_id="team",
                                      canonical_url="https://github.com/obra/superpowers", now=NOW)
    assert (refused.facts, refused.code, len(seen)) == (None, "credential_unavailable", 2)
