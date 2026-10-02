"""Carry a rendered bundle into the skills distribution repository.

`python -m oms.publish` renders a tree and stops. These helpers carry that tree
into the repository the organisation clones: sync a checkout, render into it,
commit and push. The publisher, its gate and its prune are untouched.

Two invariants shape it. The gate must block the PUSH, not merely the render,
so publish runs inside the checkout and a failed gate returns before any commit.
And history stays linear and append-only, because consumers pull --ff-only and
a rewrite breaks every machine in the organisation at once.

Credentials are never handled here: `repo` takes whatever URL the ambient git
auth can reach (an SSH remote locally, a deploy key on a cron host, a token in
the userinfo on Railway). A caller that puts that URL anywhere a person can read
must strip it first, with `oms.web.config.public_repo_url`.

This lives in the package rather than beside its CLI because two callers push
now: the scheduler (`scripts/run_publish.py`) and the console's publish button
(`src/oms/web/publish_api.py`). One implementation is what stops the timer and
the button drifting apart.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from oms.publish.gate import GateResult
from oms.publish.publisher import Publisher, SkillSelector

# The ceiling on every git subprocess. `subprocess.run` waits for ever by
# default, and the console's publish button runs these inside a request, so an
# unreachable host or a stalled TLS handshake would leave a handler that never
# returns rather than a refusal somebody can read. Generous rather than tight:
# the first run on a fresh container clones the whole repository.
GIT_TIMEOUT_SECONDS = 300


class GitCommandError(RuntimeError):
    """A git subprocess exited non-zero, or ran past GIT_TIMEOUT_SECONDS.

    The message carries git's own stderr rather than leaving a bare
    CalledProcessError (argv and an exit code, nothing else) to speak for it.
    The one reading it is routinely a cron host with no terminal attached, so
    whatever lands in this text is the entire incident report an operator
    ever sees for a bad deploy key, a renamed repository, or no network.
    """


def _git(*args: str, cwd: Path, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    try:
        result = subprocess.run(("git", *args), cwd=cwd, capture_output=True,
                                text=True, timeout=timeout,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        # The subcommand alone, never the whole argv: `git clone <repo>` would
        # put the remote, and on Railway the write token inside it, into a
        # message written to be read by a person. Naming the operation is what
        # the reader needs anyway - which git call hung, not with what flags.
        raise GitCommandError(
            f"git {args[0]} timed out after {timeout:g}s") from None
    if result.returncode != 0:
        raise GitCommandError(
            f"git {' '.join(args)} failed (exit {result.returncode}): "
            f"{result.stderr.strip()}")
    return result.stdout.strip()


class CheckoutBelongsElsewhere(GitCommandError):
    """The checkout is a clone of a different repository from the one this
    publish is for.

    Refused rather than reused: `sync_checkout` fetches, resets to and pushes
    `origin`, so reusing it would push one destination's tree to another's
    remote, and a reset onto an empty new remote would carry the old
    repository's files and ownership record across with it. Refused rather
    than deleted, because the checkout directory is the caller's to name and
    may be something other than a checkout this code made. The message names
    both repositories without their credentials."""


def _without_userinfo(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme or "@" not in parts.netloc:
        return url
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[1], parts.path,
                       parts.query, parts.fragment))


def _origin(work_dir: Path) -> str | None:
    try:
        result = subprocess.run(("git", "config", "--get", "remote.origin.url"),
                                cwd=work_dir, capture_output=True, text=True,
                                timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise GitCommandError(
            f"git config timed out after {GIT_TIMEOUT_SECONDS:g}s") from None
    return result.stdout.strip() if result.returncode == 0 else None


def _has_remote_branch(work_dir: Path, branch: str) -> bool:
    try:
        result = subprocess.run(("git", "rev-parse", "--verify", f"origin/{branch}"),
                                cwd=work_dir, capture_output=True, text=True,
                                timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise GitCommandError(
            f"git rev-parse timed out after {GIT_TIMEOUT_SECONDS:g}s") from None
    return result.returncode == 0


def sync_checkout(repo: str, work_dir: Path, branch: str = "main") -> None:
    """Put `work_dir` at origin/<branch>, cloning first if it is absent.

    Reset-and-clean rather than pull: the tree is fully generated, so starting
    from a known state costs nothing and makes a run independent of whatever
    the previous one left behind. An empty remote has no origin/<branch> yet,
    which is the first-publish case and is not an error.
    """
    if (work_dir / ".git").is_dir():
        origin = _origin(work_dir)
        if origin != repo:
            if origin is not None and _without_userinfo(origin) == _without_userinfo(repo):
                # The same repository with a new credential in its address: a
                # rotated token. The checkout pushes with what its remote
                # holds, so the remote has to carry the new one.
                _git("remote", "set-url", "origin", repo, cwd=work_dir)
            else:
                raise CheckoutBelongsElsewhere(
                    f"{work_dir} is a checkout of a different repository "
                    f"({_without_userinfo(origin or 'no remote')}), not of "
                    f"{_without_userinfo(repo)}. Nothing was published. Remove "
                    f"it, or give this publish a checkout of its own.")
    if not (work_dir / ".git").is_dir():
        work_dir.parent.mkdir(parents=True, exist_ok=True)
        # Routed through _git rather than called bare: a bad --repo value or
        # an unreachable host fails right here, on the very first command,
        # and this is the one call in the module a first-time run cannot
        # avoid hitting.
        _git("clone", repo, str(work_dir), cwd=work_dir.parent)
    else:
        _git("fetch", "origin", cwd=work_dir)
        if _has_remote_branch(work_dir, branch):
            _git("reset", "--hard", f"origin/{branch}", cwd=work_dir)
        _git("clean", "-fd", cwd=work_dir)
    # Deterministic branch name even when cloning an empty repository, where
    # git leaves HEAD on whatever init.defaultBranch happens to be.
    _git("checkout", "-B", branch, cwd=work_dir)
    # A cron host often has no global identity; without this the commit fails.
    _git("config", "user.name", "OMS Publisher", cwd=work_dir)
    _git("config", "user.email", "oms@inogen.ai", cwd=work_dir)


def commit_and_push(work_dir: Path, message: str, branch: str = "main",
                    dry_run: bool = False) -> bool:
    """Stage everything and push. True when a commit was pushed."""
    _git("add", "-A", cwd=work_dir)
    # `diff --cached --quiet` exits 1 when there ARE staged changes, so a zero
    # return means the graph rendered to exactly what the repository already
    # holds. Committing anyway would give a daily cron a daily empty commit.
    try:
        unchanged = subprocess.run(("git", "diff", "--cached", "--quiet"),
                                   cwd=work_dir, capture_output=True, text=True,
                                   timeout=GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise GitCommandError(
            f"git diff timed out after {GIT_TIMEOUT_SECONDS:g}s") from None
    changed = unchanged.returncode != 0
    if dry_run:
        if changed:
            # A dry run that reports only "nothing pushed" is indistinguishable
            # from a no-op run, which is the one thing an operator runs it to
            # tell apart. Staged rather than working-tree diff: `git add -A`
            # above is what makes a brand new skill directory show up at all.
            stat = _git("diff", "--cached", "--stat", cwd=work_dir)
            print("Dry run: these changes would be pushed:")
            # `_git` strips, which eats git's one-space indent from the first
            # line only and leaves the block ragged.
            print("\n".join(line.strip() for line in stat.splitlines()))
        return False
    if not changed:
        return False
    _git("commit", "-m", message, cwd=work_dir)
    _git("push", "origin", f"HEAD:{branch}", cwd=work_dir)
    return True


def publish_to_repo(publisher: Publisher, tenant: str, repo: str,
                    work_dir: Path, branch: str = "main",
                    dry_run: bool = False, *,
                    select: SkillSelector | None = None,
                    skills_only: bool = False,
                    extra_root_files: Mapping[str, str] | None = None,
                    message: str | None = None) -> tuple[GateResult, bool]:
    """Sync, render, and push. Returns `(gate result, whether a commit landed)`.

    `select`, `skills_only` and `extra_root_files` are the destination's
    publish options (`Publisher.publish`), passed only when a caller names
    them: a publisher wrapper written against `publish(tenant, out_dir)` sees
    exactly the call it always saw. `message` is the commit message, by
    default the one every publish has always made."""
    sync_checkout(repo, work_dir, branch)
    options: dict[str, object] = {}
    if select is not None:
        options["select"] = select
    if skills_only:
        options["skills_only"] = True
    if extra_root_files:
        options["extra_root_files"] = extra_root_files
    gate = publisher.publish(tenant, work_dir, **options)
    if not gate.passed:
        # The gate blocks the PUSH, not merely the render. Returning here,
        # before anything is staged, is the whole reason publish runs inside
        # the checkout rather than before it.
        return gate, False
    return gate, commit_and_push(
        work_dir, message or f"Publish {tenant} skills", branch, dry_run)


class CheckoutBusyError(RuntimeError):
    """Another publish already holds the checkout.

    The scheduler and the console's publish button default to the same
    `.publish-checkout` and run in the same container, because start.sh
    supervises both. `sync_checkout` opens with `reset --hard` and `clean -fd`,
    so a button press landing in the middle of a scheduled run deletes the tree
    that run is about to commit from.
    """


def checkout_lock_path(work_dir: Path) -> Path:
    """The lock file for `work_dir`, beside it rather than inside it.

    `sync_checkout` runs `git clean -fd` in the checkout, which would delete a
    lock file kept there while it was still held.
    """
    return work_dir.parent / f"{work_dir.name}.lock"


@contextmanager
def hold_checkout_lock(work_dir: Path) -> Iterator[None]:
    """Hold `work_dir` exclusively for the block, or raise CheckoutBusyError.

    An advisory flock rather than a lock file's existence: the kernel drops it
    when the holder's file handle closes, so a killed publish or a redeploy
    mid-push cannot leave the button refusing for ever. Non-blocking, because
    the caller waiting on the other side is a person waiting on an HTTP
    response, and a queued publish would hold the request open for a whole
    clone before doing the same work the next scheduled slot does anyway.
    """
    path = checkout_lock_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CheckoutBusyError(
                f"another publish is using {work_dir}") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
