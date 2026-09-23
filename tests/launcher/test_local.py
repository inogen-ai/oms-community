"""Exercise the real launcher and Compose dotenv parser without starting services."""
import json
import os
from pathlib import Path
import shutil
import shlex
import stat
import subprocess
import sys

import pytest


@pytest.fixture
def launcher(tmp_path):
    docker = shutil.which("docker")
    if not docker or subprocess.run([docker, "compose", "version"], capture_output=True).returncode:
        pytest.skip("Docker Compose CLI is needed; no daemon or containers are used")
    root = Path(__file__).resolve().parents[2]
    source = root / "community" if (root / "community/run-local.sh").exists() else root
    app = tmp_path / "OMS Community"
    app.mkdir()
    for name in ("run-local.sh", "compose.yaml"):
        shutil.copy2(source / name, app / name)
    home = tmp_path / "host-home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Compose parses its own dotenv file. Only `up` is intercepted, preserving
    # the actual shell/path/configuration behaviour without touching a daemon.
    wrapper = bin_dir / "docker"
    helper = bin_dir / "docker_boundary.py"
    helper.write_text('''
import json, os, subprocess, sys
from pathlib import Path
if "up" not in sys.argv:
    raise SystemExit(subprocess.call([os.environ["TEST_REAL_DOCKER"], *sys.argv[1:]]))
record = {key: os.environ.get(key) for key in (
    "OMS_PUBLISH_HOST_ROOT", "OMS_PUBLISH_HOST_HOME", "OMS_LOCAL_LAUNCHER_DIR", "COMPOSE_PROJECT_NAME")}
record["args"] = sys.argv[1:]
with Path(os.environ["TEST_CALLS"]).open("a") as out:
    out.write(json.dumps(record) + "\\n")
raise SystemExit(int(os.environ.get("TEST_UP_EXIT", "0")))
''')
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(helper))} "$@"\n')
    wrapper.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("OMS_", "COMPOSE_", "DOCKER_"))}
    env.update(HOME=str(home), PATH=str(bin_dir) + os.pathsep + env["PATH"],
               DOCKER_CONFIG=os.environ.get("DOCKER_CONFIG", str(Path.home() / ".docker")),
               TEST_REAL_DOCKER=docker, TEST_CALLS=str(tmp_path / "calls.jsonl"))
    def run(*args, **extra):
        return subprocess.run(["sh", str(app / "run-local.sh"), *args], env={**env, **extra},
                              text=True, capture_output=True)
    def calls():
        path = Path(env["TEST_CALLS"])
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return app, home, run, calls


def test_chosen_home_folder_and_project_survive_a_fresh_terminal(launcher):
    app, home, run, calls = launcher
    (app / ".env.local").write_text("OMS_NEO4J_PASSWORD=keep-this-password\nEXTRA_SETTING=preserved\n")
    first = run("--publish-dir", "~/.oms_skills", COMPOSE_PROJECT_NAME="vm-qa")
    assert first.returncode == 0, first.stderr
    output = home / ".oms_skills"
    (output / "sentinel.txt").write_text("Existing publication")
    second = run()
    assert second.returncode == 0, second.stderr
    assert [row["OMS_PUBLISH_HOST_ROOT"] for row in calls()] == [str(output)] * 2
    assert [row["COMPOSE_PROJECT_NAME"] for row in calls()] == ["vm-qa"] * 2
    assert calls()[0]["OMS_PUBLISH_HOST_HOME"] == str(home)
    assert calls()[0]["OMS_LOCAL_LAUNCHER_DIR"] == str(app)
    assert "keep-this-password" in (app / ".env.local").read_text()
    assert "EXTRA_SETTING=preserved" in (app / ".env.local").read_text()
    assert stat.S_IMODE((app / ".env.local").stat().st_mode) == 0o600
    assert (output / "sentinel.txt").read_text() == "Existing publication"
    assert "keep-this-password" not in first.stdout + first.stderr


def test_literal_folder_names_roundtrip_without_shell_execution(launcher):
    app, home, run, calls = launcher
    output = home / 'skills $notes\' " back\\ $(touch SHOULD_NOT_EXIST)'
    result = run("--publish-dir", str(output))
    assert result.returncode == 0, result.stderr
    result = run()
    assert result.returncode == 0, result.stderr
    assert calls()[-1]["OMS_PUBLISH_HOST_ROOT"] == str(output)
    assert not (app / "SHOULD_NOT_EXIST").exists()
    assert not (home / "SHOULD_NOT_EXIST").exists()


def test_environment_override_is_remembered_and_old_publication_is_left_in_place(launcher):
    app, home, run, calls = launcher
    assert run().returncode == 0
    old = app / "published"
    (old / "existing.txt").write_text("keep")
    new = home / "another folder"
    assert run(OMS_PUBLISH_HOST_ROOT=str(new)).returncode == 0
    assert run().returncode == 0
    assert calls()[-1]["OMS_PUBLISH_HOST_ROOT"] == str(new)
    assert (old / "existing.txt").read_text() == "keep"
    assert not (new / "existing.txt").exists()


@pytest.mark.parametrize("folder", ["/", "~", "~/", ".", "bad\nfolder"])
def test_refuses_broad_roots_and_multiline_paths_before_starting_services(launcher, folder):
    _, _, run, calls = launcher
    assert run("--publish-dir", folder).returncode != 0
    assert calls() == []


def test_linked_configuration_is_never_read_or_replaced(launcher):
    app, home, run, calls = launcher
    target = home / "private-settings"
    target.write_text("private")
    (app / ".env.local").symlink_to(target)
    assert run("--publish-dir", "~/.oms_skills").returncode != 0
    assert target.read_text() == "private"
    assert calls() == []


def test_failed_start_is_not_reported_as_ready_and_keeps_configuration_for_retry(launcher):
    app, _, run, _ = launcher
    result = run("--publish-dir", "~/.oms_skills", TEST_UP_EXIT="1")
    assert result.returncode != 0
    assert "is ready" not in result.stdout
    assert ".oms_skills" in (app / ".env.local").read_text()
    assert run().returncode == 0
