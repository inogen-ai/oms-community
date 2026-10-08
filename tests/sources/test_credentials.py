import json

import pytest

from oms.sources.credentials import ProfileAccessError, credential_environment, isolated_environment
from oms.sources.settings import SourceSettings


def test_public_sources_require_no_profile_and_derive_cache(tmp_path):
    settings = SourceSettings.from_env(tmp_path, environ={})
    assert settings.cache_root == tmp_path / 'source-cache'
    assert credential_environment(settings, None, frozenset(), environ={}) == {}


def test_profile_secret_is_required_and_never_serialized(tmp_path):
    env = {'OMS_SOURCE_CREDENTIAL_PROFILES': json.dumps({'work': {'token_env': 'SOURCE_TEST_TOKEN'}})}
    with pytest.raises(ValueError, match='missing'):
        SourceSettings.from_env(tmp_path, environ=env)
    env['SOURCE_TEST_TOKEN'] = 'synthetic-test-value'
    settings = SourceSettings.from_env(tmp_path, environ=env)
    assert 'synthetic-test-value' not in repr(settings)
    with pytest.raises(ProfileAccessError):
        credential_environment(settings, 'work', frozenset(), environ=env)
    child = credential_environment(settings, 'work', frozenset({'work'}), environ=env)
    assert child['GIT_CONFIG_VALUE_0'].startswith('Authorization: Basic ')
    assert 'synthetic-test-value' not in str(child)


def test_environment_cannot_inherit_rewrites_proxies_helpers_or_traces(tmp_path, monkeypatch):
    for key in ['GIT_CONFIG_COUNT', 'GIT_TRACE', 'GIT_SSH', 'HTTPS_PROXY', 'HOME', 'LD_PRELOAD']:
        monkeypatch.setenv(key, 'hostile')
    env = isolated_environment(tmp_path)
    assert env['HOME'] == str(tmp_path)
    assert env['GIT_CONFIG_NOSYSTEM'] == '1'
    assert env['GIT_CONFIG_GLOBAL'] == '/dev/null'
    assert env['GIT_CONFIG_COUNT'] == '0'
    assert not {'GIT_TRACE', 'GIT_SSH', 'HTTPS_PROXY', 'LD_PRELOAD'} & env.keys()


@pytest.mark.parametrize('value', ['{"p":{"token":"secret"}}', '{"p":{"token_env":"BAD-NAME"}}', '[]'])
def test_invalid_profile_configuration(value, tmp_path):
    with pytest.raises(ValueError):
        SourceSettings.from_env(tmp_path, environ={'OMS_SOURCE_CREDENTIAL_PROFILES': value})


def test_core_settings_load_source_settings(tmp_path, monkeypatch):
    from oms.settings.core import CoreSettings
    monkeypatch.setenv('OMS_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('OMS_SOURCE_CACHE_ROOT', str(tmp_path / 'other-cache'))
    settings = CoreSettings.from_env()
    assert settings.source_settings.cache_root == tmp_path / 'other-cache'
    assert settings.source_settings.custody_root == tmp_path / 'data' / 'source-custody'
