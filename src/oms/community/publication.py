"""Operator-configured publication destinations for the local Community app."""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re
import socket
from urllib.parse import urlsplit

from oms.publish.git import GitCommandError, hold_checkout_lock, publish_to_repo
from oms.settings.core import GIT_LOOPBACK_REFUSAL, advertises_loopback


DESTINATION_KEYS = frozenset({"publication_target", "publication_folder",
                              "publication_git_url", "publication_git_branch"})


def _folder(value: str) -> str:
    value = value.strip()
    if not value or value == ".":
        return ""
    if (value.startswith("/") or "\\" in value or
            any(part in ("", ".", "..") for part in value.split("/")) or
            re.search(r"[\x00-\x1f'\"`$]", value)):
        raise ValueError("choose a relative folder inside the publication directory")
    return str(PurePosixPath(value))


def _remote(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    # No local paths, Git external transports, flags, or URL credentials are
    # accepted from the browser. Authentication belongs to the operator's Git
    # credential helper / SSH configuration on the publication server.
    if re.fullmatch(r"git@[A-Za-z0-9][A-Za-z0-9.-]*:[A-Za-z0-9_./-]+", value):
        return value
    parsed = urlsplit(value)
    if (parsed.scheme not in ("https", "ssh") or not parsed.hostname or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", parsed.hostname) or
            parsed.password or parsed.query or parsed.fragment or
            (parsed.scheme == "https" and parsed.username) or
            (parsed.username and parsed.username != "git") or
            not parsed.path.strip("/") or
            not re.fullmatch(r"/[A-Za-z0-9_./~%+-]+", parsed.path) or
            re.search(r"[\s\x00-\x1f'\"`$\\]", value)):
        raise ValueError("use an HTTPS or SSH Git URL without a token or password; configure Git access on the server")
    try:
        parsed.port
    except ValueError:
        raise ValueError("the Git URL has an invalid port") from None
    return value


def _branch(value: str) -> str:
    value = value.strip()
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value) or
            any(item in value for item in ("..", "//", "@{")) or
            any(part.startswith(".") or part.endswith((".", ".lock"))
                for part in value.split("/")) or value.endswith("/")):
        raise ValueError("choose a valid Git branch name")
    return value


def destination_settings(settings, settings_store, tenant, changes=None):
    stored = settings_store.get_or_seed_default(tenant).body
    values = {"publication_target": stored.get("publication_target", "local"),
              "publication_folder": stored.get("publication_folder", ""),
              "publication_git_url": stored.get("publication_git_url", ""),
              "publication_git_branch": stored.get("publication_git_branch", "main")}
    values.update(changes or {})
    if values["publication_target"] not in ("local", "git"):
        raise ValueError("choose local or Git publication")
    values["publication_folder"] = _folder(values["publication_folder"])
    values["publication_git_url"] = _remote(values["publication_git_url"])
    values["publication_git_branch"] = _branch(values["publication_git_branch"])
    if changes is not None and values["publication_target"] == "git" and not values["publication_git_url"]:
        raise ValueError("enter a Git repository URL")
    base = Path(settings.publish_root or Path(settings.data_dir) / "published").absolute()
    output = base / values["publication_folder"]
    # Resolve and check on every use: a later symlink must not redirect a saved
    # folder into the database, an agent configuration, or another workspace.
    # /tmp is a system alias on macOS. Ancestors of the operator's base are
    # trusted; only base and descendants are user-configurable.
    cursor = output
    while cursor != base.parent:
        if cursor.is_symlink():
            raise ValueError("the publication directory cannot contain symbolic links")
        cursor = cursor.parent
    if not output.resolve().is_relative_to(base.resolve()):
        raise ValueError("the publication folder must stay inside its configured directory")
    host_base = settings.publish_host_root
    host_path = str(Path(host_base) / values["publication_folder"]) if host_base else str(output)
    if settings.publish_in_container and not host_base:
        host_path = None
    # Docker's default hostname is its container ID. Expose it only when it
    # has that shape, so setup commands target this API rather than another
    # Compose project on the same machine. Custom hostnames need manual lookup.
    hostname = socket.gethostname()
    container_id = hostname if settings.publish_in_container and re.fullmatch(r"[a-f0-9]{12,64}", hostname) else None
    return {**values, "publish_base": str(base), "publish_root": str(output),
            "publish_host_root": host_base, "publish_host_path": host_path,
            "publish_host_home": settings.publish_host_home,
            "publish_launcher_dir": settings.local_launcher_dir,
            "publish_compose_project": settings.compose_project_name,
            "publish_in_container": settings.publish_in_container,
            "publish_container_id": container_id,
            # The address a bundle tells agents to post corrections to, and
            # whether it is loopback. Git publication refuses loopback, so the
            # wizard warns as soon as Git is chosen rather than after the
            # person has created a token and logged the publisher in.
            "public_url": settings.public_url,
            "public_url_is_loopback": advertises_loopback(settings.public_url),
            "publication_ready": values["publication_target"] == "local" or bool(values["publication_git_url"])}


def _check_output(output):
    if output.is_symlink():
        raise ValueError("the publication directory cannot be a symbolic link")
    if output.exists():
        if not output.is_dir():
            raise ValueError("the publication destination must be a directory")
        for item in output.rglob("*"):
            if item.is_symlink():
                raise ValueError("remove symbolic links from the generated publication directory before publishing")


def publish_destination(services, publisher):
    tenant = services.settings.tenant_id
    config = destination_settings(services.settings, services.settings_store, tenant)
    target = config["publication_target"]
    if target == "local":
        output = Path(config["publish_root"])
        # The mount's parent may be a read-only container root (e.g.
        # /published). Locks belong to the writable application data volume.
        identity = hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:24]
        lock = Path(services.settings.data_dir) / "publication-locks" / identity
        with hold_checkout_lock(lock):
            _check_output(output)
            gate = publisher.publish(tenant, output)
        pushed = False
    else:
        remote = config["publication_git_url"]
        if not remote:
            raise ValueError("configure a Git repository before publishing")
        if advertises_loopback(services.settings.public_url):
            # Local publication may name loopback: the agents reading that
            # bundle are on this machine. A Git remote is how a bundle reaches
            # other machines, and there loopback points every correction at the
            # reader's own laptop, where nothing is listening. The corrections
            # are not refused, they are silently lost.
            raise ValueError(GIT_LOOPBACK_REFUSAL)
        # Every destination gets an owned checkout. Changing remote cannot
        # accidentally keep pushing to the previous checkout's origin.
        identity = hashlib.sha256((remote + "\n" + config["publication_git_branch"]).encode()).hexdigest()[:24]
        output = Path(services.settings.data_dir) / "git-publication" / identity

        class CheckedPublisher:
            def publish(self, selected_tenant, selected_output):
                _check_output(selected_output)
                return publisher.publish(selected_tenant, selected_output)

        try:
            with hold_checkout_lock(output):
                _check_output(output)
                gate, pushed = publish_to_repo(CheckedPublisher(), tenant, remote, output,
                                               config["publication_git_branch"])
        except GitCommandError:
            # stderr can repeat credentials supplied by an ambient Git helper.
            raise ValueError("Git publication failed. Check the repository, branch and the server's Git access, then publish again.") from None
    if not gate.passed:
        return gate, None
    return gate, {"output": str(output) if target == "local" else config["publication_git_url"],
                  "skills": len(publisher.publishable_skills(tenant)), "target": target,
                  "host_path": config["publish_host_path"] if target == "local" else None,
                  "pushed": pushed}
