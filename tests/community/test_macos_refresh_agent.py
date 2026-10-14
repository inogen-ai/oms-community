"""macOS git installations refresh through a launch agent, not cron.

On macOS a cron job may read its crontab but not write it: `crontab -` fails
with "Operation not permitted" unless the caller has Full Disk Access, which
cron jobs do not have. So a cron-run refresh can never change its own
schedule, and the three-hour line of 1.4.1 never reached a Mac that still ran
the old daily one. A cron job can load a launch agent, though, and launchd
runs a calendar job it missed while the Mac slept as soon as the Mac wakes.
Both were checked on a real Mac on 14 October 2026.

The agent fires at half past every hour, and once when it loads. The refresh
script it starts refreshes only when three hours have passed since the last
scheduled run, so a later change of period changes the script and never the
agent. A refresh run by hand always refreshes. The agent marks its runs with
OMS_SCHEDULED_BY=launchd, and the installer never reloads the agent from
inside one: that would stop the job doing the reload.

These run the generated installer against a temporary HOME with `uname`,
`launchctl` and `crontab` stood in, so they pass on Linux too.
"""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import time

import pytest

from tests.community.test_install_sources import Machine, _machine, _skill, shells

AGENT_LABEL = "ai.inogen.oms.refresh"
SCHEDULED = {"OMS_SCHEDULED_BY": "launchd"}


def _mac(tmp_path: Path, *, system: str = "Darwin") -> Machine:
    machine = _machine(tmp_path)
    stubs = {
        "uname": (f'#!/bin/sh\nif [ "$1" = -s ]; then echo {system}; '
                  'else exec /usr/bin/uname "$@"; fi\n'),
        "launchctl": ('#!/bin/sh\necho "$@" >> "$HOME/launchctl-calls"\n'
                      'if [ "$1" = bootstrap ] && [ -f "$HOME/launchctl-refuse" ]; then exit 5; fi\n'),
        # Staged and renamed, so `crontab -` cannot truncate the store before
        # the `crontab -l` feeding it has read it. A refusal is macOS's own.
        "crontab": ('#!/bin/sh\nif [ "$1" = "-l" ]; then cat "$HOME/crontab.db" 2>/dev/null; exit 0; fi\n'
                    'if [ -f "$HOME/crontab-refuse" ]; then cat >/dev/null; '
                    'echo "crontab: tmp/tmp.1: Operation not permitted" >&2; exit 1; fi\n'
                    'cat > "$HOME/crontab.db.tmp" && mv "$HOME/crontab.db.tmp" "$HOME/crontab.db"\n'),
    }
    for name, body in stubs.items():
        (machine.bin / name).write_text(body, encoding="utf-8")
        (machine.bin / name).chmod(0o755)
    return machine


def _agent(machine: Machine) -> Path:
    return machine.home / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def _refresh_script(machine: Machine) -> Path:
    return machine.home / ".oms" / "oms-refresh.sh"


def _calls(machine: Machine) -> list[str]:
    path = machine.home / "launchctl-calls"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _cron(machine: Machine) -> list[str]:
    path = machine.home / "crontab.db"
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line] if path.exists() else []


def _seed_daily(machine: Machine) -> str:
    line = f"@daily sh '{_refresh_script(machine)}' >/dev/null 2>&1 # oms-daily-pull"
    (machine.home / "crontab.db").write_text(f"30 8 * * 1 sh backup.sh\n{line}\n", encoding="utf-8")
    return line


def _run(machine: Machine, script: Path, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(script)], env={**machine.env, **env},
                          capture_output=True, text=True, timeout=120)


def _stamp(machine: Machine) -> Path:
    return machine.home / ".oms" / "last-scheduled-refresh"


@pytest.mark.parametrize("shell", shells())
def test_a_mac_git_install_schedules_the_launch_agent(tmp_path, shell):
    machine = _mac(tmp_path)
    _seed_daily(machine)
    run = machine.install(shell)
    assert run.returncode == 0, run.stderr
    agent = _agent(machine)
    assert plistlib.loads(agent.read_bytes()) == {
        "Label": AGENT_LABEL,
        "ProgramArguments": ["/bin/sh", str(_refresh_script(machine))],
        "StartCalendarInterval": {"Minute": 30},
        "RunAtLoad": True,
        "EnvironmentVariables": SCHEDULED,
    }
    assert f"bootstrap gui/{os.getuid()} {agent}" in _calls(machine)
    # The old daily line goes; the person's own entry stays.
    assert _cron(machine) == ["30 8 * * 1 sh backup.sh"]
    assert "refresh scheduled every three hours (launch agent)" in run.stdout
    assert int(_stamp(machine).read_text(encoding="utf-8")) <= int(time.time())


def test_a_crontab_macos_will_not_change_keeps_the_old_line_and_still_installs(tmp_path):
    machine = _mac(tmp_path)
    daily = _seed_daily(machine)
    (machine.home / "crontab-refuse").touch()
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert _agent(machine).is_file()
    assert daily in _cron(machine)
    assert "the old daily cron entry stays" in run.stdout


def test_the_agent_never_reloads_itself(tmp_path):
    machine = _mac(tmp_path)
    assert machine.install().returncode == 0
    before = _agent(machine).read_bytes()
    (machine.home / "launchctl-calls").unlink()
    run = _run(machine, machine.src / "install.sh", **SCHEDULED)
    assert run.returncode == 0, run.stderr
    assert _calls(machine) == []
    assert _agent(machine).read_bytes() == before


def test_a_mac_whose_agent_will_not_load_falls_back_to_cron(tmp_path):
    machine = _mac(tmp_path)
    (machine.home / "launchctl-refuse").touch()
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert not _agent(machine).exists()
    assert [line for line in _cron(machine) if line.endswith("# oms-daily-pull")] == [
        f"0 */3 * * * sh '{_refresh_script(machine)}' >/dev/null 2>&1 # oms-daily-pull"]


def test_linux_keeps_cron(tmp_path):
    machine = _mac(tmp_path, system="Linux")
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert not _agent(machine).exists()
    assert _calls(machine) == []
    assert [line for line in _cron(machine) if line.endswith("# oms-daily-pull")] == [
        f"0 */3 * * * sh '{_refresh_script(machine)}' >/dev/null 2>&1 # oms-daily-pull"]


def test_a_scheduled_run_waits_for_three_hours(tmp_path):
    machine = _mac(tmp_path)
    assert machine.install().returncode == 0
    machine.remote("org", _skill("fresh"))
    # Just installed: the run the agent starts when it loads finds nothing due.
    assert _run(machine, _refresh_script(machine), **SCHEDULED).returncode == 0
    assert not machine.link("fresh").exists()
    _stamp(machine).write_text(f"{int(time.time()) - 4 * 3600}\n", encoding="utf-8")
    assert _run(machine, _refresh_script(machine), **SCHEDULED).returncode == 0
    assert machine.link("fresh").exists()
    assert int(time.time()) - int(_stamp(machine).read_text(encoding="utf-8")) < 60


def test_a_refresh_by_hand_never_waits(tmp_path):
    machine = _mac(tmp_path)
    assert machine.install().returncode == 0
    machine.remote("org", _skill("fresh"))
    assert machine.refresh().returncode == 0
    assert machine.link("fresh").exists()


def test_moving_to_local_publication_retires_the_agent(tmp_path):
    machine = _mac(tmp_path)
    assert machine.install().returncode == 0
    agent = _agent(machine)
    assert agent.is_file()
    shutil.rmtree(machine.src / ".git")
    run = machine.install()
    assert run.returncode == 0, run.stderr
    assert not agent.exists()
    assert f"bootout gui/{os.getuid()} {agent}" in _calls(machine)


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS only")
def test_macos_accepts_the_agent_file(tmp_path):
    machine = _mac(tmp_path)
    assert machine.install().returncode == 0
    lint = subprocess.run(["plutil", "-lint", str(_agent(machine))], capture_output=True, text=True)
    assert lint.returncode == 0, lint.stdout + lint.stderr
