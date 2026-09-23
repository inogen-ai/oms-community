"""Interactive HTTPS Git setup for the account that runs Community.

Secrets travel through stdin and Git's credential store, never CLI arguments,
browser settings or the publication checkout. Only repository reads are tested.
"""
from __future__ import annotations

import getpass
import fcntl
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
import warnings

from oms.community.publication import _remote


class GitAccessError(ValueError):
    pass


def https_repository(value: str) -> str:
    remote = _remote(value)
    if urlsplit(remote).scheme != "https":
        raise GitAccessError("Use an HTTPS repository URL for token login. SSH repositories use your existing SSH configuration.")
    if any(ord(char) < 32 or ord(char) == 127 for char in unquote(remote)):
        raise GitAccessError("The repository URL contains an invalid character.")
    return remote


def _git(*args: str, cwd: Path, payload: str | None = None) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, input=payload,
            capture_output=True, text=True, timeout=45,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "false",
                 "GCM_INTERACTIVE": "Never", "SSH_ASKPASS": "false"})
    except (OSError, subprocess.TimeoutExpired):
        raise GitAccessError("Git could not finish. Check that Git is installed and the server can reach your Git provider, then retry.") from None
    if result.returncode:
        # Providers and helpers can echo credentials in diagnostics.
        raise GitAccessError("Git access failed. Check the repository URL, token expiry, selected repository and organisation approval, then retry.")
    return result.stdout


def _helper(path: Path) -> str:
    return "store --file " + shlex.quote(str(path))


def _record(remote: str, username: str, token: str) -> str:
    if not username.strip() or not token or any(ord(char) < 32 or ord(char) == 127 for char in username + token):
        raise GitAccessError("Enter a username and a non-empty, single-line token.")
    return f"url={remote}\nusername={username.strip()}\npassword={token}\n\n"


def check_access(remote: str) -> None:
    remote = _remote(remote)
    if not remote:
        raise GitAccessError("Enter a repository URL.")
    with tempfile.TemporaryDirectory(prefix="oms-git-check-") as temporary:
        _git("-c", "http.followRedirects=false", "ls-remote", "--", remote, cwd=Path(temporary))


def save_login(remote: str, username: str, token: str) -> None:
    remote = https_repository(remote)
    record = _record(remote, username, token)
    with tempfile.TemporaryDirectory(prefix="oms-git-login-") as temporary:
        root = Path(temporary)
        trial = root / "credentials"
        try:
            _git("credential-store", "--file", str(trial), "store", cwd=root, payload=record)
            _git("-c", f"credential.{remote}.helper=", "-c", f"credential.{remote}.helper={_helper(trial)}",
                 "-c", f"credential.{remote}.username={username.strip()}",
                 "-c", f"credential.{remote}.useHttpPath=true", "-c", "http.followRedirects=false",
                 "ls-remote", "--", remote, cwd=root)
        except GitAccessError as error:
            raise GitAccessError(f"{error} Existing saved credentials have not been replaced.") from None

        directory = Path.home() / ".oms-git"
        if directory.is_symlink():
            raise GitAccessError("The Git credential directory must not be a symbolic link.")
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        # Serialize our setup/cleanup and respect Git's own config lock. The
        # only commit point is the config rename: until then, Git still sees
        # the complete old helper and token, even if preparation fails.
        with os.fdopen(os.open(directory / "setup.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), "wb") as setup_lock:
            try:
                fcntl.flock(setup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise GitAccessError("Another Git login is being saved. Wait for it to finish, then retry.") from None
            _commit_login(remote, username.strip(), trial, root, directory)


def _commit_login(remote: str, username: str, trial: Path, root: Path, directory: Path) -> None:
    config = Path(os.environ.get("GIT_CONFIG_GLOBAL", str(Path.home() / ".gitconfig")))
    if config.is_symlink():
        raise GitAccessError("Your Git config is a symbolic link. Use your existing credential manager to configure this repository instead.")
    lock = config.with_name(config.name + ".lock")
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise GitAccessError("Another Git command has locked the configuration. Wait for it to finish, then retry. Existing credentials are unchanged.") from None
    committed = False
    credentials = None
    identity = hashlib.sha256(remote.encode()).hexdigest()
    try:
        with os.fdopen(descriptor, "wb") as output:
            candidate = root / "config"
            candidate.write_bytes(config.read_bytes() if config.exists() else b"")
            candidate.chmod(0o600)
            token_fd, filename = tempfile.mkstemp(prefix=identity + ".", suffix=".credentials", dir=directory)
            credentials = Path(filename)
            with os.fdopen(token_fd, "wb") as secret:
                secret.write(trial.read_bytes())
                secret.flush()
                os.fsync(secret.fileno())
            # Reset inherited helpers only for this URL; also replace a stale
            # configured username that would otherwise mask the entered one.
            key = f"credential.{remote}"
            _git("config", "--file", str(candidate), "--replace-all", f"{key}.helper", "", cwd=root)
            _git("config", "--file", str(candidate), "--add", f"{key}.helper", _helper(credentials), cwd=root)
            _git("config", "--file", str(candidate), "--replace-all", f"{key}.useHttpPath", "true", cwd=root)
            _git("config", "--file", str(candidate), "--replace-all", f"{key}.username", username, cwd=root)
            output.write(candidate.read_bytes())
            output.flush()
            os.fsync(output.fileno())
            os.replace(lock, config)
            committed = True
        for previous in directory.glob(identity + ".*.credentials"):
            if previous != credentials:
                try:
                    previous.unlink()
                except OSError:
                    pass  # The new login is active; an old file cannot undo it.
    finally:
        if not committed:
            lock.unlink(missing_ok=True)
            if credentials is not None:
                credentials.unlink(missing_ok=True)


def login(remote: str, *, check: bool = False) -> int:
    try:
        if check:
            check_access(remote)
        else:
            remote = https_repository(remote)
            if not sys.stdin.isatty():
                raise GitAccessError("Run this command in an interactive terminal so the token can be entered without echoing it.")
            print(f"Connect the Community publisher to {remote}")
            print("The token is saved unencrypted with owner-only permissions in this server account's home directory.")
            username = input("Git username: ")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("Repository access token (hidden): ")
            save_login(remote, username, token)
            print("Git credentials saved for this repository.")
        print("Repository read check passed. An empty repository is OK. This does not verify write permission.")
        print("Return to Settings, continue to Publish, then choose Publish and push to verify writing and create the bundle.")
        return 0
    except (GitAccessError, ValueError) as error:
        print(str(error), file=sys.stderr)
    except (EOFError, getpass.GetPassWarning):
        print("Login cancelled; no token was saved.", file=sys.stderr)
    except KeyboardInterrupt:
        print("Login cancelled. Rerun the command to check or update saved access.", file=sys.stderr)
    except OSError:
        print("Could not save Git access. Check the server account's home-directory permissions and retry.", file=sys.stderr)
    return 1
