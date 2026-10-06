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


# -- a checkout belongs to one repository -----------------------------------------
#
# A checkout keeps the remote it was cloned from. Pushing a different
# repository's publish through it would send one destination's tree to
# another's remote, so a checkout of another repository is refused, never
# reused. The same repository with a new credential is a rotated token, and the
# checkout's remote is simply updated.

def _remote_count(remote: Path) -> int:
    run = subprocess.run(["git", "rev-list", "--count", "main"], cwd=remote,
                         capture_output=True, text=True)
    return int(run.stdout.strip()) if run.returncode == 0 else 0


def test_a_checkout_of_another_repository_is_refused_not_pushed_to(bare, tmp_path):
    from oms.publish.git import GitCommandError
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "--bare", "--quiet", str(other)], check=True)
    work = tmp_path / "checkout"
    publish_to_repo(Publisher(_store()), TENANT, str(bare), work)
    with pytest.raises(GitCommandError) as refused:
        publish_to_repo(Publisher(_store()), TENANT, str(other), work)
    assert "another repository" in str(refused.value) or "different repository" in str(refused.value)
    assert _remote_count(other) == 0


def test_a_rotated_credential_updates_the_checkouts_remote(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("needs git")
    for key in tuple(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    subprocess.run(["git", "init", "--bare", "--quiet", str(remotes / "skills.git")], check=True)
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = Test\n\temail = test@example.org\n"
        f'[url "{remotes}/"]\n\tinsteadOf = https://bot:old@git.example/\n'
        f"\tinsteadOf = https://bot:new@git.example/\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    work = tmp_path / "checkout"
    publish_to_repo(Publisher(_store()), TENANT, "https://bot:old@git.example/skills.git", work)
    store = _store()
    store.upsert_skill(Skill(id="year-end", name="year-end", description="Guidance.",
                             domain="finance", tenant_id=TENANT))
    store.upsert_rule(Rule(id="year-end-rule", body="Close the year.", tenant_id=TENANT))
    store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id="year-end-rule", to_id="year-end"))
    gate, pushed = publish_to_repo(Publisher(store), TENANT,
                                   "https://bot:new@git.example/skills.git", work)
    assert gate.passed and pushed
    origin = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=work,
                            capture_output=True, text=True).stdout.strip()
    assert origin == "https://bot:new@git.example/skills.git"


# A server names its checkout relative to its working folder, and an extension
# keeps one checkout per destination inside a folder of its own. The clone runs
# in the checkout's parent folder, so a relative name must still land where it
# was named, not one level further down.

def test_a_relative_checkout_in_a_new_folder_is_cloned_where_it_was_named(
        bare, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    checkout = Path("checkouts") / "finance"

    gate, pushed = publish_to_repo(Publisher(_store()), TENANT, str(bare), checkout)

    assert gate.passed and pushed
    assert (tmp_path / "checkouts" / "finance" / ".git").is_dir()
    assert not (tmp_path / "checkouts" / "checkouts").exists()
    assert _remote_count(bare) == 1
