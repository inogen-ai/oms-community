"""How often a git installation refreshes: every three hours.

Once a day was too seldom. cron never runs a job it missed, so a Mac asleep at
midnight skipped that day's pull altogether, and a machine could run days
behind its bundle with nothing wrong anywhere. Eight slots a day bound that to
a few hours.

Machines installed under the old schedule carry an `@daily` entry. Every git
refresh re-runs the installer, which rewrites the entry, so the first pull
that brings this installer moves a machine to the new schedule by itself.

The Windows task cannot run here (there is no Task Scheduler off Windows), so
its trigger is checked as text.
"""
import pytest

from oms.publish.render import render_install_ps1
from tests.community.test_install_sources import MCP_URL, Machine, _machine, shells

EVERY_THREE_HOURS = "0 */3 * * * "


def _entries(machine: Machine) -> list[str]:
    text = (machine.home / "crontab.db").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line]


def _ours(machine: Machine) -> list[str]:
    return [line for line in _entries(machine) if line.endswith("# oms-daily-pull")]


@pytest.mark.parametrize("shell", shells())
def test_a_git_install_pulls_every_three_hours(tmp_path, shell):
    machine = _machine(tmp_path)
    run = machine.install(shell)
    assert run.returncode == 0, run.stderr
    refresh = machine.home / ".oms" / "oms-refresh.sh"
    assert _ours(machine) == [f"{EVERY_THREE_HOURS}sh '{refresh}' >/dev/null 2>&1 # oms-daily-pull"]
    assert "refresh scheduled every three hours" in run.stdout


def test_a_refresh_moves_a_daily_machine_to_the_new_schedule(tmp_path):
    machine = _machine(tmp_path)
    # The shared stand-in truncates its store as `crontab -` starts, which can
    # beat the `crontab -l` feeding it. Real crontab keeps them apart; staging
    # and renaming does the same here, so the user's own entry can be checked.
    (machine.bin / "crontab").write_text(
        '#!/bin/sh\nif [ "$1" = "-l" ]; then cat "$HOME/crontab.db" 2>/dev/null; '
        'else cat > "$HOME/crontab.db.tmp" && mv "$HOME/crontab.db.tmp" "$HOME/crontab.db"; fi\n',
        encoding="utf-8")
    assert machine.install().returncode == 0
    refresh = machine.home / ".oms" / "oms-refresh.sh"
    (machine.home / "crontab.db").write_text(
        "30 8 * * 1 sh backup.sh\n"
        f"@daily sh '{refresh}' >/dev/null 2>&1 # oms-daily-pull\n", encoding="utf-8")
    run = machine.refresh()
    assert run.returncode == 0, run.stderr
    assert "30 8 * * 1 sh backup.sh" in _entries(machine)
    assert _ours(machine) == [f"{EVERY_THREE_HOURS}sh '{refresh}' >/dev/null 2>&1 # oms-daily-pull"]


def test_the_windows_task_repeats_every_three_hours():
    script = render_install_ps1(MCP_URL)
    assert "$trigger = New-ScheduledTaskTrigger -Daily -At 9am" in script
    assert ("-RepetitionInterval (New-TimeSpan -Hours 3) "
            "-RepetitionDuration (New-TimeSpan -Days 1)).Repetition") in script
    assert 'Write-Host "  refresh scheduled every three hours"' in script
