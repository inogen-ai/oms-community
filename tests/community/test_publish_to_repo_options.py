"""`publish_to_repo` carries a destination's publish options into the push.

An extension publishing one tenant to several repositories has to be able to
say, per repository, which skills go there, whether it is a skills-only
destination, which extra root files it carries, and what the commit says. A
caller that names none of them must see exactly the call it always made,
because publisher wrappers written against the two-argument `publish` exist.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.models import Edge, Rule, Skill
from oms.domain.types import EdgeType
from oms.publish.gate import GateResult
from oms.publish.git import publish_to_repo
from oms.publish.publisher import Publisher

TENANT = "acme"


@pytest.fixture
def bare(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("needs git")
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (tmp_path / "home").mkdir()
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(remote)], check=True)
    return remote


def _store() -> InMemoryGraphStore:
    store = InMemoryGraphStore()
    for skill_id, domain in (("revenue-recognition", "finance"), ("campaign-briefs", "marketing")):
        store.upsert_skill(Skill(id=skill_id, name=skill_id, description="Guidance.",
                                 domain=domain, tenant_id=TENANT))
        store.upsert_rule(Rule(id=f"{skill_id}-rule", body="Check it twice.", tenant_id=TENANT))
        store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id=f"{skill_id}-rule",
                               to_id=skill_id))
    return store


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def test_the_options_reach_the_published_tree(bare, tmp_path):
    gate, pushed = publish_to_repo(
        Publisher(_store()), TENANT, str(bare), tmp_path / "checkout",
        select=lambda skill: skill.domain == "finance", skills_only=True,
        extra_root_files={"NOTICE.txt": "Finance only.\n"},
        message="Publish acme finance skills")
    assert gate.passed and pushed
    files = set(_git("ls-tree", "-r", "--name-only", "main", cwd=bare).splitlines())
    assert "skills/revenue-recognition/SKILL.md" in files
    assert "NOTICE.txt" in files
    assert not any(name.startswith("skills/campaign-briefs/") for name in files)
    assert "install.sh" not in files
    assert _git("log", "-1", "--format=%s", "main", cwd=bare) == "Publish acme finance skills"


def test_the_default_message_names_the_tenant(bare, tmp_path):
    publish_to_repo(Publisher(_store()), TENANT, str(bare), tmp_path / "checkout")
    assert _git("log", "-1", "--format=%s", "main", cwd=bare) == "Publish acme skills"


class _TwoArgumentPublisher:
    """A wrapper written against the original `publish(tenant, out_dir)`."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def publish(self, tenant, out_dir):
        self.calls.append((tenant, out_dir))
        (Path(out_dir) / "README.md").write_text("hello\n", encoding="utf-8")
        return GateResult(passed=True)


def test_a_caller_naming_no_option_makes_the_original_call(bare, tmp_path):
    wrapper = _TwoArgumentPublisher()
    gate, pushed = publish_to_repo(wrapper, TENANT, str(bare), tmp_path / "checkout")
    assert gate.passed and pushed
    assert wrapper.calls == [(TENANT, tmp_path / "checkout")]
