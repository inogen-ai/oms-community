"""Repository facts from the GitHub REST API, fetched with the git reader's safeguards (spec section 6)."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from oms.sources import github_api
from oms.sources.github_api import MAX_BYTES, facts_stale, fetch_repository_facts, repository_concerns
from oms.sources.models import RepositoryFacts

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
REPOSITORY_JSON = {
    "full_name": "obra/superpowers", "description": "Core skills library", "stargazers_count": 120,
    "forks_count": 12, "archived": False, "disabled": False, "fork": False,
    "license": {"spdx_id": "MIT", "name": "MIT License"}, "created_at": "2025-01-01T00:00:00Z",
    "pushed_at": "2026-10-10T12:00:00Z", "default_branch": "main",
}


def facts(**changes):
    return RepositoryFacts(full_name="obra/superpowers", description=None, stars=120, forks=12, archived=False,
                           disabled=False, is_fork=False, licence="MIT", created_at=NOW - timedelta(days=400),
                           pushed_at=NOW - timedelta(days=2), default_branch="main",
                           fetched_at=NOW).model_copy(update=changes)


def fetch(handler, *, token=None, addresses=("140.82.112.6",)):
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)
    result = fetch_repository_facts("obra", "superpowers", now=NOW, token=token,
                                    transport=httpx.MockTransport(record), resolve=lambda host: list(addresses))
    return result, seen


def test_a_repository_reads_as_facts():
    result, seen = fetch(lambda request: httpx.Response(200, json=REPOSITORY_JSON))
    assert result.code is None
    assert result.facts == RepositoryFacts(
        full_name="obra/superpowers", description="Core skills library", stars=120, forks=12, archived=False,
        disabled=False, is_fork=False, licence="MIT", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        pushed_at=datetime(2026, 10, 10, 12, tzinfo=timezone.utc), default_branch="main", fetched_at=NOW)
    request, = seen
    assert request.method == "GET" and str(request.url) == "https://140.82.112.6/repos/obra/superpowers"
    assert request.headers["host"] == "api.github.com"
    assert request.extensions["sni_hostname"] == "api.github.com"
    assert "authorization" not in request.headers


def test_the_token_is_sent_only_when_given():
    _, seen = fetch(lambda request: httpx.Response(200, json=REPOSITORY_JSON), token="fixture-token")
    assert seen[0].headers["authorization"] == "Bearer fixture-token"


def test_missing_licence_push_and_description_read_as_none():
    result, _ = fetch(lambda request: httpx.Response(200, json={**REPOSITORY_JSON, "license": None,
                                                                "pushed_at": None, "description": None}))
    assert (result.facts.licence, result.facts.pushed_at, result.facts.description) == (None, None, None)
    unrecognised, _ = fetch(lambda request: httpx.Response(200, json={**REPOSITORY_JSON,
                                                                      "license": {"spdx_id": "NOASSERTION"}}))
    assert unrecognised.facts.licence == "NOASSERTION"


@pytest.mark.parametrize(("response", "code"), [
    (lambda request: httpx.Response(404), "not_found"),
    (lambda request: httpx.Response(403), "access_refused"),
    (lambda request: httpx.Response(500), "http_error"),
    (lambda request: httpx.Response(301, headers={"location": "https://elsewhere.example/"}), "redirect_refused"),
    (lambda request: httpx.Response(200, content=b"not json"), "invalid_response"),
    (lambda request: httpx.Response(200, json={**REPOSITORY_JSON, "stargazers_count": "many"}), "invalid_response"),
    (lambda request: httpx.Response(200, content=b"x" * (MAX_BYTES + 1)), "response_too_large"),
    (lambda request: httpx.Response(200, content=iter([b"x" * 600_000, b"x" * 600_000])), "response_too_large"),
])
def test_failures_return_no_facts_and_a_code(response, code):
    result, _ = fetch(response)
    assert (result.facts, result.code) == (None, code)


def test_a_timeout_returns_no_facts():
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)
    result, _ = fetch(slow)
    assert (result.facts, result.code) == (None, "timeout")


@pytest.mark.parametrize("addresses", [("10.0.0.5",), ("140.82.112.6", "127.0.0.1"), ()])
def test_a_non_global_address_is_refused_before_connecting(addresses):
    result, seen = fetch(lambda request: httpx.Response(200, json=REPOSITORY_JSON), addresses=addresses)
    assert (result.facts, result.code, seen) == (None, "unsafe_destination", [])


def test_a_resolver_failure_or_a_bad_name_returns_no_facts():
    def broken(host):
        raise OSError("no resolver")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=REPOSITORY_JSON))
    assert fetch_repository_facts("obra", "superpowers", now=NOW, transport=transport, resolve=broken) == (
        github_api.FactsFetch(None, "dns_failed"))
    assert fetch_repository_facts("obra", "../x", now=NOW, transport=transport, resolve=lambda host: ["1.1.1.1"]) == (
        github_api.FactsFetch(None, "invalid_repository"))


def test_the_git_reader_and_the_api_share_one_address_guard():
    # Ruling P6b: one guard for both readers; the git reader keeps its own error and options.
    from oms.sources.git_reader import AcquisitionError, GitTransport
    from oms.sources.network import UnsafeDestination, pinned_address
    assert pinned_address(["2606:50c0::1", "140.82.112.6"]) == "140.82.112.6"
    assert pinned_address(["2606:50c0::1"]) == "[2606:50c0::1]"
    assert GitTransport.network_options(["140.82.112.6"])[-1] == "http.curloptResolve=github.com:443:140.82.112.6"
    for unsafe in ([], ["10.0.0.5"], ["140.82.112.6", "127.0.0.1"]):
        with pytest.raises(UnsafeDestination):
            pinned_address(unsafe)
        with pytest.raises(AcquisitionError, match="unsafe network destination"):
            GitTransport.network_options(unsafe)


def test_the_client_has_no_proxy_no_redirects_and_a_ten_second_timeout(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    built = {}
    real = httpx.Client

    def spy(**kwargs):
        built.update(kwargs)
        return real(**kwargs)
    monkeypatch.setattr(github_api.httpx, "Client", spy)
    result, _ = fetch(lambda request: httpx.Response(200, json=REPOSITORY_JSON))
    assert result.facts is not None
    assert built["trust_env"] is False and built["follow_redirects"] is False
    assert built["timeout"] == httpx.Timeout(10.0)


def codes(value, tracked="obra/superpowers"):
    return [row.code for row in repository_concerns(value, NOW, tracked_name=tracked)]


def test_concerns_follow_the_thresholds():
    assert codes(facts()) == []
    assert codes(facts(stars=9)) == ["few_stars"] and codes(facts(stars=10)) == []
    assert codes(facts(created_at=NOW - timedelta(days=29, hours=23))) == ["new_repository"]
    assert codes(facts(created_at=NOW - timedelta(days=30))) == []
    assert codes(facts(archived=True, disabled=True, licence=None, is_fork=True)) == [
        "archived", "disabled", "no_licence", "fork"]
    assert codes(facts(full_name="someone/superpowers")) == ["moved"]
    assert codes(facts(full_name="someone/superpowers"), tracked=None) == []


def test_a_name_that_differs_only_in_case_has_not_moved():
    # Review Focus 5 (ruling R7).
    assert codes(facts(full_name="Obra/Superpowers")) == []


def test_concerns_have_plain_labels():
    every = facts(archived=True, disabled=True, licence=None, created_at=NOW, stars=0, is_fork=True,
                  full_name="someone/superpowers")
    assert {row.code: row.label for row in repository_concerns(every, NOW, tracked_name="obra/superpowers")} == {
        "archived": "Archived on GitHub", "disabled": "Disabled on GitHub", "no_licence": "No licence",
        "new_repository": "Created less than 30 days ago", "few_stars": "Fewer than 10 stars",
        "fork": "A fork of another repository", "moved": "Moved on GitHub to someone/superpowers"}


def test_facts_go_stale_after_an_hour():
    assert facts_stale(None, NOW)
    assert not facts_stale(facts(fetched_at=NOW - timedelta(minutes=59)), NOW)
    assert facts_stale(facts(fetched_at=NOW - timedelta(hours=1)), NOW)
