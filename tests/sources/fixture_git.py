"""Registered synthetic repositories for deterministic source acquisition tests."""
from pathlib import Path
import json
import subprocess

from oms.sources.limits import run_bounded


class FixtureGitTransport:
    def __init__(self, root: Path):
        self.root = root
        self.registered: dict[str, Path] = {}
        self.environments: list[dict[str, str]] = []
        self.endpoints: list[str] = []
        self.local_path_reads: list[str] = []
        self.fetches = 0

    def register(self, endpoint: str, files: dict[str, bytes], *, branch: str = 'main',
                 filter_blobs: bool = False) -> str:
        index = len(self.registered)
        work = self.root / f'work-{index}'
        work.mkdir(parents=True)
        self._git(work, 'init', '-q', '-b', branch)
        self._git(work, 'config', 'user.name', 'Fixture')
        self._git(work, 'config', 'user.email', 'fixture@example.invalid')
        for name, body in files.items():
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        self._git(work, 'add', '.')
        self._git(work, 'commit', '-qm', 'Fixture revision')
        remote = self.root / f'remote-{index}.git'
        self._git(self.root, 'clone', '-q', '--bare', str(work), str(remote))
        if filter_blobs:
            self._git(remote, 'config', 'uploadpack.allowFilter', 'true')
        self.registered[endpoint] = remote
        return self._git(work, 'rev-parse', 'HEAD').strip()

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        result = subprocess.run(['/usr/bin/git', *args], cwd=cwd, check=True,
                                capture_output=True, text=True,
                                env={'PATH': '/usr/bin:/bin', 'HOME': str(cwd),
                                     'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'})
        return result.stdout

    def select_revision(self, endpoint: str, state_file: Path) -> None:
        state = json.loads(state_file.read_text())
        revision = state['revision']
        remote = self.registered[endpoint]
        self._git(remote, 'cat-file', '-e', revision + '^{commit}')
        self._git(remote, 'update-ref', 'refs/heads/main', revision)

    def run(self, args, env, cache, budget, *, stdin=None, stdout_cap=None):
        self.environments.append(dict(env))
        translated = list(args)
        for i, arg in enumerate(translated):
            if arg.startswith('https://'):
                if arg not in self.registered:
                    raise ValueError('unregistered fixture endpoint')
                self.endpoints.append(arg)
                translated[i] = self.registered[arg].resolve().as_uri()
        if 'fetch' in args:
            self.fetches += 1
        fixture_env = dict(env, GIT_ALLOW_PROTOCOL='file')
        return run_bounded(['/usr/bin/git', '-c', 'protocol.file.allow=always', *translated],
                           fixture_env, cache, budget, stdin=stdin, stdout_cap=stdout_cap)
