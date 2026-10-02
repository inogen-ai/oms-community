"""Extra skill sources: git repositories whose skills are linked beside the
bundle's own.

An extension names them through the installer's `select_sources` fragment, as
`name=url` entries in OMS_SOURCES. The installer clones each one under
`$OMS_DIR/sources/<name>`, links the bundle's skills first and each source's
after, and removes the links of a source that is no longer named. The daily
refresh pulls each source before the bundle. A pull git refuses for access is
a revocation: the source's skills are unlinked until a pull works again. Any
other failure is staleness, and unlinks nothing.

These run the generated POSIX installer and refresh script against a
temporary HOME. The repositories are local bare repositories reached through
`url.<base>.insteadOf`, so the URLs the installer sees are ordinary HTTPS
ones. `git` itself is a stand-in on PATH that hands everything to the real
git, except the pulls and clones a test tells it to refuse or to fail as a
network outage would.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

from oms.ports.publishing import ShellInstallFragments
from oms.publish.render import render_install_script

MCP_URL = "https://oms.example/mcp"
REAL_GIT = shutil.which("git")
BASE = "https://git.example/"

pytestmark = pytest.mark.skipif(REAL_GIT is None, reason="needs git")

# The fragment every test installs with: the sources come from a file in the
# test's HOME, so a test changes them between runs without republishing.
READ_SOURCES = 'OMS_SOURCES="$(cat "$HOME/sources" 2>/dev/null || true)"\n'

# A mute stand-in that uses the core's own helper, as an extension's mute
# clause does: a muted name is skipped, and a link this installer made for it
# is removed, whichever source it points into.
MUTE = '''    if grep -qxF "$name" "$HOME/muted" 2>/dev/null; then
      if [ -L "$target" ] && oms_owned_link "$target"; then rm -f "$target"; fi
      continue
    fi
'''

GIT_STANDIN = f'''#!/bin/sh
# Refuse or fail what the test lists, by the basename of the working folder
# for a pull and by the last path segment of the URL for a clone.
here="$(basename "$(pwd)")"
if [ "$1" = pull ]; then
  if grep -qxF "$here" "$HOME/refuse-pull" 2>/dev/null; then
    echo "ERROR: Repository not found." >&2
    echo "fatal: Could not read from remote repository." >&2
    echo "" >&2
    echo "Please make sure you have the correct access rights" >&2
    echo "and the repository exists." >&2
    exit 128
  fi
  if grep -qxF "$here" "$HOME/offline-pull" 2>/dev/null; then
    echo "fatal: unable to access 'https://git.example/$here.git/': Could not resolve host: git.example" >&2
    exit 128
  fi
fi
if [ "$1" = clone ]; then
  for arg in "$@"; do last_url="$arg"; case "$arg" in https://*) url="$arg" ;; esac; done
  name="$(basename "${{url:-none}}" .git)"
  if grep -qxF "$name" "$HOME/refuse-clone" 2>/dev/null; then
    echo "remote: Repository not found." >&2
    echo "fatal: repository '$url/' not found" >&2
    exit 128
  fi
fi
exec "{REAL_GIT}" "$@"
'''


def _git(*args: str, cwd: Path, env: dict[str, str]) -> None:
    subprocess.run([REAL_GIT, *args], cwd=cwd, env=env, check=True,
                   capture_output=True, text=True)


class Machine:
    """A temporary HOME with Claude Code on it, the remotes it can reach, and
    the bundle cloned from the organisation's remote."""

    def __init__(self, tmp_path: Path, *, fragments: ShellInstallFragments,
                 bundle_clone: str = "oms-org", local_bundle: bool = False) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.remotes = tmp_path / "remotes"
        self.remotes.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        stubs = {
            "claude": '#!/bin/sh\necho "$@" >> "$HOME/claude-cli.log"\n',
            "crontab": ('#!/bin/sh\nif [ "$1" = "-l" ]; then cat "$HOME/crontab.db" 2>/dev/null; '
                        'else cat > "$HOME/crontab.db"; fi\n'),
            "launchctl": "#!/bin/sh\nexit 1\n",
            "git": GIT_STANDIN,
        }
        for name, body in stubs.items():
            (self.bin / name).write_text(body, encoding="utf-8")
            (self.bin / name).chmod(0o755)
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = Test\n\temail = test@example.org\n"
            "[init]\n\tdefaultBranch = main\n"
            f'[url "{self.remotes}/"]\n\tinsteadOf = {BASE}\n', encoding="utf-8")
        self.env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
                    "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                    "GIT_CONFIG_NOSYSTEM": "1"}
        bundle_files = {
            "CLAUDE.md": "# Organisational Constraints\n\nBe kind.\n",
            "AGENTS.md": "# Organisational Constraints\n\nBe kind.\n",
            "install.sh": render_install_script(MCP_URL, fragments=fragments),
            "skills/demo/SKILL.md": "---\nname: Demo\ndescription: A demo.\n---\n",
            ".oms-publication": "revision-1\n",
        }
        if local_bundle:
            self.src = self.tmp / "bundle"
            for relative, body in bundle_files.items():
                (self.src / relative).parent.mkdir(parents=True, exist_ok=True)
                (self.src / relative).write_text(body, encoding="utf-8")
        else:
            self.remote("org", bundle_files)
            self.src = self.home / bundle_clone
            self.src.parent.mkdir(parents=True, exist_ok=True)
            _git("clone", "--quiet", f"{BASE}org.git", str(self.src),
                 cwd=self.tmp, env=self.env)

    def remote(self, name: str, files: dict[str, str]) -> None:
        """Create or replace the remote `name` with `files` as its one commit
        on top of whatever it held."""
        bare = self.remotes / f"{name}.git"
        work = self.tmp / f"work-{name}"
        if not bare.exists():
            _git("init", "--bare", "--quiet", str(bare), cwd=self.tmp, env=self.env)
            _git("clone", "--quiet", str(bare), str(work), cwd=self.tmp, env=self.env)
        for relative, body in files.items():
            path = work / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        _git("add", "-A", cwd=work, env=self.env)
        _git("commit", "--quiet", "-m", "publish", cwd=work, env=self.env)
        _git("push", "--quiet", "origin", "HEAD:main", cwd=work, env=self.env)

    def drop(self, name: str, relative: str) -> None:
        work = self.tmp / f"work-{name}"
        _git("rm", "-r", "--quiet", relative, cwd=work, env=self.env)
        _git("commit", "--quiet", "-m", "retire", cwd=work, env=self.env)
        _git("push", "--quiet", "origin", "HEAD:main", cwd=work, env=self.env)

    def sources(self, *entries: str) -> None:
        (self.home / "sources").write_text(" ".join(entries) + "\n", encoding="utf-8")

    def listing(self, name: str, *values: str) -> None:
        (self.home / name).write_text("".join(f"{v}\n" for v in values), encoding="utf-8")

    def install(self, shell: str = "bash") -> subprocess.CompletedProcess[str]:
        return subprocess.run([shell, str(self.src / "install.sh")], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def refresh(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["sh", str(self.home / ".oms" / "oms-refresh.sh")],
                              env=self.env, capture_output=True, text=True, timeout=120)

    def link(self, name: str) -> Path:
        return self.home / ".claude" / "skills" / name

    def status(self) -> str:
        return (self.home / ".oms" / "status.md").read_text(encoding="utf-8")


def _skill(name: str) -> dict[str, str]:
    return {f"skills/{name}/SKILL.md": f"---\nname: {name}\ndescription: {name}.\n---\n"}


def _machine(tmp_path: Path, **kwargs) -> Machine:
    return Machine(tmp_path, fragments=ShellInstallFragments(
        select_sources=READ_SOURCES, skip_skill=MUTE), **kwargs)


def shells() -> list[str]:
    return ["bash", *(["dash"] if shutil.which("dash") else [])]


# -- Installing ---------------------------------------------------------------------

@pytest.mark.parametrize("shell", shells())
def test_a_source_is_downloaded_and_its_skills_linked(tmp_path, shell):
    machine = _machine(tmp_path)
    machine.remote("finance", _skill("month-end"))
    machine.sources(f"finance={BASE}finance.git")
    run = machine.install(shell)
    assert run.returncode == 0, run.stderr
    clone = machine.home / ".oms" / "sources" / "finance"
    assert machine.link("month-end").resolve() == (clone / "skills" / "month-end").resolve()
    assert machine.link("demo").resolve() == (machine.src / "skills" / "demo").resolve()
    assert (machine.home / ".oms" / "sources.list").read_text(encoding="utf-8").split() == ["finance"]


def test_sources_are_linked_in_order_after_the_bundle(tmp_path):
    machine = _machine(tmp_path)
    machine.remote("finance", {**_skill("demo"), **_skill("shared")})
    machine.remote("leadership", _skill("shared"))
    machine.sources(f"finance={BASE}finance.git", f"leadership={BASE}leadership.git")
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert machine.link("demo").resolve() == (machine.src / "skills" / "demo").resolve()
    sources = machine.home / ".oms" / "sources"
    assert machine.link("shared").resolve() == (sources / "finance" / "skills" / "shared").resolve()
    assert "an earlier source already provides skills/shared" in run.stdout


def test_a_source_no_longer_named_loses_its_links(tmp_path):
    machine = _machine(tmp_path)
    machine.remote("finance", _skill("month-end"))
    machine.sources(f"finance={BASE}finance.git")
    assert machine.install().returncode == 0
    machine.sources()
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert not machine.link("month-end").is_symlink()
    assert machine.link("demo").is_symlink()
    assert "the finance skills are no longer installed here" in run.stdout
    assert (machine.home / ".oms" / "sources.list").read_text(encoding="utf-8").split() == []


def test_a_users_own_link_is_never_swept(tmp_path):
    machine = _machine(tmp_path)
    mine = tmp_path / "mine"
    mine.mkdir()
    (machine.home / ".claude" / "skills").mkdir(parents=True)
    machine.link("mine").symlink_to(mine)
    machine.sources()
    assert machine.install().returncode == 0
    assert machine.link("mine").is_symlink()


def test_a_muted_skill_from_a_source_is_unlinked(tmp_path):
    machine = _machine(tmp_path)
    machine.remote("finance", _skill("month-end"))
    machine.sources(f"finance={BASE}finance.git")
    assert machine.install().returncode == 0
    assert machine.link("month-end").is_symlink()
    machine.listing("muted", "month-end")
    assert machine.install().returncode == 0
    assert not machine.link("month-end").exists() and not machine.link("month-end").is_symlink()


@pytest.mark.parametrize("entry", [
    "Finance=https://git.example/finance.git",
    "fin_ance=https://git.example/finance.git",
    "finance=ftp://git.example/finance.git",
    "finance=file:///tmp/finance.git",
    "finance=/tmp/finance.git",
    "finance=--upload-pack=touch",
    "finance=https://user:token@git.example/finance.git",
    "finance=ext::sh",
    "=https://git.example/finance.git",
    "finance",
])
def test_an_unusable_entry_is_skipped_and_the_install_still_succeeds(tmp_path, entry):
    machine = _machine(tmp_path)
    machine.remote("finance", _skill("month-end"))
    machine.sources(entry)
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert not (machine.home / ".oms" / "sources" / "finance").exists()
    assert not machine.link("month-end").is_symlink()
    assert machine.link("demo").is_symlink()


def test_a_folder_that_is_not_a_clone_is_left_alone(tmp_path):
    machine = _machine(tmp_path)
    machine.remote("finance", _skill("month-end"))
    folder = machine.home / ".oms" / "sources" / "finance"
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("mine\n", encoding="utf-8")
    machine.sources(f"finance={BASE}finance.git")
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert (folder / "notes.txt").read_text(encoding="utf-8") == "mine\n"
    assert not machine.link("month-end").is_symlink()
    assert "is in the way" in (machine.home / ".oms" / ".sources-notice").read_text(encoding="utf-8")


def test_a_moved_repository_is_downloaded_afresh(tmp_path):
    machine = _machine(tmp_path)
    machine.remote("finance", _skill("month-end"))
    machine.remote("finance-new", _skill("year-end"))
    machine.sources(f"finance={BASE}finance.git")
    assert machine.install().returncode == 0
    machine.sources(f"finance={BASE}finance-new.git")
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert machine.link("year-end").is_symlink()
    assert not machine.link("month-end").is_symlink()


def test_a_clone_refused_access_is_reported_and_tried_again(tmp_path):
    machine = _machine(tmp_path)
    machine.remote("finance", _skill("month-end"))
    machine.sources(f"finance={BASE}finance.git")
    machine.listing("refuse-clone", "finance")
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert not machine.link("month-end").is_symlink()
    notice = (machine.home / ".oms" / ".sources-notice").read_text(encoding="utf-8")
    assert "access to their repository was refused" in notice
    machine.listing("refuse-clone")
    assert machine.install().returncode == 0
    assert machine.link("month-end").is_symlink()


def test_a_local_publication_install_skips_sources(tmp_path):
    machine = _machine(tmp_path, local_bundle=True)
    machine.remote("finance", _skill("month-end"))
    machine.sources(f"finance={BASE}finance.git")
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert not (machine.home / ".oms" / "sources" / "finance").exists()
    assert "not a git clone" in run.stdout


def test_a_source_cannot_be_the_bundles_own_folder(tmp_path):
    machine = _machine(tmp_path, bundle_clone=".oms/sources/org")
    machine.sources(f"org={BASE}org.git")
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert "is this bundle's own folder" in run.stderr
    assert machine.link("demo").resolve() == (machine.src / "skills" / "demo").resolve()


# -- Refreshing -------------------------------------------------------------------

def _installed(tmp_path: Path) -> Machine:
    machine = _machine(tmp_path)
    machine.remote("finance", _skill("month-end"))
    machine.sources(f"finance={BASE}finance.git")
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert machine.link("month-end").is_symlink()
    return machine


def test_the_refresh_brings_a_new_skill_from_a_source(tmp_path):
    machine = _installed(tmp_path)
    machine.remote("finance", _skill("year-end"))
    assert machine.refresh().returncode == 0
    assert machine.link("year-end").is_symlink()
    assert machine.status() == ""


def test_a_skill_retired_from_a_source_is_unlinked_on_refresh(tmp_path):
    machine = _installed(tmp_path)
    machine.drop("finance", "skills/month-end")
    machine.refresh()
    assert not machine.link("month-end").is_symlink()


def test_a_refused_pull_unlinks_the_source_and_says_why(tmp_path):
    machine = _installed(tmp_path)
    machine.listing("refuse-pull", "finance")
    machine.refresh()
    assert not machine.link("month-end").is_symlink()
    assert machine.link("demo").is_symlink()
    assert "Access to the finance skills was refused" in machine.status()
    assert (machine.home / ".oms" / "sources" / ".finance.refused").is_file()


def test_restored_access_relinks_the_source_and_clears_the_notice(tmp_path):
    machine = _installed(tmp_path)
    machine.listing("refuse-pull", "finance")
    machine.refresh()
    machine.listing("refuse-pull")
    machine.refresh()
    assert machine.link("month-end").is_symlink()
    assert machine.status() == ""
    assert not (machine.home / ".oms" / "sources" / ".finance.refused").exists()


def test_a_network_failure_keeps_the_links_and_reports_staleness(tmp_path):
    machine = _installed(tmp_path)
    machine.listing("offline-pull", "finance")
    machine.refresh()
    assert machine.link("month-end").is_symlink()
    assert "The finance skills on this machine are out of date" in machine.status()
    assert not (machine.home / ".oms" / "sources" / ".finance.refused").exists()


def test_a_refusal_is_acted_on_even_when_the_bundle_cannot_be_pulled(tmp_path):
    machine = _installed(tmp_path)
    machine.listing("refuse-pull", "finance", "oms-org")
    machine.refresh()
    assert not machine.link("month-end").is_symlink()
    status = machine.status()
    assert "out of date" in status
    assert "Access to the finance skills was refused" in status


def test_without_sources_the_refresh_leaves_the_status_empty(tmp_path):
    machine = Machine(tmp_path, fragments=ShellInstallFragments())
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert machine.refresh().returncode == 0
    assert machine.status() == ""
    assert not (machine.home / ".oms" / "sources").exists()


# -- What counts as refused ---------------------------------------------------------
#
# The pattern both refresh scripts match git's output against, run through
# grep exactly as the generated scripts run it, over what real hosts say.

REFUSALS = [
    "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.",
    "ERROR: Repository not found.\nfatal: Could not read from remote repository.",
    "remote: Repository not found.\nfatal: repository 'https://github.com/acme/x.git/' not found",
    "remote: Invalid username or password.\nfatal: Authentication failed for 'https://github.com/acme/x.git/'",
    "remote: Permission to acme/x.git denied to bob.\nfatal: unable to access "
    "'https://github.com/acme/x.git/': The requested URL returned error: 403",
    "remote: The project you were looking for could not be found or you don't have "
    "permission to view it.\nfatal: repository 'https://gitlab.com/acme/x.git/' not found",
    "fatal: unable to access 'https://dev.azure.com/acme/_git/x/': The requested URL returned error: 401",
    "remote: You do not have permission to access this repository.",
]

NOT_REFUSALS = [
    "ssh: Could not resolve hostname github.com: nodename nor servname provided, or not known\n"
    "fatal: Could not read from remote repository.\n\nPlease make sure you have the correct "
    "access rights\nand the repository exists.",
    "fatal: unable to access 'https://github.com/acme/x.git/': Could not resolve host: github.com",
    "fatal: unable to access 'https://github.com/acme/x.git/': Failed to connect to github.com "
    "port 443 after 75000 ms: Operation timed out",
    "ssh: connect to host github.com port 22: Connection refused",
    "error: cannot open .git/FETCH_HEAD: Permission denied",
    "fatal: Not possible to fast-forward, aborting.",
    "fatal: unable to access 'https://git.example/x.git/': The requested URL returned error: 500",
]


def _matches(message: str) -> bool:
    from oms.publish.render import SOURCE_REFUSAL_PATTERN
    run = subprocess.run(["sh", "-c", 'printf "%s\\n" "$1" | grep -Eqi "$2"', "sh",
                          message, SOURCE_REFUSAL_PATTERN])
    return run.returncode == 0


@pytest.mark.parametrize("message", REFUSALS)
def test_an_access_refusal_is_recognised(message):
    assert _matches(message)


@pytest.mark.parametrize("message", NOT_REFUSALS)
def test_a_network_or_local_failure_is_not_a_refusal(message):
    assert not _matches(message)
