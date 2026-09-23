"""The community edition must tell a published agent where to post corrections.

Contribution is configured by one variable, OMS_PUBLIC_URL, the same variable
the commercial edition reads. Until this was wired the community publisher was
constructed with no ingest endpoint at all, so every bundle it published
carried constraints and a skills list but no contribution instruction, no
bundled `oms_contribute.py` and no `.mcp.json`. An agent installing that
bundle had no way to send anything back, and nothing in the output said so.
"""
from dataclasses import replace
from pathlib import Path
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from oms.community.app import build_memory
from oms.domain.models import Edge, Rule, Skill
from oms.domain.types import EdgeType
from oms.settings.core import DEFAULT_PUBLIC_URL, CoreSettings, advertises_loopback
from oms.web.api import create_app
from oms.web.endpoints import INGEST_ROUTE, MCP_ROUTE


@pytest.fixture
def world(tmp_path):
    services = build_memory(tmp_path / "workspace", "local")
    services.store.upsert_skill(Skill(id="checks", name="Checks", description="Review the publication.",
                                      domain="engineering", tenant_id="local"))
    services.store.upsert_rule(Rule(id="check-rule", body="Check the result before publishing.", tenant_id="local"))
    services.store.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id="check-rule", to_id="checks"))
    return services


def client_for(services):
    return TestClient(create_app(services), base_url="http://127.0.0.1:4317")


def publish_to(services) -> Path:
    with client_for(services) as client:
        response = client.post("/api/publish")
        assert response.status_code == 200, response.text
        return Path(response.json()["output"])


def test_unset_public_url_is_the_localhost_default(monkeypatch):
    monkeypatch.delenv("OMS_PUBLIC_URL", raising=False)
    monkeypatch.setenv("OMS_ACKNOWLEDGE_NETWORK_EXPOSURE", "1")
    settings = CoreSettings.from_env()
    assert settings.public_url == DEFAULT_PUBLIC_URL
    assert settings.contribution_endpoint == f"{DEFAULT_PUBLIC_URL}{INGEST_ROUTE}"
    assert settings.mcp_endpoint == f"{DEFAULT_PUBLIC_URL}{MCP_ROUTE}"


def test_a_configured_public_url_is_advertised_verbatim(monkeypatch):
    monkeypatch.setenv("OMS_PUBLIC_URL", "https://oms.example.org/")
    settings = CoreSettings.from_env()
    assert settings.public_url == "https://oms.example.org"
    assert settings.contribution_endpoint == "https://oms.example.org/api/ingest"
    assert settings.mcp_endpoint == "https://oms.example.org/mcp"


def test_an_empty_public_url_closes_the_door(monkeypatch):
    monkeypatch.setenv("OMS_PUBLIC_URL", "  ")
    settings = CoreSettings.from_env()
    assert settings.public_url is None
    assert settings.contribution_endpoint is None
    assert settings.mcp_endpoint is None


@pytest.mark.parametrize("raw", ["localhost:4317", "ftp://oms.example.org", "/api/ingest",
                                 "http://", "not a url"])
def test_a_public_url_that_is_not_an_absolute_http_address_is_refused(raw):
    with pytest.raises(ValueError, match="OMS_PUBLIC_URL"):
        CoreSettings(public_url=raw).validate()


def test_the_default_port_follows_the_configured_port(monkeypatch):
    monkeypatch.delenv("OMS_PUBLIC_URL", raising=False)
    monkeypatch.setenv("OMS_PORT", "9100")
    monkeypatch.setenv("OMS_ALLOWED_ORIGINS", "http://localhost:9100")
    monkeypatch.setenv("OMS_ACKNOWLEDGE_NETWORK_EXPOSURE", "1")
    settings = CoreSettings.from_env()
    assert settings.public_url == "http://localhost:9100"
    assert settings.contribution_endpoint == "http://localhost:9100/api/ingest"


def test_published_root_instructions_carry_the_contribution_block(world):
    output = publish_to(world)
    for name in ("CLAUDE.md", "AGENTS.md"):
        body = (output / name).read_text()
        assert "# Contributing learnings" in body, f"{name} has no contribution instruction"
        assert f"{DEFAULT_PUBLIC_URL}{INGEST_ROUTE}" in body
        assert "log_correction" in body


def test_published_bundle_carries_the_contribution_tool_and_mcp_configuration(world):
    output = publish_to(world)
    assert (output / "oms_contribute.py").is_file()
    assert f"{DEFAULT_PUBLIC_URL}{INGEST_ROUTE}" in (output / "oms_contribute.py").read_text()
    assert f"{DEFAULT_PUBLIC_URL}{MCP_ROUTE}" in (output / ".mcp.json").read_text()


def test_contribution_off_publishes_no_instruction_and_no_tool(world):
    closed = replace(world, settings=replace(world.settings, public_url=None))
    output = publish_to(closed)
    assert "# Contributing learnings" not in (output / "CLAUDE.md").read_text()
    assert not (output / "oms_contribute.py").exists()
    assert not (output / ".mcp.json").exists()
    assert (output / "skills/checks/SKILL.md").is_file(), "the bundle itself must still publish"


# The three ways a deployment arrives at loopback. The launcher's own default
# is the one that matters most: Compose always sets the variable, so a guard
# reading "was this configured?" rather than "what does it say?" never fires
# under the only supported way to start the product.
LOOPBACK = [None, "http://localhost:4317", "http://127.0.0.1:4317", "http://localhost"]


@pytest.mark.parametrize("configured", LOOPBACK)
def test_git_publication_refuses_any_loopback_address(world, monkeypatch, configured):
    from oms.community import publication
    pushed = []
    monkeypatch.setattr(publication, "publish_to_repo",
                        lambda *args: pushed.append(args) or (None, True))
    services = (world if configured is None else
                replace(world, settings=replace(world.settings, public_url=configured)))
    with client_for(services) as client:
        saved = client.patch("/api/settings", json={"publication_target": "git",
                                                   "publication_git_url": "https://example.org/skills.git"})
        assert saved.status_code == 200, saved.text
        refused = client.post("/api/publish")
    assert refused.status_code == 422, refused.text
    assert "OMS_PUBLIC_URL" in refused.text
    assert pushed == [], "a bundle naming loopback reached the remote"


@pytest.mark.parametrize("configured", LOOPBACK[1:])
def test_local_publication_is_unaffected_by_a_loopback_address(world, configured):
    """The agents reading a local bundle are on the machine that served it."""
    services = replace(world, settings=replace(world.settings, public_url=configured))
    output = publish_to(services)
    assert f"{configured}/api/ingest" in (output / "CLAUDE.md").read_text()


# The refusal above fires at the last step of the setup wizard, after the
# person has already created a token and logged the publisher in. The settings
# response carries the advertised address so the wizard can warn at the first
# step instead, the moment Git is chosen.
@pytest.mark.parametrize("configured, loopback", [
    ("http://localhost:4317", True), ("http://127.0.0.1:4317", True),
    ("https://oms.example.org", False), (None, False)])
def test_settings_report_the_advertised_address_and_whether_it_is_loopback(world, configured, loopback):
    services = replace(world, settings=replace(world.settings, public_url=configured))
    with client_for(services) as client:
        settings = client.get("/api/settings").json()
    assert settings["public_url"] == configured
    assert settings["public_url_is_loopback"] is loopback


def test_the_command_line_git_publish_refuses_loopback_too(world, monkeypatch, tmp_path, capsys):
    """The console is not the only way to push; the CLI reaches the same remote."""
    from oms import cli
    from oms.publish import git
    monkeypatch.delenv("OMS_PUBLIC_URL", raising=False)
    monkeypatch.setenv("OMS_DATA_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr("oms.adapters.neo4j.driver.build_driver",
                        lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr("oms.community.app.build_community", lambda settings, driver: world)
    pushed = []
    monkeypatch.setattr(git, "publish_to_repo", lambda *a, **k: pushed.append(a) or (None, True))
    code = cli.main(["publish", "--git-url", "https://example.org/skills.git"])
    assert code == 1
    assert "OMS_PUBLIC_URL" in capsys.readouterr().err
    assert pushed == [], "the command line pushed a bundle naming loopback"


def test_moving_the_served_port_moves_the_advertised_address():
    """`oms serve --port` must not leave the published address on the old port."""
    moved = CoreSettings().with_port(9100)
    assert moved.port == 9100
    assert moved.public_url == "http://localhost:9100"
    configured = CoreSettings(public_url="https://oms.example.org").with_port(9100)
    assert configured.public_url == "https://oms.example.org", "a real address must not be rewritten"


@pytest.mark.parametrize("raw", ["https://host/?token=secret", "https://host/#fragment",
                                 "https://user:secret@host"])
def test_a_public_url_that_would_publish_a_broken_or_secret_address_is_refused(raw):
    """This value is copied verbatim into files that reach a shared repository."""
    with pytest.raises(ValueError, match="OMS_PUBLIC_URL"):
        CoreSettings(public_url=raw).validate()


def test_git_publication_proceeds_once_a_real_address_is_configured(world, tmp_path, monkeypatch):
    from oms.community import publication
    from oms.community.publication import publish_to_repo as actual_publish
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)],
                   check=True, capture_output=True)
    monkeypatch.setattr(publication, "publish_to_repo",
                        lambda publisher, tenant, url, work_dir, branch:
                        actual_publish(publisher, tenant, str(remote), work_dir, branch))
    deployed = replace(world, settings=replace(world.settings,
                                               public_url="https://oms.example.org"))
    with client_for(deployed) as client:
        saved = client.patch("/api/settings", json={"publication_target": "git",
                                                   "publication_git_url": "https://example.org/skills.git"})
        assert saved.status_code == 200, saved.text
        response = client.post("/api/publish")
    assert response.status_code == 200, response.text
    listed = subprocess.run(["git", "--git-dir", str(remote), "show", "main:CLAUDE.md"],
                            check=True, capture_output=True, text=True).stdout
    assert "https://oms.example.org/api/ingest" in listed


def test_the_community_routes_match_the_derivations_agents_are_handed(world):
    """The advertised URL must be the route the server actually mounts."""
    paths = {route.path for route in create_app(world).routes}
    assert INGEST_ROUTE in paths
    assert MCP_ROUTE in paths
    advertised = CoreSettings(public_url="http://127.0.0.1:4317")
    assert advertised.contribution_endpoint.endswith(INGEST_ROUTE)
    assert advertised.mcp_endpoint.endswith(MCP_ROUTE)
    for endpoint in (advertised.contribution_endpoint, advertised.mcp_endpoint):
        assert endpoint.removeprefix("http://127.0.0.1:4317") in paths


def test_the_loopback_test_reads_the_address_not_the_configuration():
    assert advertises_loopback("http://localhost:4317") is True
    assert advertises_loopback("http://127.0.0.1:4317") is True
    assert advertises_loopback("http://[::1]:4317") is True
    assert advertises_loopback("https://oms.example.org") is False
    assert advertises_loopback(None) is False, "contribution off advertises nothing to lose"
