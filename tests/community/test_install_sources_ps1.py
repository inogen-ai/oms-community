"""The Windows installer's extra skill sources: install.sh's contract,
in PowerShell.

The same machine as `test_install_sources`: a temporary HOME, local bare
repositories reached through `url.<base>.insteadOf`, and a `git` stand-in that
refuses or fails what a test lists. These need `pwsh`, and skip without it;
off Windows the installer links with symbolic links rather than junctions,
which is what makes it testable here. A `powershell` stand-in hands the
refresh's re-run of the installer to `pwsh`.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

from oms.ports.publishing import PowerShellInstallFragments, ShellInstallFragments
from oms.publish.render import render_install_ps1
from tests.community.test_install_sources import BASE, MCP_URL, Machine, _skill

PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PWSH is None or shutil.which("git") is None,
                                reason="needs pwsh and git")

READ_SOURCES = ('$OmsSources = @((Read-TextFile "$HOME/sources") -split \'\\s+\' '
                '| Where-Object { $_ })\n')

MUTE = '''        if ((Read-TextFile "$HOME/muted") -split '\\s+' -contains $dir.Name) {
            if ((Test-Path -LiteralPath $link) -and (Test-OmsOwnedLink $link)) {
                (Get-Item -LiteralPath $link -Force).Delete()
            }
            continue
        }
'''


class WindowsMachine(Machine):
    def __init__(self, tmp_path: Path, bundle_clone: str = "oms-org") -> None:
        script = render_install_ps1(MCP_URL, fragments=PowerShellInstallFragments(
            select_sources=READ_SOURCES, skip_skill=MUTE))
        super().__init__(tmp_path, fragments=ShellInstallFragments(),
                         bundle_clone=bundle_clone, extra_files={"install.ps1": script})
        shim = self.bin / "powershell"
        shim.write_text(f'#!/bin/sh\nexec "{PWSH}" "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
        self.env["USERPROFILE"] = str(self.home)
        self.env["PATH"] = f"{self.bin}:{Path(PWSH).parent}:/usr/bin:/bin"

    def install(self, shell: str = "pwsh") -> subprocess.CompletedProcess[str]:
        return subprocess.run([PWSH, "-NoProfile", "-File", str(self.src / "install.ps1")],
                              env=self.env, capture_output=True, text=True, timeout=180)

    def refresh(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([PWSH, "-NoProfile", "-File",
                               str(self.home / ".oms" / "oms-refresh.ps1")],
                              env=self.env, capture_output=True, text=True, timeout=180)


def _installed(tmp_path: Path) -> WindowsMachine:
    machine = WindowsMachine(tmp_path)
    machine.remote("finance", _skill("month-end"))
    machine.sources(f"finance={BASE}finance.git")
    run = machine.install()
    assert run.returncode == 0, run.stdout + run.stderr
    assert machine.link("month-end").is_symlink()
    return machine


def test_the_script_with_sources_parses(tmp_path):
    script = tmp_path / "install.ps1"
    script.write_text(render_install_ps1(MCP_URL, fragments=PowerShellInstallFragments(
        select_sources=READ_SOURCES, skip_skill=MUTE)), encoding="utf-8")
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-Command",
         "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
         f"'{script}', [ref]$null, [ref]$e); "
         "if ($e) { $e | ForEach-Object { $_.Message }; exit 1 }"],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_a_source_is_downloaded_and_linked_after_the_bundle(tmp_path):
    machine = WindowsMachine(tmp_path)
    machine.remote("finance", {**_skill("month-end"), **_skill("demo")})
    machine.sources(f"finance={BASE}finance.git")
    run = machine.install()
    assert run.returncode == 0, run.stdout + run.stderr
    clone = machine.home / ".oms" / "sources" / "finance"
    assert machine.link("month-end").resolve() == (clone / "skills" / "month-end").resolve()
    assert machine.link("demo").resolve() == (machine.src / "skills" / "demo").resolve()
    assert "an earlier source already provides skills/demo" in run.stdout
    assert (machine.home / ".oms" / "sources.list").read_text(encoding="utf-8").split() == ["finance"]


def test_a_source_no_longer_named_loses_its_links(tmp_path):
    machine = _installed(tmp_path)
    machine.sources()
    run = machine.install()
    assert run.returncode == 0, run.stdout + run.stderr
    assert not machine.link("month-end").is_symlink()
    assert "the finance skills are no longer installed here" in run.stdout


def test_a_muted_skill_from_a_source_is_unlinked(tmp_path):
    machine = _installed(tmp_path)
    machine.listing("muted", "month-end")
    assert machine.install().returncode == 0
    assert not machine.link("month-end").is_symlink()


@pytest.mark.parametrize("entry", [
    "Finance=https://git.example/finance.git",
    "finance=file:///tmp/finance.git",
    "finance=--upload-pack=touch",
    "finance=https://user:token@git.example/finance.git",
    "finance=-x@git.example:finance.git",
    "finance=ssh://-x@git.example/finance.git",
    "finance",
])
def test_an_unusable_entry_is_skipped(tmp_path, entry):
    machine = WindowsMachine(tmp_path)
    machine.remote("finance", _skill("month-end"))
    machine.sources(entry)
    run = machine.install()
    assert run.returncode == 0, run.stdout + run.stderr
    assert not (machine.home / ".oms" / "sources" / "finance").exists()
    assert machine.link("demo").is_symlink()


def test_a_refused_pull_unlinks_and_restored_access_relinks(tmp_path):
    machine = _installed(tmp_path)
    machine.listing("refuse-pull", "finance")
    machine.refresh()
    assert not machine.link("month-end").is_symlink()
    assert "Access to the finance skills was refused" in machine.status()
    machine.listing("refuse-pull")
    machine.refresh()
    assert machine.link("month-end").is_symlink()
    assert machine.status() == ""


def test_a_network_failure_keeps_the_links_and_reports_staleness(tmp_path):
    machine = _installed(tmp_path)
    machine.listing("offline-pull", "finance")
    machine.refresh()
    assert machine.link("month-end").is_symlink()
    assert "The finance skills on this machine are out of date" in machine.status()


def test_without_sources_the_refresh_leaves_the_status_empty(tmp_path):
    machine = WindowsMachine(tmp_path)
    machine.sources()
    assert machine.install().returncode == 0
    assert machine.refresh().returncode == 0
    assert machine.status() == ""


def test_a_source_beside_a_bundle_in_the_sources_folder_is_unlinked_when_refused(tmp_path):
    machine = WindowsMachine(tmp_path, bundle_clone=".oms/sources/org")
    machine.remote("org-ops", _skill("rota"))
    machine.sources(f"org-ops={BASE}org-ops.git")
    assert machine.install().returncode == 0
    assert machine.link("rota").is_symlink()
    machine.listing("refuse-pull", "org-ops")
    machine.refresh()
    assert not machine.link("rota").is_symlink()
    assert machine.link("demo").is_symlink()


def test_a_refusal_is_recognised_whatever_language_the_machine_speaks(tmp_path):
    machine = _installed(tmp_path)
    machine.env["LC_ALL"] = "de_DE.UTF-8"
    machine.listing("refuse-pull", "finance")
    machine.refresh()
    assert not machine.link("month-end").is_symlink()
    assert "Access to the finance skills was refused" in machine.status()


def test_running_the_installer_retries_a_refused_source_at_once(tmp_path):
    machine = _installed(tmp_path)
    machine.listing("refuse-pull", "finance")
    machine.refresh()
    assert not machine.link("month-end").is_symlink()
    machine.listing("refuse-pull")
    run = machine.install()
    assert run.returncode == 0, run.stdout + run.stderr
    assert machine.link("month-end").is_symlink()


def test_the_separator_reaches_git_quoted():
    """PowerShell drops a bare -- when it binds a function's arguments, so the
    one that keeps an address from reading as an option is quoted."""
    script = render_install_ps1(MCP_URL, fragments=PowerShellInstallFragments(
        select_sources=READ_SOURCES))
    assert "Invoke-SourceGit clone --quiet '--' $url" in script
    assert "Invoke-SourceGit clone --quiet -- " not in script

