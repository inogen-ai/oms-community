"""Operator settings for fetching sources: cache and custody roots and the credential profiles. A profile names
the environment variable holding its token; the token itself is never stored."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from collections.abc import Mapping


@dataclass(frozen=True)
class CredentialProfile:
    profile_id: str
    token_env: str


@dataclass(frozen=True)
class SourceSettings:
    cache_root: Path
    profiles: tuple[CredentialProfile, ...] = ()
    custody_root: Path | None = None

    @classmethod
    def from_env(cls, data_root: Path, *, environ: Mapping[str, str] | None = None) -> 'SourceSettings':
        env = os.environ if environ is None else environ
        root = Path(env['OMS_SOURCE_CACHE_ROOT']) if env.get('OMS_SOURCE_CACHE_ROOT') else data_root / 'source-cache'
        try:
            raw = json.loads(env.get('OMS_SOURCE_CREDENTIAL_PROFILES', '{}'))
        except (ValueError, TypeError):
            raise ValueError('invalid source credential profile configuration') from None
        if not isinstance(raw, dict):
            raise ValueError('source credential profiles must be a mapping')
        profiles = []
        for name, config in raw.items():
            if (not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', name) or not isinstance(config, dict)
                    or set(config) != {'token_env'} or not isinstance(config['token_env'], str)
                    or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', config['token_env'])):
                raise ValueError('invalid source credential profile configuration')
            if not env.get(config['token_env']):
                raise ValueError('source credential profile has a missing required environment variable')
            profiles.append(CredentialProfile(name, config['token_env']))
        return cls(root, tuple(sorted(profiles, key=lambda profile: profile.profile_id)), data_root / 'source-custody')
