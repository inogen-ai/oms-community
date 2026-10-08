"""The environment Git runs in, and the credential header a profile adds to it.

The environment is closed: no user or system config, no prompts, no SSH, HTTPS only,
no lazy fetching. A token is read from the profile's environment variable at the
moment of use and passed to Git as a header, never written to disk.
"""
import base64
from collections.abc import Mapping
import os
from pathlib import Path

from oms.sources.settings import SourceSettings


class ProfileAccessError(PermissionError):
    """The admitted caller cannot use this profile."""


def isolated_environment(home: Path) -> dict[str, str]:
    return {
        'PATH': '/usr/bin:/bin', 'HOME': str(home), 'XDG_CONFIG_HOME': str(home),
        'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_SYSTEM': '/dev/null',
        'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_COUNT': '0',
        'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '/bin/false',
        'GIT_SSH_COMMAND': '/bin/false', 'GIT_LFS_SKIP_SMUDGE': '1',
        'GIT_ALLOW_PROTOCOL': 'https', 'GIT_NO_LAZY_FETCH': '1',
        'GIT_OPTIONAL_LOCKS': '0',
    }


def credential_environment(settings: SourceSettings, profile_id: str | None,
                           granted_profiles: frozenset[str], *,
                           environ: Mapping[str, str] | None = None,
                           repository_url: str = 'https://github.com/') -> dict[str, str]:
    if profile_id is None:
        return {}
    if profile_id not in granted_profiles:
        raise ProfileAccessError('source credential profile access denied')
    profile = next((p for p in settings.profiles if p.profile_id == profile_id), None)
    if profile is None:
        raise ProfileAccessError('source credential profile unavailable')
    env = os.environ if environ is None else environ
    token = env.get(profile.token_env)
    if not token:
        raise ProfileAccessError('source credential profile has a missing required environment variable')
    encoded = base64.b64encode(('x-access-token:' + token).encode()).decode('ascii')
    return {'GIT_CONFIG_COUNT': '1',
            'GIT_CONFIG_KEY_0': f'http.{repository_url}.extraHeader',
            'GIT_CONFIG_VALUE_0': 'Authorization: Basic ' + encoded}
