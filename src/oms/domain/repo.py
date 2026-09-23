"""What names a repository, in one place.

Two callers have to agree about whether two spellings of a remote are the same
repo: the client helper bundled into `published/oms_contribute.py`, which reads
`git remote get-url origin` on somebody's laptop, and the server, which anchors
a correction to a bundle. A second implementation of this that drifted would
split one repo's rules across two bundles, and nothing would report it.

The key is the remote URL, not the directory name. A directory name collides
three ways that all occur in practice: a git worktree shares no name with its
repo, a fork or a second clone is checked out under another name, and five
organisations each have a repo called `website`.
"""
from __future__ import annotations

import re

#: Reserved. Every path that mints a skill id refuses it (Task 4), and
#: `is_repo_skill` is the whole of the publish and list exclusion (Task 3).
REPO_ID_PREFIX = "repo-"


class ReservedSkillId(ValueError):
    """A person asked for a skill id inside the reserved namespace.

    A named type rather than a bare `ValueError`, because two layers above have
    to tell this apart from anything else that can go wrong while reading a
    file. `UploadService.stage` turns it into an `UploadRefused` so both
    routers' `except UploadRefused` show the person the sentence below;
    catching plain `ValueError` there would swallow every unrelated fault in
    the parser and report it as a bad archive.

    Still a `ValueError`, so a caller that only wanted to know the input was
    rejected keeps working.
    """


def reserved_id_refusal(skill_id: str) -> str:
    """The one wording for "you cannot call a skill that".

    Two sites mint an id a person chose - console authoring
    (`SkillAdminService.create_skill`) and the importer's frontmatter reader
    (`parse_skill_file`) - and each carried its own copy of the same three
    lines. One copy, so a later edit to the explanation reaches the person
    whichever door they came in by.
    """
    return (f"the {REPO_ID_PREFIX!r} id prefix is reserved for repository "
            f"bundles, which never publish; choose another id "
            f"({skill_id!r} would take it)")

# scp-style remotes (`git@host:owner/name`) are not URLs and urllib will not
# parse them. Matched first, and deliberately not merged into the URL branch:
# the colon means "path separator" here and "port" there, so one pattern
# serving both would have to guess which.
_SCP = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")

# A bare `host/path`, which is this function's own output and therefore the
# shape it has to accept. It is also the shape every piece of documentation
# tells an agent to send: `github.com/acme/payments` is the example in the
# published instruction, in the MCP tool description and in
# `CorrectionPayload.repo`'s field description.
#
# The host half is deliberately narrow. Without a shape to match, any prose
# containing a slash would parse as a repository - "read/write", "and/or", a
# sentence with a slash in it - and a correction would be filed against a
# bundle named after a phrase. So: one token, no whitespace, and at least one
# dot, which every real remote host has and no ordinary English word does.
_BARE = re.compile(r"^(?P<host>[a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z0-9-]+)/(?P<path>\S+)$")

# A port, in the position an scp-style remote puts its path. `host:2222/team/svc`
# is what this function returns for `ssh://git@host:2222/team/svc.git`, so
# without this the key of a key moves the port into the path and idempotency
# fails for every remote that names one. Applied only when there is no `user@`
# in front: git cannot express a port in scp syntax at all, so `git@host:2222/x`
# really does mean the path `2222/x`, while the userless form can only have come
# from here.
_PORT_PATH = re.compile(r"^(?P<port>\d+)/(?P<path>.+)$")


def repo_key(remote: str | None) -> str | None:
    """The normalised remote, or None when the text names no repository.

    None is a real answer and not a failure: a repo with no remote gets no
    bundle, and its rules are judged general or machine like any other.
    """
    text = (remote or "").strip()
    if not text:
        return None
    host, path = _split(text)
    if not host or not path:
        return None
    return _fold(f"{host}/{path}")


def repo_skill_id(key: str) -> str:
    """The Skill id for a bundle. Every separator flattens to a hyphen.

    Skill ids become directory names in the published tree. A repo bundle never
    publishes, so no directory is ever made from this - but an id that could
    not be a filename would be a trap for whoever changes that, and flattening
    costs nothing.
    """
    return REPO_ID_PREFIX + re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")


def is_repo_skill(skill_id: str) -> bool:
    """Whether this id names a repo bundle.

    The id, never a `repo` field. `Skill` fields are named by hand in
    `Neo4jGraphStore.upsert_skill` and `_skill_from_node`, so a field can be
    forgotten and read back as its default with nothing failing - and the
    default of a nullable field is "publishable", which is the unsafe answer.
    The id is the node's primary key and its uniqueness constraint: a
    persistence bug that lost it breaks every lookup loudly instead.
    """
    return skill_id.startswith(REPO_ID_PREFIX)


def _split(text: str) -> tuple[str, str]:
    """Host (with port, without credentials) and path (without .git)."""
    if _SCHEME.match(text):
        # Not urllib.parse: it accepts almost anything, so "not a url at all"
        # would come back with an empty host and an unexpected path rather than
        # as the nothing it is. One regex answers both halves or neither.
        rest = text.split("://", 1)[1]
    else:
        scp = _SCP.match(text)
        if scp is None:
            bare = _BARE.match(text)
            if bare is None:
                return "", ""
            return _host(bare.group("host")), _path(bare.group("path"))
        port = (None if scp.group("user")
                else _PORT_PATH.match(scp.group("path")))
        if port is not None:
            return (_host(f"{scp.group('host')}:{port.group('port')}"),
                    _path(port.group("path")))
        return _host(scp.group("host")), _path(scp.group("path"))
    if "/" not in rest:
        return "", ""
    authority, path = rest.split("/", 1)
    return _host(authority), _path(path)


def _host(authority: str) -> str:
    """The host and its port. Credentials are dropped, never keyed on: a token
    in a remote URL is a secret, and keying on it would put it in the bundle's
    name, its id, and every log line that names it."""
    return authority.rsplit("@", 1)[-1].strip("/")


def _path(path: str) -> str:
    trimmed = path.strip("/")
    return trimmed[:-4] if trimmed.endswith(".git") else trimmed


def _fold(value: str) -> str:
    """Lowercase, and underscores to hyphens.

    The fold mirrors `SkillCatalogue._key` (`src/oms/publish/catalogue.py:454`),
    which already does `.replace("_", "-")` before comparing names. Without it,
    `acme/my_repo` and `acme/my-repo` are two distinct repositories that
    `_resolve` cannot tell apart - and since `_resolve` iterates
    `skills_for_tenant`, whose query has no ORDER BY, which one it returns
    changes between calls.

    Folding here makes that collision happen once and visibly, at the moment
    the key is minted, rather than invisibly inside the catalogue.
    """
    return value.lower().replace("_", "-")
