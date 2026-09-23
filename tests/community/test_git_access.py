"""Independent Git onboarding acceptance checks, with no network or user HOME.

Git's real credential/config machinery runs inside a disposable home directory.
Only ls-remote is replaced: it resolves credentials locally and simulates a
provider response, so these tests cannot contact a repository or use real tokens.
"""
from dataclasses import replace
import getpass
import os
from pathlib import Path
import shutil
import stat
import subprocess
from types import SimpleNamespace
import warnings

import pytest

from oms import cli
from oms.adapters.memory.settings import InMemorySettingsStore
from oms.community import git_access, publication
from oms.settings.core import CoreSettings


REPOSITORY = "https://git.example.invalid/team/skills.git"
OTHER_REPOSITORY = "https://git.example.invalid/other/skills.git"
TOKEN = "critic-fake-token-new"
OLD_TOKEN = "critic-fake-token-old"


@pytest.fixture
def git_home(tmp_path, monkeypatch):
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required to exercise real credential/config behavior")
    # Remove all inherited Git overrides before launching any Git command.
    # In particular, --global and credential helpers must never read user files.
    for key in tuple(os.environ):
        if key.startswith(("GIT_", "GCM_")) or key in ("SSH_ASKPASS", "XDG_CONFIG_HOME"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home with spaces"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("GIT_ASKPASS", "false")
    monkeypatch.setenv("SSH_ASKPASS", "false")
    real_run = subprocess.run

    state = SimpleNamespace(home=home, calls=[], reads=[], expected=("operator", TOKEN),
                            public=False, failure=None, fail_config_once=False)

    def run_actual(args, *, payload=None, cwd=None, env=None):
        return real_run([git, *args], cwd=cwd or home, input=payload,
                        capture_output=True, text=True, timeout=10,
                        env=env or dict(os.environ))

    def credential(remote, options=(), cwd=None, env=None):
        result = run_actual([*options, "credential", "fill"],
                            payload=f"url={remote}\n\n", cwd=cwd, env=env)
        return dict(line.split("=", 1) for line in result.stdout.splitlines()
                    if "=" in line) if result.returncode == 0 else {}

    def isolated_run(command, **kwargs):
        args = list(command)
        assert Path(args[0]).name == "git", "Only Git may be executed by these tests"
        state.calls.append((args, kwargs))
        if "ls-remote" in args:
            index = args.index("ls-remote")
            remote = args[-1]
            assert remote.endswith(".git"), "Only fixture Git repository URLs are permitted"
            state.reads.append(remote)
            if state.failure == "timeout":
                raise subprocess.TimeoutExpired(args, 45, stderr=TOKEN)
            if state.failure == "missing-git":
                raise FileNotFoundError(TOKEN)
            accepted = state.public
            if not accepted and state.failure is None:
                resolved = credential(remote, args[1:index], kwargs["cwd"], kwargs["env"])
                accepted = (resolved.get("username"), resolved.get("password")) == state.expected
            return subprocess.CompletedProcess(args, 0 if accepted else 128, "",
                "" if accepted else f"provider returned a sensitive diagnostic: {TOKEN}")
        assert "credential-store" in args or "config" in args, "Unexpected Git operation"
        if (state.fail_config_once and "config" in args
                and any(arg.endswith(".helper") for arg in args)
                and any(arg.startswith("store --file ") for arg in args)):
            state.fail_config_once = False
            return subprocess.CompletedProcess(args, 1, "", f"Cannot lock config: {TOKEN}")
        # No command delegated here can access a network remote.
        return real_run([git, *args[1:]], **kwargs)

    monkeypatch.setattr(git_access.subprocess, "run", isolated_run)
    state.run = run_actual
    state.credential = credential
    return state


def _interactive(monkeypatch, *, username="operator", token=TOKEN):
    monkeypatch.setattr(git_access.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: username)
    monkeypatch.setattr(git_access.getpass, "getpass", lambda _prompt: token)


def _home_files(home):
    return {str(path.relative_to(home)): path.read_bytes()
            for path in home.rglob("*") if path.is_file()}


def test_saved_token_is_private_repo_scoped_and_not_in_arguments(git_home):
    ambient = git_home.home / "existing credentials"
    result = git_home.run(["credential-store", "--file", str(ambient), "store"],
        payload=f"url={OTHER_REPOSITORY}\nusername=other-user\npassword={OLD_TOKEN}\n\n")
    assert result.returncode == 0
    helper = "store --file " + "'" + str(ambient) + "'"
    assert git_home.run(["config", "--global", "credential.helper", helper]).returncode == 0

    git_access.save_login(REPOSITORY, "operator", TOKEN)

    assert git_home.credential(REPOSITORY)["password"] == TOKEN
    assert git_home.credential(OTHER_REPOSITORY)["password"] == OLD_TOKEN
    assert git_home.credential("https://git.example.invalid/team/unconfigured.git").get("password") != TOKEN
    assert git_home.credential("https://other.example.invalid/team/skills.git").get("password") != TOKEN
    files = [path for path in git_home.home.rglob("*")
             if path.is_file() and TOKEN.encode() in path.read_bytes()]
    assert len(files) == 1
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(files[0].parent.stat().st_mode) == 0o700
    assert TOKEN not in (git_home.home / ".gitconfig").read_text()
    assert all(TOKEN not in arg for args, _ in git_home.calls for arg in args)
    trial_paths = [Path(args[args.index("--file") + 1]) for args, _ in git_home.calls
                   if "credential-store" in args and "--file" in args
                   and not Path(args[args.index("--file") + 1]).is_relative_to(git_home.home)]
    assert trial_paths and not any(path.exists() for path in trial_paths)


def test_rotation_replaces_saved_token_without_changing_other_helpers(git_home):
    git_home.expected = ("operator", OLD_TOKEN)
    git_access.save_login(REPOSITORY, "operator", OLD_TOKEN)
    git_home.expected = ("operator", TOKEN)
    git_access.save_login(REPOSITORY, "operator", TOKEN)
    assert git_home.credential(REPOSITORY)["password"] == TOKEN
    assert not any(OLD_TOKEN.encode() in content for content in _home_files(git_home.home).values())


def test_failed_read_preserves_previous_working_credentials(git_home):
    git_home.expected = ("operator", OLD_TOKEN)
    git_access.save_login(REPOSITORY, "operator", OLD_TOKEN)
    before = _home_files(git_home.home)
    git_home.failure = "provider-refused"
    with pytest.raises(git_access.GitAccessError, match="not been replaced"):
        git_access.save_login(REPOSITORY, "operator", TOKEN)
    assert _home_files(git_home.home) == before
    assert git_home.credential(REPOSITORY)["password"] == OLD_TOKEN


def test_config_failure_rolls_back_credential_rotation(git_home):
    git_home.expected = ("operator", OLD_TOKEN)
    git_access.save_login(REPOSITORY, "operator", OLD_TOKEN)
    before = _home_files(git_home.home)
    git_home.expected = ("operator", TOKEN)
    git_home.fail_config_once = True
    with pytest.raises(git_access.GitAccessError):
        git_access.save_login(REPOSITORY, "operator", TOKEN)
    assert not git_home.fail_config_once, "The config-write fault must have been exercised"
    assert _home_files(git_home.home) == before
    assert git_home.credential(REPOSITORY)["password"] == OLD_TOKEN


def test_entered_username_takes_precedence_over_old_repository_identity(git_home):
    assert git_home.run(["config", "--global", f"credential.{REPOSITORY}.username",
                         "previous-operator"]).returncode == 0
    git_access.save_login(REPOSITORY, "operator", TOKEN)
    saved = git_home.credential(REPOSITORY)
    assert saved["username"] == "operator"
    assert saved["password"] == TOKEN


def test_another_git_config_writer_prevents_partial_rotation(git_home, monkeypatch, capsys):
    git_home.expected = ("operator", OLD_TOKEN)
    git_access.save_login(REPOSITORY, "operator", OLD_TOKEN)
    lock = git_home.home / ".gitconfig.lock"
    lock.write_text("An unrelated Git process owns this lock.\n")
    before = _home_files(git_home.home)
    _interactive(monkeypatch)
    git_home.expected = ("operator", TOKEN)
    assert git_access.login(REPOSITORY) == 1
    assert _home_files(git_home.home) == before
    assert git_home.credential(REPOSITORY)["password"] == OLD_TOKEN
    assert TOKEN not in capsys.readouterr().err


def test_final_config_commit_failure_preserves_old_login(git_home, monkeypatch, capsys):
    git_home.expected = ("operator", OLD_TOKEN)
    git_access.save_login(REPOSITORY, "operator", OLD_TOKEN)
    before = _home_files(git_home.home)
    _interactive(monkeypatch)
    git_home.expected = ("operator", TOKEN)
    actual_replace = os.replace
    attempted = []
    def failed_commit(source, destination, *args, **kwargs):
        if Path(destination) == git_home.home / ".gitconfig":
            attempted.append(True)
            raise PermissionError(f"Simulated disk failure: {TOKEN}")
        return actual_replace(source, destination, *args, **kwargs)
    monkeypatch.setattr(git_access.os, "replace", failed_commit)
    assert git_access.login(REPOSITORY) == 1
    assert attempted, "The final configuration commit fault must be exercised"
    assert _home_files(git_home.home) == before
    assert git_home.credential(REPOSITORY)["password"] == OLD_TOKEN
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


@pytest.mark.parametrize("failure", ["provider-refused", "timeout", "missing-git"])
def test_failed_login_redacts_provider_and_process_errors(git_home, monkeypatch, capsys, failure):
    _interactive(monkeypatch)
    git_home.failure = failure
    assert git_access.login(REPOSITORY) == 1
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert "Git credentials saved" not in output.out
    assert _home_files(git_home.home) == {}


def test_non_interactive_login_never_requests_or_stores_a_secret(git_home, monkeypatch, capsys):
    monkeypatch.setattr(git_access.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("Must not prompt without a TTY"))
    assert git_access.login(REPOSITORY) == 1
    assert "interactive terminal" in capsys.readouterr().err
    assert not git_home.calls
    assert _home_files(git_home.home) == {}


def test_getpass_echo_fallback_is_refused(git_home, monkeypatch, capsys):
    _interactive(monkeypatch)
    def no_secure_terminal(_prompt):
        warnings.warn("echo would be enabled", getpass.GetPassWarning)
        pytest.fail("GetPassWarning must abort before fallback reads the token")
    monkeypatch.setattr(git_access.getpass, "getpass", no_secure_terminal)
    assert git_access.login(REPOSITORY) == 1
    assert "no token was saved" in capsys.readouterr().err
    assert not git_home.calls


@pytest.mark.parametrize("remote", [
    "git@git.example.invalid:team/skills.git",
    "https://operator:secret@git.example.invalid/team/skills.git",
    "https://git.example.invalid/team/skills.git?access_token=secret",
    "https://git.example.invalid/team/%0askills.git",
    "https://git.example.invalid/team/%7fskills.git",
    "https://git.example.invalid/team/$(touch-owned).git",
])
def test_unsafe_or_ssh_login_is_rejected_before_prompt(git_home, monkeypatch, capsys, remote):
    _interactive(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("Invalid URL must not prompt"))
    assert git_access.login(remote) == 1
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err
    assert not git_home.calls


@pytest.mark.parametrize("username, token", [
    ("operator\npassword=evil", TOKEN), ("operator", "token\rhost=other.invalid"),
    ("operator", "bad\x00token"), ("operator", "bad\x7ftoken"), ("", TOKEN), ("operator", ""),
])
def test_credential_protocol_injection_is_rejected(git_home, username, token):
    with pytest.raises(git_access.GitAccessError):
        git_access.save_login(REPOSITORY, username, token)
    assert not git_home.calls
    assert _home_files(git_home.home) == {}


def test_public_empty_repository_reports_read_success_without_claiming_write(git_home, monkeypatch, capsys):
    _interactive(monkeypatch)
    git_home.public = True
    assert git_access.login(REPOSITORY) == 0
    output = capsys.readouterr()
    assert "Git credentials saved for this repository" in output.out
    assert "empty repository is OK" in output.out
    assert "does not verify write permission" in output.out
    assert TOKEN not in output.out + output.err


def test_read_check_uses_existing_access_without_prompting_or_writing(git_home, monkeypatch):
    git_access.save_login(REPOSITORY, "operator", TOKEN)
    before = _home_files(git_home.home)
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("A read check must not prompt"))
    assert git_access.login(REPOSITORY, check=True) == 0
    assert _home_files(git_home.home) == before


def test_git_login_command_does_not_require_workspace_or_database(git_home, monkeypatch):
    git_home.public = True
    def forbidden(*_args, **_kwargs):
        pytest.fail("Credential setup must run before workspace/database configuration")
    monkeypatch.setattr(cli.CoreSettings, "from_env", forbidden)
    monkeypatch.setenv("OMS_PORT", "not-an-integer")
    assert cli.main(["git-login", "--repository", REPOSITORY, "--check"]) == 0
    assert git_home.reads == [REPOSITORY]
    assert _home_files(git_home.home) == {}


@pytest.mark.parametrize("in_container, hostname, expected", [
    (True, "a12b34c56d78", "a12b34c56d78"),
    (True, "custom-community-api", None),
    (True, "invalid;other-container", None),
    (False, "a12b34c56d78", None),
])
def test_container_identity_only_exposes_current_docker_style_hostname(tmp_path, monkeypatch, in_container, hostname, expected):
    monkeypatch.setattr(publication.socket, "gethostname", lambda: hostname)
    settings = replace(CoreSettings(data_dir=tmp_path), publish_in_container=in_container)
    response = publication.destination_settings(settings, InMemorySettingsStore(), "acme")
    assert response["publish_container_id"] == expected
