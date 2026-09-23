"""Independent checks for the bounded, tenant-scoped dashboard history feed.

Run against an installed candidate wheel. The repository source is never added
to the Python path, and the Neo4j fixture owns a fresh disposable database.
"""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest

from oms.community.app import build_community, build_memory
from oms.domain.models import Skill, SkillVersion
from oms.domain.types import SkillVersionCause
from oms.settings.core import CoreSettings
from oms.web.api import create_app
from tests.community.test_critic_workflow import critic_neo4j_driver  # noqa: F401


API_URL = "http://127.0.0.1:4317"
ORIGIN = {"Origin": "http://127.0.0.1:4318"}
AT = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
SUMMARY_FIELDS = {
    "id", "skill_id", "skill_name", "at", "cause", "actor_person_id",
    "detail", "revision",
}
SNAPSHOT_MARKER = "THIS_FULL_SNAPSHOT_MUST_NOT_TRAVEL_WITH_THE_DASHBOARD"


@dataclass
class Feed:
    services: object
    client: TestClient
    adapter: str


@pytest.fixture(params=["memory", pytest.param("neo4j", marks=pytest.mark.integration)])
def feed(request, tmp_path):
    data = tmp_path / "feed-data"
    if request.param == "memory":
        services = build_memory(data, tenant="acme")
    else:
        driver = request.getfixturevalue("critic_neo4j_driver")
        with driver.session() as session:
            session.run("MATCH (n) DETACH DELETE n").consume()
        services = build_community(CoreSettings(data_dir=data, tenant_id="acme"), driver)
    with TestClient(create_app(services), base_url=API_URL) as client:
        yield Feed(services, client, request.param)


def _skill(feed, skill_id="expenses", *, tenant="acme", name=None):
    skill = Skill(id=skill_id, name=name or skill_id, description="Process guidance",
                  domain="finance", tenant_id=tenant)
    feed.services.store.upsert_skill(skill)
    return skill


def _version(feed, version_id, skill_id="expenses", *, tenant="acme", at=AT,
             cause=SkillVersionCause.CONSOLE_EDIT, actor=None, detail=None):
    version = SkillVersion(
        id=version_id, skill_id=skill_id, tenant_id=tenant, at=at,
        revision=f"revision-{version_id}", cause=cause,
        actor_person_id=actor, detail=detail,
        parts_json='[{"text":"' + SNAPSHOT_MARKER + '"}]',
        metadata_json='{"name":"stale snapshot name"}',
        rules_json='[{"body":"' + SNAPSHOT_MARKER + '"}]',
    )
    feed.services.store.append_skill_version(version)
    return version


def _rows(feed, **params):
    response = feed.client.get("/api/skill-changes", params=params)
    assert response.status_code == 200, response.text
    assert isinstance(response.json(), list), "the summary feed is a simple array"
    return response.json()


def _ordered_versions(feed, count=17):
    skills = [_skill(feed, f"skill-{index}") for index in range(4)]
    # Interleave IDs in groups of equal timestamps. Sorting only by time or
    # insertion order gives a different answer at the eight-row boundary.
    versions = []
    for index in [*range(0, count, 2), *range(1, count, 2)]:
        versions.append(_version(feed, f"version-{index:03d}",
            skills[index % len(skills)].id, at=AT + timedelta(minutes=index // 4)))
    return sorted(versions, key=lambda version: (version.at, version.id), reverse=True)


def test_empty_history_is_an_empty_summary_array(feed):
    assert _rows(feed) == []
    assert feed.services.store.recent_skill_changes("acme") == []
    _skill(feed)
    assert _rows(feed) == [], "reading the dashboard must not synthesise a baseline snapshot"


def test_latest_eight_changes_are_globally_ordered_with_a_stable_id_tie_breaker(feed):
    expected = _ordered_versions(feed)
    for _ in range(2):
        rows = _rows(feed)
        assert [row["id"] for row in rows] == [version.id for version in expected[:8]]
        assert all(set(row) == SUMMARY_FIELDS for row in rows)
    summaries = feed.services.store.recent_skill_changes("acme", limit=3)
    assert [row.id for row in summaries] == [version.id for version in expected[:3]]
    assert all(set(asdict(row)) == SUMMARY_FIELDS for row in summaries)


@pytest.mark.parametrize("requested_limit", [1, 10_000])
def test_http_limit_cannot_change_the_fixed_eight_row_dashboard_window(feed, requested_limit):
    expected = _ordered_versions(feed)
    rows = _rows(feed, limit=requested_limit)
    assert [row["id"] for row in rows] == [version.id for version in expected[:8]]


def test_both_version_and_current_skill_must_belong_to_the_workspace_before_limiting(feed):
    valid = _ordered_versions(feed, count=12)
    foreign = _skill(feed, "foreign-skill", tenant="other", name="Private foreign name")
    doomed = _skill(feed, "deleted-skill")
    _version(feed, "deleted-history", doomed.id)
    feed.services.store.delete_skill(doomed.id)

    # Corrupt or historical dangling references must not leak, and must be
    # filtered before LIMIT rather than crowding all valid changes out.
    for index in range(12):
        newer = AT + timedelta(days=index + 1)
        _version(feed, f"foreign-{index}", foreign.id, tenant="other", at=newer)
        _version(feed, f"foreign-version-local-skill-{index}", "skill-0",
                 tenant="other", at=newer)
        _version(feed, f"local-version-foreign-skill-{index}", foreign.id, at=newer)
        _version(feed, f"dangling-{index}", doomed.id, at=newer)
    assert feed.services.store.get_skill(doomed.id) is None
    rows = _rows(feed)
    assert [row["id"] for row in rows] == [version.id for version in valid[:8]]
    assert "Private foreign name" not in str(rows)
    assert [row.id for row in feed.services.store.recent_skill_changes("acme")] == [
        version.id for version in valid[:8]
    ]
    other = feed.services.store.recent_skill_changes("other")
    assert {row.skill_id for row in other} == {foreign.id}
    assert len(other) == 8


@pytest.mark.parametrize("parameter", ["tenant", "tenant_id"])
def test_the_http_feed_cannot_select_a_different_workspace(feed, parameter):
    _skill(feed)
    _version(feed, "ours")
    _skill(feed, "foreign", tenant="other")
    _version(feed, "theirs", "foreign", tenant="other")
    response = feed.client.get("/api/skill-changes", params={parameter: "other"})
    assert response.status_code == 400, response.text
    assert _rows(feed)[0]["id"] == "ours"


def test_summaries_keep_current_skill_names_and_truthful_optional_attribution(feed):
    skill = _skill(feed, name="Old title")
    unproven = _version(feed, "unknown-actor", actor=None, detail=None)
    known = _version(feed, "known-actor", at=AT + timedelta(minutes=1),
        actor="local-reviewer", cause=SkillVersionCause.RULE_EDIT, detail="manual reinforce")
    feed.services.store.upsert_skill(replace(skill, name="Current title"))
    rows = _rows(feed)
    assert [row["id"] for row in rows] == [known.id, unproven.id]
    assert {row["skill_name"] for row in rows} == {"Current title"}
    assert rows[0]["actor_person_id"] == "local-reviewer"
    assert rows[0]["cause"] == "rule_edit" and rows[0]["detail"] == "manual reinforce"
    assert rows[1]["actor_person_id"] is None and rows[1]["detail"] is None
    assert datetime.fromisoformat(rows[0]["at"].replace("Z", "+00:00")) == known.at
    assert rows[0]["revision"] == known.revision


def _assert_projected_value(value):
    from neo4j.graph import Node, Relationship, Path as GraphPath
    assert not isinstance(value, (Node, Relationship, GraphPath)), (
        "the dashboard fetched a complete graph entity instead of summary columns")
    if isinstance(value, dict):
        assert not {"parts_json", "metadata_json", "rules_json"} & set(value)
        for nested in value.values():
            _assert_projected_value(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_projected_value(nested)
    elif isinstance(value, str):
        assert SNAPSHOT_MARKER not in value


class _ProjectedSession:
    def __init__(self, session, queries):
        self.session, self.queries = session, queries

    def __enter__(self):
        self.session.__enter__()
        return self

    def __exit__(self, *args):
        return self.session.__exit__(*args)

    def run(self, query, *args, **kwargs):
        self.queries.append(str(query))
        result = self.session.run(query, *args, **kwargs)
        first = result.peek()
        if first is not None:
            for value in first.values():
                _assert_projected_value(value)
        return result


class _ProjectedDriver:
    def __init__(self, driver):
        self.driver, self.queries = driver, []

    def session(self, *args, **kwargs):
        return _ProjectedSession(self.driver.session(*args, **kwargs), self.queries)


def test_dashboard_read_projects_summaries_without_full_history_or_document_fan_out(feed, monkeypatch):
    expected = _ordered_versions(feed, count=33)
    store = feed.services.store
    traced = None
    if feed.adapter == "neo4j":
        traced = _ProjectedDriver(store._driver)
        monkeypatch.setattr(store, "_driver", traced)

    def unexpected_detail(*args, **kwargs):
        raise AssertionError("the recent-change feed requested a full skill document or history")

    for method in ("skill_versions", "get_skill_version", "latest_skill_version",
                   "rules_for_skill", "sections_for_skill", "artefacts_for_skill",
                   "skills_for_tenant"):
        monkeypatch.setattr(store, method, unexpected_detail)
    if hasattr(store, "_skill_version_from_node"):
        monkeypatch.setattr(store, "_skill_version_from_node", unexpected_detail)
    monkeypatch.setattr(feed.services.history, "capture_required", unexpected_detail)
    rows = _rows(feed)
    assert [row["id"] for row in rows] == [version.id for version in expected[:8]]
    assert all(set(row) == SUMMARY_FIELDS for row in rows)
    assert SNAPSHOT_MARKER not in str(rows)
    if traced is not None:
        assert len(traced.queries) == 1, "a single projected query should serve the whole feed"


def test_real_manual_reinforcement_is_reported_as_such_and_keeps_the_skill_link(feed):
    created = feed.client.post("/api/skills", headers=ORIGIN,
        json={"name": "Expense process", "description": "Finance guidance", "domain": "finance"})
    assert created.status_code == 201, created.text
    skill_id = created.json()["id"]
    wording = "Keep original receipts with every expense claim."
    first = feed.client.post("/api/ingest", headers=ORIGIN, json={
        "transaction_id": "feed-create", "correction": wording, "skill_hint": skill_id,
    })
    assert first.status_code in (200, 202), first.text
    accepted = feed.client.post("/api/review/feed-create/decision", headers=ORIGIN,
        json={"action": "create", "body": wording, "skill_ids": [skill_id]})
    assert accepted.status_code == 200, accepted.text
    rule_id = accepted.json()["rule_id"]
    second = feed.client.post("/api/ingest", headers=ORIGIN, json={
        "transaction_id": "feed-reinforce", "correction": wording, "skill_hint": skill_id,
    })
    assert second.status_code in (200, 202), second.text
    reinforced = feed.client.post("/api/review/feed-reinforce/decision", headers=ORIGIN,
        json={"action": "reinforce", "body": wording, "skill_ids": [skill_id], "rule_id": rule_id})
    assert reinforced.status_code == 200, reinforced.text
    rows = _rows(feed)
    assert rows[0]["detail"] == "manual reinforce" and rows[0]["cause"] == "rule_edit"
    assert rows[0]["skill_id"] == skill_id and rows[0]["skill_name"] == "Expense process"
    assert any(row["detail"] == "manual create" for row in rows)
    assert all(set(row) == SUMMARY_FIELDS for row in rows)
