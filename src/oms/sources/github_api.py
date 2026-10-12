"""Facts about a GitHub repository from the REST API, fetched with the git reader's safeguards.

One read-only GET to api.github.com, pinned to a resolved global address with SNI and
certificate checks for api.github.com, no redirects, no proxy, a 10 second deadline and
a 1 MB body cap. Any failure returns no facts and a code; it never stops discovery, an
install or a check (spec D5).
"""
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
import concurrent.futures
import json
import re
import socket
import threading
import time
from typing import Literal

import httpx
from pydantic import ValidationError

from oms.sources.network import pinned_address
from oms.sources.primitives import NonEmpty, Record, RepositoryFacts

API_HOST = "api.github.com"
TIMEOUT_SECONDS = 10.0
MAX_BYTES = 1024 * 1024
NEW_REPOSITORY = timedelta(days=30)
FEW_STARS = 10
REFRESH_AFTER = timedelta(hours=1)
_TOKEN = re.compile(r"[\x21-\x7e]+")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
# DNS has no timeout of its own; it runs here and is abandoned at the deadline.
_RESOLVER = ThreadPoolExecutor(max_workers=2, thread_name_prefix="github-api-dns")


@dataclass(frozen=True)
class FactsFetch:
    facts: RepositoryFacts | None
    code: str | None = None


def resolve_addresses(host: str) -> list[str]:
    return sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


def _facts(data: object, now: datetime) -> RepositoryFacts:
    if not isinstance(data, dict):
        raise ValueError("repository response is not an object")
    licence = data.get("license")
    spdx = licence.get("spdx_id") if isinstance(licence, dict) else None
    description = data.get("description")
    return RepositoryFacts(
        full_name=data["full_name"], description=description[:500] if isinstance(description, str) else None,
        stars=data["stargazers_count"], forks=data["forks_count"], archived=data["archived"],
        disabled=data["disabled"], is_fork=data["fork"], licence=spdx if isinstance(spdx, str) and spdx else None,
        created_at=data["created_at"], pushed_at=data.get("pushed_at"), default_branch=data["default_branch"],
        fetched_at=now)


def _exchange(url: str, headers: dict[str, str], transport: httpx.BaseTransport | None, timeout: float,
              max_bytes: int, now: datetime, abandoned: threading.Event) -> FactsFetch:
    """The HTTP exchange and the parse; any failure is a code. Stops reading once the caller has given up."""
    body = bytearray()
    try:
        with httpx.Client(transport=transport, trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(timeout)) as client:
            with client.stream("GET", url, headers=headers, extensions={"sni_hostname": API_HOST}) as response:
                if 300 <= response.status_code < 400:
                    return FactsFetch(None, "redirect_refused")
                if response.status_code == 404:
                    return FactsFetch(None, "not_found")
                if response.status_code in (401, 403, 429):
                    return FactsFetch(None, "access_refused")
                if response.status_code != 200:
                    return FactsFetch(None, "http_error")
                declared = response.headers.get("content-length")
                if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                    return FactsFetch(None, "response_too_large")
                for chunk in response.iter_bytes():
                    if abandoned.is_set():
                        return FactsFetch(None, "timeout")
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        return FactsFetch(None, "response_too_large")
    except httpx.TimeoutException:
        return FactsFetch(None, "timeout")
    except httpx.HTTPError:
        return FactsFetch(None, "network_failed")
    except Exception:
        return FactsFetch(None, "network_failed")
    try:
        return FactsFetch(_facts(json.loads(body), now))
    except (ValueError, TypeError, KeyError, ValidationError):
        return FactsFetch(None, "invalid_response")
    except Exception:   # for example RecursionError from deeply nested JSON
        return FactsFetch(None, "invalid_response")


def fetch_repository_facts(owner: str, repo: str, *, now: datetime, token: str | None = None,
                           transport: httpx.BaseTransport | None = None,
                           resolve: Callable[[str], list[str]] = resolve_addresses,
                           timeout: float = TIMEOUT_SECONDS, max_bytes: int = MAX_BYTES) -> FactsFetch:
    """One wall-clock deadline of `timeout` seconds bounds DNS, connect, TLS, headers and body together."""
    if any(not _NAME.fullmatch(name) or name in {".", ".."} for name in (owner, repo)):
        return FactsFetch(None, "invalid_repository")
    if token and not _TOKEN.fullmatch(token):
        return FactsFetch(None, "credential_invalid")
    deadline = time.monotonic() + timeout
    lookup = _RESOLVER.submit(resolve, API_HOST)
    try:
        addresses = lookup.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        lookup.cancel()
        return FactsFetch(None, "timeout")
    except Exception:
        return FactsFetch(None, "dns_failed")
    try:
        host = pinned_address(addresses)
    except ValueError:   # UnsafeDestination, or a resolver answer that is not an address
        return FactsFetch(None, "unsafe_destination")
    headers = {"Host": API_HOST, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "oms-skill-sources", "Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return FactsFetch(None, "timeout")
    # A private holder per call: a worker that outlives the deadline writes only to its own list.
    outcome: list[FactsFetch] = []
    abandoned = threading.Event()
    url = f"https://{host}/repos/{owner}/{repo}"

    def work() -> None:
        outcome.append(_exchange(url, headers, transport, remaining, max_bytes, now, abandoned))
    worker = threading.Thread(target=work, name="github-api-fetch", daemon=True)
    worker.start()
    worker.join(max(deadline - time.monotonic(), 0))
    if worker.is_alive():
        abandoned.set()
        return FactsFetch(None, "timeout")
    return outcome[0] if outcome else FactsFetch(None, "network_failed")


class RepositoryConcern(Record):
    code: Literal["archived", "disabled", "no_licence", "new_repository", "few_stars", "fork", "moved"]
    label: NonEmpty


def repository_concerns(facts: RepositoryFacts, now: datetime, *,
                        tracked_name: str | None = None) -> tuple[RepositoryConcern, ...]:
    """What a reviewer should know about the repository, computed at read time (spec section 6)."""
    found = []
    if facts.archived:
        found.append(RepositoryConcern(code="archived", label="Archived on GitHub"))
    if facts.disabled:
        found.append(RepositoryConcern(code="disabled", label="Disabled on GitHub"))
    if facts.licence is None:
        found.append(RepositoryConcern(code="no_licence", label="No licence"))
    if now - facts.created_at < NEW_REPOSITORY:
        found.append(RepositoryConcern(code="new_repository", label="Created less than 30 days ago"))
    if facts.stars < FEW_STARS:
        found.append(RepositoryConcern(code="few_stars", label="Fewer than 10 stars"))
    if facts.is_fork:
        found.append(RepositoryConcern(code="fork", label="A fork of another repository"))
    if tracked_name is not None and facts.full_name.casefold() != tracked_name.casefold():
        found.append(RepositoryConcern(code="moved", label=f"Moved on GitHub to {facts.full_name}"))
    return tuple(found)


def facts_stale(facts: RepositoryFacts | None, now: datetime) -> bool:
    return facts is None or now - facts.fetched_at >= REFRESH_AFTER
