"""Credential-safe URLs for generated public documents."""
from urllib.parse import urlsplit, urlunsplit


def public_repo_url(raw: str | None) -> str | None:
    """The distribution repository's remote with any credential removed.

    SKILLS_REPO_URL is the variable the publisher pushes with, and the shape
    the runbook recommends carries a write token in the userinfo:
    `https://x-access-token:<PAT>@github.com/<org>/skills.git`. The README
    published into that same repository tells a new starter which URL to
    clone, so passing the variable through unfiltered would hand a token with
    write access to the organisation's skills to every machine that installs
    the bundle, and to anyone who can read the repository.

    Stripping happens here, at the boundary where the variable enters the
    process, rather than at the point of use: a second caller that forgot
    would reintroduce the leak, and there is no legitimate reason for the
    credential to travel further than the git remote itself.

    scp-style SSH remotes (`git@github.com:org/repo.git`) are returned as
    they are. They have no userinfo field, and `git@` is the transport user
    every such URL carries rather than anything secret.
    """
    if not raw or not raw.strip():
        return None
    url = raw.strip()
    parts = urlsplit(url)
    if not parts.scheme or "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
