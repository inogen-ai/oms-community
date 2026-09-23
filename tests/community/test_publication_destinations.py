"""Publication choices must change real output without exposing server files."""
from dataclasses import replace
from pathlib import Path
import subprocess

from fastapi.testclient import TestClient
import pytest

from oms.community.app import build_memory
from oms.community.publication import destination_settings
from oms.domain.models import Edge, Rule, Skill
from oms.domain.types import EdgeType
from oms.web.api import create_app


@pytest.fixture
def world(tmp_path):
    services = build_memory(tmp_path / "workspace", "local")
    services.store.upsert_skill(Skill(id="checks", name="Checks", description="Review the publication.", domain="engineering", tenant_id="local"))
    services.store.upsert_rule(Rule(id="check-rule", body="Check the result before publishing.", tenant_id="local"))
    services.store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id="check-rule", to_id="checks"))
    with TestClient(create_app(services), base_url="http://127.0.0.1:4317") as client:
        yield services, client


@pytest.fixture
def deployed(world):
    """The same workspace with a reachable address configured.

    Publishing to a Git remote is refused while the advertised address is a
    loopback address, because the bundle would tell every machine that clones it
    to post its corrections to itself.
    """
    services, _ = world
    reachable = replace(services, settings=replace(services.settings,
                                                   public_url="https://oms.example.org"))
    with TestClient(create_app(reachable), base_url="http://127.0.0.1:4317") as client:
        yield reachable, client


def test_editable_folder_drives_publication_and_preserves_render_settings(world):
    services, client = world
    saved = client.patch("/api/settings", json={"publication_folder": "agents/current", "body_budget": 25})
    assert saved.status_code == 200, saved.text
    assert saved.json()["body_budget"] == 25
    output = Path(saved.json()["publish_root"])
    assert output == services.settings.data_dir / "published/agents/current"
    assert saved.json()["publish_host_path"] == str(output)
    response = client.post("/api/publish")
    assert response.status_code == 200, response.text
    assert response.json()["output"] == str(output)
    assert response.json()["skills"] == 1
    assert (output / "install.sh").is_file()
    assert (output / "skills/checks/SKILL.md").is_file()
    assert (output / ".oms-publication").is_file()
    assert not (output / ".oms-publishing").exists()


@pytest.mark.parametrize("folder", ["../private", "/tmp/elsewhere", "a/../../secret", "a\\b", "a//b", "$(touch bad)", 'bad"folder'])
def test_folder_cannot_escape_its_operator_configured_root(world, folder):
    _, client = world
    assert client.patch("/api/settings", json={"publication_folder": folder}).status_code == 422
    assert client.get("/api/settings").json()["publication_folder"] == ""


def test_symlinks_are_rechecked_when_publishing(world, tmp_path):
    services, client = world
    assert client.patch("/api/settings", json={"publication_folder": "skills"}).status_code == 200
    base = services.settings.data_dir / "published"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / "skills").symlink_to(outside, target_is_directory=True)
    response = client.post("/api/publish")
    assert response.status_code == 422, response.text
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("remote", ["file:///tmp/repo", "/tmp/repo", "--upload-pack=bad", "ext::evil", "https://token@github.com/acme/skills.git", "https://git:secret@github.com/acme/skills.git", "https://github.com/acme/skills.git?token=secret", "https://example.org/skills.git;touch-flag", "ssh://git@example.org/skills|sh"])
def test_remote_does_not_store_credentials_or_enable_local_transports(world, remote):
    services, client = world
    result = client.patch("/api/settings", json={"publication_target": "git", "publication_git_url": remote})
    assert result.status_code == 422
    assert "secret" not in result.text
    assert "publication_git_url" not in services.settings_store.get_or_seed_default("local").body


@pytest.mark.parametrize("branch", ["--all", "../main", "a..b", "refs/.hidden", "branch.lock", "main/"])
def test_invalid_branch_is_rejected(world, branch):
    _, client = world
    assert client.patch("/api/settings", json={"publication_git_branch": branch}).status_code == 422


def test_docker_host_path_is_derived_from_the_operator_mapping(world):
    services, _ = world
    settings = replace(services.settings, publish_root=Path("/published"),
                       publish_host_root="/Users/test/OMS bundles", publish_in_container=True)
    result = destination_settings(settings, services.settings_store, "local", {"publication_folder": "team"})
    assert result["publish_host_path"] == "/Users/test/OMS bundles/team"
    assert result["publish_root"] == "/published/team"
    missing = destination_settings(replace(settings, publish_host_root=None), services.settings_store, "local")
    assert missing["publish_host_path"] is None


def test_launcher_metadata_guides_host_setup_without_making_mounts_browser_editable(world, monkeypatch):
    from oms.settings.core import CoreSettings
    services, client = world
    monkeypatch.setenv("OMS_PUBLISH_HOST_HOME", "/home/test")
    monkeypatch.setenv("OMS_LOCAL_LAUNCHER_DIR", "/home/test/OMS Community")
    monkeypatch.setenv("OMS_COMPOSE_PROJECT_NAME", "my-workspace")
    config = destination_settings(CoreSettings.from_env(), services.settings_store, "local")
    assert config["publish_host_home"] == "/home/test"
    assert config["publish_launcher_dir"] == "/home/test/OMS Community"
    assert config["publish_compose_project"] == "my-workspace"
    assert client.patch("/api/settings", json={"publish_host_root": "/arbitrary/host"}).status_code == 422


def test_publication_lock_lives_in_data_even_when_output_parent_is_read_only(world, tmp_path):
    services, _ = world
    parent = tmp_path / "read-only-parent"
    output = parent / "published"
    output.mkdir(parents=True)
    parent.chmod(0o555)
    try:
        configured = replace(services, settings=replace(services.settings, publish_root=output))
        with TestClient(create_app(configured), base_url="http://127.0.0.1:4317") as client:
            result = client.post("/api/publish")
        assert result.status_code == 200, result.text
        assert (output / "install.sh").is_file()
        assert not (parent / "published.lock").exists()
        assert list((services.settings.data_dir / "publication-locks").glob("*.lock"))
    finally:
        parent.chmod(0o755)


def test_git_publication_pushes_to_the_selected_remote_and_is_idempotent(deployed, tmp_path, monkeypatch):
    from oms.community import publication
    from oms.publish.git import publish_to_repo as actual_publish
    services, client = deployed
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
    # The HTTP contract forbids file:// remotes. Only the transport in this
    # isolated integration test is redirected to a temporary bare repository.
    calls = []
    def transport(publisher, tenant, url, work_dir, branch):
        calls.append((url, work_dir, branch))
        return actual_publish(publisher, tenant, str(remote), work_dir, branch)
    monkeypatch.setattr(publication, "publish_to_repo", transport)
    saved = client.patch("/api/settings", json={"publication_target": "git", "publication_git_url": "https://example.org/skills.git", "publication_git_branch": "main"})
    assert saved.status_code == 200, saved.text
    first = client.post("/api/publish")
    assert first.status_code == 200, first.text
    assert first.json()["pushed"] is True
    tree = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main"], check=True, capture_output=True, text=True).stdout
    assert "skills/checks/SKILL.md" in tree
    assert ".oms-publishing" not in tree
    second = client.post("/api/publish")
    assert second.status_code == 200, second.text
    assert second.json()["pushed"] is False
    assert calls[0][0] == "https://example.org/skills.git"
    assert calls[0][1].is_relative_to(services.settings.data_dir)


def test_git_failures_are_actionable_without_exposing_credentials(deployed, monkeypatch):
    from oms.community import publication
    from oms.publish.git import GitCommandError
    _, client = deployed
    client.patch("/api/settings", json={"publication_target": "git", "publication_git_url": "git@example.org:team/skills.git"})
    def failed(*args):
        raise GitCommandError("https://secret-token@example.org: password-secret")
    monkeypatch.setattr(publication, "publish_to_repo", failed)
    result = client.post("/api/publish")
    assert result.status_code == 422
    assert "Git access" in result.text
    assert "secret" not in result.text
