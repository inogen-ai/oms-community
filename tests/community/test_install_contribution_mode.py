"""Each installation chooses whether its agents share corrections by
themselves (automatic) or ask the person first (confirm).

The choice belongs to the machine, not to the published bundle: a bundle
carries both sets of root instructions, and the installer picks one from
OMS_CONTRIBUTION_MODE, else from the choice the machine recorded on its last
run, else automatic. Every agent tool installed in one run is given the same
variant, the refresh keeps it, and a machine that chose confirm is never
moved back to automatic except by choosing automatic again.

These tests run the generated POSIX installer against a temporary HOME and a
temporary bundle. Every command the installer could use to reach outside the
test is shadowed on PATH: the agent CLIs, `crontab`, which would otherwise
schedule a job on the machine running the suite, and `launchctl`, which a
temporary HOME does not isolate. The PowerShell installer is checked by what
it renders, because `pwsh` is not assumed.
"""
from collections.abc import Sequence
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest

from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.models import Constraint, Skill
from oms.ports.publishing import PowerShellInstallFragments, ShellInstallFragments
from oms.publish.publisher import Publisher
from oms.publish.render import (
    DEFAULT_ROOT_INSTRUCTION_FILES, _contribution_block, render_install_ps1,
    render_install_script, render_readme,
)

MCP_URL = "https://oms.example/mcp"
# The confirm-mode question, which only the confirm instructions ask.
OFFER = ("Should this apply just to this task, or would you like to suggest it "
         "for the team's <skill> guidance?")
# Quoted exactly: what the installer says when it refuses a mode.
MODE_ERROR = "OMS_CONTRIBUTION_MODE must be automatic or confirm"
NO_VARIANT = ("This bundle has no confirm-mode instructions. "
              "Ask your OMS administrator to publish again.")
# What a confirm-mode run says on a bundle that takes no contributions.
NO_CONTRIBUTIONS = ("This bundle takes no contributions, so both modes install the "
                    "same instructions. Confirm mode is kept for when it does.")
# The heading every published contribution block starts with.
CONTRIBUTING = "\n# Contributing learnings\n\nLog qualifying corrections in the same turn.\n"
# The confirm copy published beside each root file, spelled out rather than
# derived: these are the names the installer has to open.
VARIANT = {"AGENTS.md": "AGENTS.confirm.md", "CLAUDE.md": "CLAUDE.confirm.md"}
# Every agent tool the installer knows, by the directory that shows it is there.
HARNESS_DIRS = (".claude", ".codex", ".codeium/windsurf", ".gemini", ".cursor")
# Where the tools that take a copy of the instructions read it, under HOME.
COPIED = (".codex/AGENTS.md", ".codeium/windsurf/memories/global_rules.md",
          ".gemini/AGENTS.md")
# The file a tool with no global instructions file is given to paste.
STAGED = ".oms/cursor-user-rules.md"
MODE_FILE = ".oms/contribution-mode"
# The mode the tools were last given, written at the end of a run.
INSTALLED_FILE = ".oms/contribution-mode-installed"
CONTRIBUTION_TOOLS = ("log_correction", "log_signal")


def automatic_text(name: str) -> str:
    return f"# Organisational Constraints\n\nAutomatic contribution, from {name}.\n"


def confirm_text(name: str) -> str:
    return f"# Organisational Constraints\n\nAsk the person first, from {name}.\n"


def make_bundle(tmp_path: Path, *, root_files: Sequence[str] | None = None,
                variants: bool = True,
                fragments: ShellInstallFragments | None = None) -> Path:
    """A published bundle's root, in the local-folder form: no Git clone,
    and a publication revision the local refresh compares."""
    bundle = tmp_path / "bundle"
    (bundle / "skills" / "demo").mkdir(parents=True)
    (bundle / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: Demo\ndescription: A demo skill.\n---\n", encoding="utf-8")
    for name in root_files or DEFAULT_ROOT_INSTRUCTION_FILES:
        (bundle / name).write_text(automatic_text(name), encoding="utf-8")
        if variants:
            (bundle / VARIANT[name]).write_text(confirm_text(VARIANT[name]), encoding="utf-8")
    (bundle / "install.sh").write_text(
        render_install_script(MCP_URL, root_files, fragments=fragments), encoding="utf-8")
    (bundle / ".oms-publication").write_text("revision-1\n", encoding="utf-8")
    return bundle


def make_machine(tmp_path: Path, *harness_dirs: str) -> tuple[Path, Path]:
    """A HOME holding the given agent tools' directories, and a directory of
    stand-in commands to put first on PATH. Returns both."""
    home = tmp_path / "home"
    home.mkdir()
    for directory in harness_dirs:
        (home / directory).mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        # Record what they were asked to do, and do nothing else.
        "claude": '#!/bin/sh\necho "$@" >> "$HOME/claude-cli.log"\n',
        "codex": "#!/bin/sh\nexit 0\n",
        "crontab": ('#!/bin/sh\nif [ "$1" = "-l" ]; then cat "$HOME/crontab.db" 2>/dev/null; '
                    'else cat > "$HOME/crontab.db"; fi\n'),
        # Never the real one: a temporary HOME does not isolate the user's
        # launch agents.
        "launchctl": "#!/bin/sh\nexit 1\n",
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    return home, bin_dir


def environment(home: Path, bin_dir: Path, mode: str | None = None) -> dict[str, str]:
    """Built from nothing, so neither a mode nor a Claude configuration
    directory set in the environment running the suite can reach the run."""
    env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin",
           "CLAUDE_CONFIG_DIR": str(home / ".claude")}
    if mode is not None:
        env["OMS_CONTRIBUTION_MODE"] = mode
    return env


def install(bundle: Path, home: Path, bin_dir: Path, mode: str | None = None,
            shell: str = "bash") -> subprocess.CompletedProcess[str]:
    return subprocess.run([shell, str(bundle / "install.sh")],
                          env=environment(home, bin_dir, mode),
                          capture_output=True, text=True, timeout=60)


def snapshot(root: Path) -> dict[str, object]:
    """Everything under `root`: each entry's kind, content or link target,
    and each file's modification time, so a bare `touch` shows as a change."""
    state: dict[str, object] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            state[relative] = ("link", str(path.readlink()))
        elif path.is_dir():
            state[relative] = ("dir",)
        else:
            state[relative] = ("file", path.read_bytes(), path.stat().st_mtime_ns)
    return state


def read(home: Path, relative: str) -> str:
    return (home / relative).read_text(encoding="utf-8")


def codex_config(home: Path) -> dict:
    return tomllib.loads(read(home, ".codex/config.toml"))


def shells() -> list[str]:
    """bash, and dash where it is installed: a stricter POSIX sh, which is
    what the generated script promises to run under."""
    return ["bash", *(["dash"] if shutil.which("dash") else [])]


@pytest.mark.parametrize("shell", shells())
def test_unset_mode_installs_automatic_and_records_it(tmp_path: Path, shell: str) -> None:
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, *HARNESS_DIRS)

    run = install(bundle, home, bin_dir, shell=shell)

    assert run.returncode == 0, run.stderr
    assert f"@{bundle}/CLAUDE.md\n" in read(home, ".claude/CLAUDE.md")
    assert "CLAUDE.confirm.md" not in read(home, ".claude/CLAUDE.md")
    for relative in COPIED:
        assert automatic_text("CLAUDE.md") in read(home, relative), relative
    assert read(home, STAGED) == automatic_text("CLAUDE.md")
    assert read(home, MODE_FILE) == "automatic\n"
    assert "Contribution mode: automatic" in run.stdout.splitlines()
    # Said on the terminal, not in the status file every agent is pointed
    # at, where empty is what healthy looks like.
    assert read(home, ".oms/status.md") == ""


@pytest.mark.parametrize("shell", shells())
@pytest.mark.parametrize("root_files", [None, ["AGENTS.md"]], ids=["default", "agents-only"])
def test_confirm_mode_installs_the_confirm_variant_for_every_harness(
        tmp_path: Path, shell: str, root_files: list[str] | None) -> None:
    """One run, one variant: an import for Claude Code, a copy for the tools
    that take one, and the text a Cursor user pastes, all from the confirm
    copy of the root file the installer imports."""
    bundle = make_bundle(tmp_path, root_files=root_files)
    home, bin_dir = make_machine(tmp_path, *HARNESS_DIRS)
    imported = "CLAUDE.md" if root_files is None else "AGENTS.md"

    run = install(bundle, home, bin_dir, "confirm", shell=shell)

    assert run.returncode == 0, run.stderr
    claude_md = read(home, ".claude/CLAUDE.md")
    assert f"@{bundle}/{VARIANT[imported]}\n" in claude_md
    assert f"@{bundle}/{imported}\n" not in claude_md
    for relative in COPIED:
        body = read(home, relative)
        assert confirm_text(VARIANT[imported]) in body, relative
        assert "Automatic contribution" not in body, relative
    assert read(home, STAGED) == confirm_text(VARIANT[imported])
    assert read(home, MODE_FILE) == "confirm\n"
    assert "Contribution mode: confirm" in run.stdout.splitlines()


def test_the_mode_survives_a_refresh_without_the_variable(tmp_path: Path) -> None:
    """The scheduled refresh re-runs the installer with none of the
    environment of the run that chose the mode, so the recorded choice is
    what keeps a confirm machine on the confirm instructions."""
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, *HARNESS_DIRS)
    assert install(bundle, home, bin_dir, "confirm").returncode == 0

    # A new publication lands in the folder, and the refresh finds it.
    (bundle / "CLAUDE.confirm.md").write_text(confirm_text("the next publication"),
                                              encoding="utf-8")
    (bundle / ".oms-publication").write_text("revision-2\n", encoding="utf-8")
    refresh = subprocess.run(["sh", str(home / ".oms" / "oms-refresh.sh")],
                             env=environment(home, bin_dir), capture_output=True,
                             text=True, timeout=60)

    assert refresh.returncode == 0, refresh.stderr
    assert read(home, ".oms/local-revision") == "revision-2\n"
    for relative in COPIED:
        assert confirm_text("the next publication") in read(home, relative), relative
    assert f"@{bundle}/CLAUDE.confirm.md\n" in read(home, ".claude/CLAUDE.md")
    assert read(home, MODE_FILE) == "confirm\n"
    assert read(home, ".oms/status.md") == ""

    # A later run by hand without the variable keeps it too.
    again = install(bundle, home, bin_dir)
    assert again.returncode == 0, again.stderr
    assert "Contribution mode: confirm" in again.stdout.splitlines()
    assert read(home, MODE_FILE) == "confirm\n"


@pytest.mark.parametrize("installed_before", [False, True], ids=["fresh", "installed"])
@pytest.mark.parametrize("value", ["confirmed", "Confirm", "manual", " confirm", " "])
def test_an_invalid_mode_changes_nothing(tmp_path: Path, value: str,
                                         installed_before: bool) -> None:
    """Refused before the first write, on a fresh machine and on one that
    was installed before, whose recorded choice stands."""
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, *HARNESS_DIRS)
    if installed_before:
        assert install(bundle, home, bin_dir, "confirm").returncode == 0
    before = snapshot(home)

    run = install(bundle, home, bin_dir, value)

    assert run.returncode != 0
    assert MODE_ERROR in run.stderr.splitlines()
    assert snapshot(home) == before


def test_an_empty_variable_falls_through_to_the_recorded_choice(tmp_path: Path) -> None:
    """Only a set, non-empty variable is a choice, so `OMS_CONTRIBUTION_MODE=`
    is the same as leaving it unset."""
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, *HARNESS_DIRS)
    assert install(bundle, home, bin_dir, "confirm").returncode == 0

    run = install(bundle, home, bin_dir, "")

    assert run.returncode == 0, run.stderr
    assert read(home, MODE_FILE) == "confirm\n"


@pytest.mark.parametrize("recorded", ["confirmed\n", "", "\n"], ids=["misspelt", "empty", "blank"])
def test_an_unusable_recorded_choice_is_refused_until_one_is_made(
        tmp_path: Path, recorded: str) -> None:
    """A recorded choice that cannot be read as a mode is not a choice of
    automatic. The refresh then fails where a person can see it, and an
    explicit choice replaces the record."""
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, *HARNESS_DIRS)
    (home / ".oms").mkdir()
    (home / MODE_FILE).write_text(recorded, encoding="utf-8")
    before = snapshot(home)

    refused = install(bundle, home, bin_dir)

    assert refused.returncode != 0
    assert MODE_ERROR in refused.stderr.splitlines()
    assert str(home / MODE_FILE) in refused.stderr
    assert snapshot(home) == before
    # The way out names both modes, confirm first. A record that cannot be
    # read may well have said confirm, and automatic is never the fix for
    # that on its own: going back to it is a choice, not a repair.
    examples = [line.strip() for line in refused.stderr.splitlines()
                if line.strip().startswith("OMS_CONTRIBUTION_MODE=")]
    assert examples == [f"OMS_CONTRIBUTION_MODE={mode} sh '{bundle}/install.sh'"
                        for mode in ("confirm", "automatic")]

    chosen = install(bundle, home, bin_dir, "automatic")
    assert chosen.returncode == 0, chosen.stderr
    assert read(home, MODE_FILE) == "automatic\n"


def test_confirm_without_a_confirm_variant_fails_before_writing(tmp_path: Path) -> None:
    """Never a silent fall back to the automatic instructions: that would
    share corrections without asking the person who chose to be asked. Only
    choosing automatic again installs them. A bundle that takes
    contributions and has no confirm copy is an older publication, which
    publishing again brings up to date."""
    bundle = make_bundle(tmp_path, variants=False)
    for name in DEFAULT_ROOT_INSTRUCTION_FILES:
        with (bundle / name).open("a", encoding="utf-8") as root:
            root.write(CONTRIBUTING)
    home, bin_dir = make_machine(tmp_path, *HARNESS_DIRS)
    before = snapshot(home)

    asked = install(bundle, home, bin_dir, "confirm")

    assert asked.returncode != 0
    assert NO_VARIANT in asked.stderr.splitlines()
    assert snapshot(home) == before

    # A machine that recorded confirm is held to it by the refresh too.
    (home / ".oms").mkdir()
    (home / MODE_FILE).write_text("confirm\n", encoding="utf-8")
    recorded = snapshot(home)
    refreshed = install(bundle, home, bin_dir)
    assert refreshed.returncode != 0
    assert NO_VARIANT in refreshed.stderr.splitlines()
    assert snapshot(home) == recorded

    back = install(bundle, home, bin_dir, "automatic")
    assert back.returncode == 0, back.stderr
    assert read(home, MODE_FILE) == "automatic\n"
    assert f"@{bundle}/CLAUDE.md\n" in read(home, ".claude/CLAUDE.md")


def test_confirm_on_a_bundle_that_takes_no_contributions_installs_its_one_file(
        tmp_path: Path) -> None:
    """A bundle published without a contribution endpoint has no contribution
    block, so its root file says the same in both modes and no confirm copy
    exists. Asking the administrator to publish again cannot help there, and
    choosing automatic to get past it would share without asking the day the
    bundle starts taking contributions. So the one file is installed and the
    machine keeps confirm, and the refresh after a publication that takes
    contributions installs the confirm copy."""
    bundle = make_bundle(tmp_path, variants=False)
    home, bin_dir = make_machine(tmp_path, *HARNESS_DIRS)

    run = install(bundle, home, bin_dir, "confirm")

    assert run.returncode == 0, run.stderr
    assert NO_CONTRIBUTIONS in spaced(run.stdout)
    assert read(home, MODE_FILE) == "confirm\n"
    assert f"@{bundle}/CLAUDE.md\n" in read(home, ".claude/CLAUDE.md")
    assert read(home, ".codex/AGENTS.md").count(automatic_text("CLAUDE.md")) == 1

    for name, variant in VARIANT.items():
        (bundle / variant).write_text(confirm_text(variant), encoding="utf-8")
    refreshed = install(bundle, home, bin_dir)
    assert refreshed.returncode == 0, refreshed.stderr
    assert NO_CONTRIBUTIONS not in spaced(refreshed.stdout)
    assert f"@{bundle}/CLAUDE.confirm.md\n" in read(home, ".claude/CLAUDE.md")


def test_codex_confirm_mode_asks_before_each_contribution_tool(tmp_path: Path) -> None:
    """Codex's own per-tool approval, set to ask, for both tools that send a
    contribution. The rest of the user's configuration is theirs."""
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, ".codex")
    (home / ".codex" / "config.toml").write_text(
        'model = "gpt-5.6"\n\n[mcp_servers.node_repl]\ncommand = "node_repl"\n',
        encoding="utf-8")

    assert install(bundle, home, bin_dir, "confirm").returncode == 0
    assert install(bundle, home, bin_dir, "confirm").returncode == 0

    raw = read(home, ".codex/config.toml")
    config = tomllib.loads(raw)
    assert config["model"] == "gpt-5.6"
    assert config["mcp_servers"]["node_repl"] == {"command": "node_repl"}
    oms = config["mcp_servers"]["oms"]
    assert oms["url"] == MCP_URL
    assert oms["tools"] == {tool: {"approval_mode": "prompt"} for tool in CONTRIBUTION_TOOLS}
    # Twice installed, one of each table: a second copy of any table stops
    # Codex reading its configuration at all.
    assert raw.count("[mcp_servers.oms]") == 1
    for tool in CONTRIBUTION_TOOLS:
        assert raw.count(f"[mcp_servers.oms.tools.{tool}]") == 1


def test_codex_automatic_mode_has_no_per_tool_approval(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, ".codex")

    assert install(bundle, home, bin_dir).returncode == 0
    assert "tools" not in codex_config(home)["mcp_servers"]["oms"]

    # Switching back rewrites the block between its markers, so nothing of
    # confirm mode's approval is left behind.
    assert install(bundle, home, bin_dir, "confirm").returncode == 0
    assert "tools" in codex_config(home)["mcp_servers"]["oms"]
    assert install(bundle, home, bin_dir, "automatic").returncode == 0
    assert "tools" not in codex_config(home)["mcp_servers"]["oms"]
    assert "approval_mode" not in read(home, ".codex/config.toml")


def test_a_users_own_approval_for_a_contribution_tool_is_left_to_decide(tmp_path: Path) -> None:
    """A table the user wrote for one of the tools is theirs. Writing a second
    one would stop Codex reading its configuration at all, so the installer
    writes approval only for the other tool and says which setting decides."""
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, ".codex")
    own = '[mcp_servers.oms.tools.log_correction]\napproval_mode = "approve"\n'
    (home / ".codex" / "config.toml").write_text(own, encoding="utf-8")

    run = install(bundle, home, bin_dir, "confirm")

    assert run.returncode == 0, run.stderr
    raw = read(home, ".codex/config.toml")
    assert raw.startswith(own)
    assert raw.count("[mcp_servers.oms.tools.log_correction]") == 1
    tools = tomllib.loads(raw)["mcp_servers"]["oms"]["tools"]
    assert tools == {"log_correction": {"approval_mode": "approve"},
                     "log_signal": {"approval_mode": "prompt"}}
    assert "[mcp_servers.oms.tools.log_correction]" in run.stdout


def test_a_fragment_reads_the_resolved_mode_and_root_file(tmp_path: Path) -> None:
    """The variables an extension's installer fragments are documented to
    read, set before the first fragment runs."""
    probe = ShellInstallFragments(before_links=(
        'printf \'%s %s\\n\' "$OMS_CONTRIBUTION_MODE_RESOLVED" "$OMS_ROOT_FILE"'
        ' > "$OMS_DIR/fragment-saw"\n'))
    bundle = make_bundle(tmp_path, fragments=probe)
    home, bin_dir = make_machine(tmp_path, ".claude")

    assert install(bundle, home, bin_dir, "confirm").returncode == 0
    assert read(home, ".oms/fragment-saw") == "confirm CLAUDE.confirm.md\n"
    assert install(bundle, home, bin_dir, "automatic").returncode == 0
    assert read(home, ".oms/fragment-saw") == "automatic CLAUDE.md\n"


def test_a_published_bundle_installs_the_instructions_of_the_chosen_mode(tmp_path: Path) -> None:
    """End to end through a real publish, so the names publish writes and the
    names the installer it wrote opens are checked together."""
    graph = InMemoryGraphStore()
    graph.upsert_constraint(Constraint(id="c1", body="Honour the retention policy.",
                                       tenant_id="acme"))
    graph.upsert_skill(Skill(id="checks", name="Checks", description="Review the publication.",
                             domain="engineering", tenant_id="acme"))
    out = tmp_path / "published"
    assert Publisher(graph, contribution_endpoint="https://oms.example/api/ingest",
                     mcp_endpoint=MCP_URL).publish("acme", out).passed
    home, bin_dir = make_machine(tmp_path, ".claude", ".codex")

    confirm = install(out, home, bin_dir, "confirm")
    assert confirm.returncode == 0, confirm.stderr
    assert f"@{out}/CLAUDE.confirm.md\n" in read(home, ".claude/CLAUDE.md")
    assert OFFER in read(home, ".codex/AGENTS.md")

    automatic = install(out, home, bin_dir, "automatic")
    assert automatic.returncode == 0, automatic.stderr
    assert f"@{out}/CLAUDE.md\n" in read(home, ".claude/CLAUDE.md")
    assert OFFER not in read(home, ".codex/AGENTS.md")
    assert "# Contributing learnings" in read(home, ".codex/AGENTS.md")


def last_line(run: subprocess.CompletedProcess[str]) -> str:
    return run.stdout.strip().splitlines()[-1]


def spaced(text: str) -> str:
    """The text as one line of single spaces, so a sentence can be matched
    however the installer wrapped it across echo lines."""
    return " ".join(line.strip() for line in text.splitlines())


ROOT_FOR = {"automatic": "CLAUDE.md", "confirm": "CLAUDE.confirm.md"}
TEXT_FOR = {"automatic": automatic_text, "confirm": confirm_text}
NOT_WRITTEN = ("Windsurf: instructions NOT written.",
               "caps at 6000. The skills above are installed; the organisational "
               "constraints are not. Ask whoever administers OMS to shorten them.")


def windsurf_installed_in(tmp_path: Path, mode: str) -> tuple[Path, Path, Path, Path]:
    """A machine with Claude Code and Windsurf, installed once in `mode` with
    the person's own text in Windsurf's rules file, and the root file of the
    other mode, or of the same one, left for the test to grow past the cap.
    Returns the bundle, HOME, the stand-in commands and the rules file."""
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, ".claude", ".codeium/windsurf")
    # A Codex command on PATH is itself evidence of Codex; this machine has none.
    (bin_dir / "codex").unlink()
    rules = home / ".codeium/windsurf/memories/global_rules.md"
    rules.parent.mkdir(parents=True)
    rules.write_text("My own rules.\n", encoding="utf-8")
    assert install(bundle, home, bin_dir, mode).returncode == 0
    assert TEXT_FOR[mode](ROOT_FOR[mode]) in rules.read_text(encoding="utf-8")
    return bundle, home, bin_dir, rules


def grow_past_the_cap(bundle: Path, mode: str) -> None:
    root = bundle / ROOT_FOR[mode]
    root.write_text(root.read_text(encoding="utf-8") + "x" * 6100 + "\n", encoding="utf-8")


@pytest.mark.parametrize("before, after, recorded", [
    ("automatic", "confirm", True),
    # A machine with no record counts as automatic: installed before modes
    # were recorded, or never configured.
    ("automatic", "confirm", False),
    ("confirm", "automatic", True),
], ids=["automatic-to-confirm", "no-record-to-confirm", "confirm-to-automatic"])
def test_windsurf_over_its_cap_after_a_change_of_mode_has_no_oms_instructions(
        tmp_path: Path, before: str, after: str, recorded: bool) -> None:
    """The confirm copy is longer than the automatic file, so a root file can
    fit Windsurf's cap in one mode and not in the other. The block the other
    mode wrote must not stay behind while the run reports the new mode."""
    bundle, home, bin_dir, rules = windsurf_installed_in(tmp_path, before)
    if not recorded:
        (home / MODE_FILE).unlink()
        (home / INSTALLED_FILE).unlink()
    grow_past_the_cap(bundle, after)

    run = install(bundle, home, bin_dir, after)

    assert run.returncode == 0, run.stderr
    # The person's own text stays; no OMS block of either mode does.
    assert rules.read_text(encoding="utf-8") == "My own rules.\n"
    assert all(line in spaced(run.stdout) for line in NOT_WRITTEN)
    assert f"Windsurf: removed the OMS instructions an earlier run left in {rules}." in spaced(
        run.stderr)
    assert "Not updated: Windsurf. The reasons are above." in run.stderr.splitlines()
    assert last_line(run) == ("Done. Configured: Claude Code. Open any project and the "
                              "skills are live.")
    assert read(home, MODE_FILE) == f"{after}\n"


@pytest.mark.parametrize("mode", ["automatic", "confirm"])
def test_windsurf_over_its_cap_in_an_unchanged_mode_keeps_its_old_block(
        tmp_path: Path, mode: str) -> None:
    """A root file that grows past the cap in the mode the machine already
    had is how the installer has always behaved, and it behaves exactly as
    before: the lines that say what was not written, the block from the
    last run left in place, and the tool still listed as configured."""
    bundle, home, bin_dir, rules = windsurf_installed_in(tmp_path, mode)
    installed = rules.read_text(encoding="utf-8")
    grow_past_the_cap(bundle, mode)

    # The refresh's way: no variable, so the recorded mode applies.
    run = install(bundle, home, bin_dir)

    assert run.returncode == 0, run.stderr
    assert rules.read_text(encoding="utf-8") == installed
    assert all(line in spaced(run.stdout) for line in NOT_WRITTEN)
    assert "removed the OMS instructions" not in run.stderr
    assert "Not updated" not in run.stderr
    assert last_line(run) == ("Done. Configured: Claude Code, Windsurf. Open any project "
                              "and the skills are live.")


def test_windsurf_over_its_cap_on_a_first_install_gets_no_rules_file(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    (bundle / "CLAUDE.confirm.md").write_text("x" * 6100 + "\n", encoding="utf-8")
    home, bin_dir = make_machine(tmp_path, ".codeium/windsurf")
    # Windsurf alone: no other tool's command on PATH, and no Claude Code
    # configuration directory in the environment.
    env = environment(home, bin_dir, "confirm")
    del env["CLAUDE_CONFIG_DIR"]
    for command in ("claude", "codex"):
        (bin_dir / command).unlink()

    run = subprocess.run(["sh", str(bundle / "install.sh")], env=env,
                         capture_output=True, text=True, timeout=60)

    assert run.returncode == 0, run.stderr
    assert not (home / ".codeium/windsurf/memories/global_rules.md").exists()
    assert "Not updated: Windsurf. The reasons are above." in run.stderr.splitlines()
    assert last_line(run) == "Done, with no agent tool fully configured. The reasons are above."


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores the permission bits this sets")
def test_a_tool_skipped_before_writing_is_named_as_not_updated(tmp_path: Path) -> None:
    """A tool whose instructions file cannot be written keeps whatever an
    earlier run left there, which after a change of mode is the other mode's.
    The run cannot fix that, so it says so and does not count the tool."""
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, ".claude", ".codex")
    assert install(bundle, home, bin_dir).returncode == 0
    agents = home / ".codex" / "AGENTS.md"
    agents.chmod(0o444)
    try:
        run = install(bundle, home, bin_dir, "confirm")
    finally:
        agents.chmod(0o644)

    assert run.returncode == 0, run.stderr
    assert "skipped Codex" in run.stderr
    assert "Not updated: Codex. The reasons are above." in run.stderr.splitlines()
    assert last_line(run) == ("Done. Configured: Claude Code. Open any project and the "
                              "skills are live.")


def test_cursor_is_told_to_paste_again_after_a_change_of_mode(tmp_path: Path) -> None:
    """Cursor's rules are pasted by hand, so the installer cannot replace
    them. Pasted once in one mode, they stay that mode until the person
    pastes the new text over them, and only this run can tell them so."""
    once = "Paste this into Cursor Settings > Rules, once, and it applies to every project:"
    again = ("these rules changed with the contribution mode. Paste this into Cursor "
             "Settings > Rules again, replacing the rules you pasted before:")
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, ".cursor")

    first = install(bundle, home, bin_dir, "confirm")
    assert once in spaced(first.stdout) and again not in spaced(first.stdout)

    switched = install(bundle, home, bin_dir, "automatic")
    assert again in spaced(switched.stdout) and once not in spaced(switched.stdout)
    assert read(home, STAGED) == automatic_text("CLAUDE.md")

    unchanged = install(bundle, home, bin_dir)
    assert once in spaced(unchanged.stdout) and again not in spaced(unchanged.stdout)

    # A machine installed before modes were recorded staged automatic rules.
    (home / MODE_FILE).unlink()
    (home / INSTALLED_FILE).unlink()
    opted_in = install(bundle, home, bin_dir, "confirm")
    assert again in spaced(opted_in.stdout)


def test_a_run_that_stopped_part_way_still_sees_the_change_of_mode(tmp_path: Path) -> None:
    """The chosen mode is recorded before any tool is written, so the refresh
    keeps a new choice even when the run that made it stopped part way. What
    the tools were last given is recorded after them, at the end of a run,
    and a change of mode is measured against that: the next run still sees
    the change, and still tells a Cursor user to paste again."""
    again = ("these rules changed with the contribution mode. Paste this into Cursor "
             "Settings > Rules again, replacing the rules you pasted before:")
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, ".cursor")
    assert install(bundle, home, bin_dir, "automatic").returncode == 0
    assert read(home, INSTALLED_FILE) == "automatic\n"

    # A run asked for confirm, recorded the choice and stopped there.
    (home / MODE_FILE).write_text("confirm\n", encoding="utf-8")
    refreshed = install(bundle, home, bin_dir)

    assert refreshed.returncode == 0, refreshed.stderr
    assert again in spaced(refreshed.stdout)
    assert read(home, STAGED) == confirm_text("CLAUDE.confirm.md")
    assert read(home, INSTALLED_FILE) == "confirm\n"
    # The run after a completed one sees no change.
    assert again not in spaced(install(bundle, home, bin_dir).stdout)


def test_a_machine_installed_before_the_record_of_what_it_was_given(tmp_path: Path) -> None:
    """Such a machine was given the mode it chose, so that choice is what a
    change is measured against, as before the second record existed."""
    once = "Paste this into Cursor Settings > Rules, once, and it applies to every project:"
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, ".cursor")
    assert install(bundle, home, bin_dir, "confirm").returncode == 0
    (home / INSTALLED_FILE).unlink()

    unchanged = install(bundle, home, bin_dir)

    assert once in spaced(unchanged.stdout)
    assert read(home, INSTALLED_FILE) == "confirm\n"


def test_codex_with_its_own_oms_table_is_told_confirm_mode_set_no_approval(
        tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    home, bin_dir = make_machine(tmp_path, ".codex")
    own = '[mcp_servers.oms]\nurl = "http://localhost:8000/mcp"\n'
    (home / ".codex" / "config.toml").write_text(own, encoding="utf-8")
    notice = ("Confirm mode set no per-tool approval there, so Codex may not ask before "
              "a contribution unless you configure it.")

    confirm = install(bundle, home, bin_dir, "confirm")
    automatic = install(bundle, home, bin_dir, "automatic")

    assert read(home, ".codex/config.toml") == own
    assert notice in spaced(confirm.stdout)
    assert notice not in spaced(automatic.stdout)


@pytest.mark.parametrize("mcp_url", [MCP_URL, None], ids=["with-mcp", "without-mcp"])
def test_the_generated_shell_is_valid_posix_sh(tmp_path: Path, mcp_url: str | None) -> None:
    script = tmp_path / "install.sh"
    script.write_text(render_install_script(mcp_url), encoding="utf-8")
    for shell in shells():
        checked = subprocess.run([shell, "-n", str(script)], capture_output=True, text=True)
        assert checked.returncode == 0, (shell, checked.stderr)


# -- The PowerShell installer, by what it renders ---------------------------------

def test_the_powershell_installer_chooses_the_same_way() -> None:
    body = render_install_ps1(MCP_URL)
    assert "$env:OMS_CONTRIBUTION_MODE" in body
    assert '$OmsModeFile = "$OMS_DIR/contribution-mode"' in body
    assert MODE_ERROR in body
    assert NO_VARIANT in body
    # Case-sensitive comparisons, like sh: "Confirm" is not a mode.
    assert "-cne 'automatic'" in body and "-cne 'confirm'" in body
    # Every harness reads the variant the mode chose.
    assert "'CLAUDE.confirm.md'" in body
    assert 'Read-TextFile "$SRC/$OmsRootFile"' in body
    assert '"@$SRC/$OmsRootFile`n@$OMS_DIR/status.md"' in body
    # Recorded for the refresh, and said on the terminal.
    assert 'Write-TextFile $OmsModeFile "$OmsContributionMode`n"' in body
    assert 'Write-Host "Contribution mode: $OmsContributionMode"' in body
    # Codex asks before each contribution tool in confirm mode only.
    assert "foreach ($tool in 'log_correction', 'log_signal')" in body
    assert 'approval_mode = `"prompt`"' in body
    assert "@@" not in body


def test_the_powershell_mode_is_settled_before_anything_is_written() -> None:
    body = render_install_ps1(MCP_URL)
    first_write = body.index("New-Item -ItemType Directory -Path $OMS_DIR")
    assert body.index(MODE_ERROR) < first_write
    assert body.index(NO_VARIANT) < first_write
    # On a bundle that takes no contributions, its one file instead.
    assert body.index("$OmsRootFile = 'CLAUDE.md'") < first_write
    assert body.index('Write-Host "This bundle takes no contributions, so both modes install '
                      'the same instructions."') < first_write
    # A fragment runs after the mode is known.
    fragment = "Write-Host 'fragment'"
    with_fragment = render_install_ps1(MCP_URL, fragments=PowerShellInstallFragments(
        before_links=fragment))
    assert with_fragment.index("$OmsContributionMode = ") < with_fragment.index(fragment)


def test_the_powershell_installer_reports_what_it_could_not_update() -> None:
    body = render_install_ps1(MCP_URL)
    write = body[body.index("function Write-Instructions"):body.index("function Set-ManualRules")]
    over_cap = write[:write.index("$kept = Remove-OmsBlock (Read-TextFile $File)")]
    # Over its cap after a change of mode, and only then, Windsurf loses the
    # block the other mode wrote, is told so on stderr and is not counted as
    # configured. In an unchanged mode the path is as it always was.
    changed = over_cap.index("if ($OmsModeBefore -cne $OmsContributionMode) {")
    assert changed < over_cap.index("Remove-OmsBlock $existing")
    assert changed < over_cap.index(
        'Write-Err "  ${Label}: removed the OMS instructions an earlier run left in $File."')
    assert changed < over_cap.index("Add-NotUpdated $Label")
    # A machine with no record counts as automatic, and a change of mode is
    # measured against what the tools were last given, recorded only once
    # every tool step is done.
    chosen = next(line for line in body.splitlines() if line.startswith("$OmsModeChosen = "))
    assert chosen.endswith("else { 'automatic' }")
    before = next(line for line in body.splitlines() if line.startswith("$OmsModeBefore = "))
    assert "Read-TextFile $OmsInstalledModeFile" in before
    assert before.endswith("else { $OmsModeChosen }")
    assert '$OmsInstalledModeFile = "$OMS_DIR/contribution-mode-installed"' in body
    given = body.index('Write-TextFile $OmsInstalledModeFile "$OmsContributionMode`n"')
    assert body.index("# 4. Daily refresh") < given
    assert given < body.index('Write-Host "Contribution mode: $OmsContributionMode"')
    # A tool skipped before any write is named as well.
    assert 'Add-NotUpdated "Windsurf"' in body
    assert "Not updated: " in body
    assert 'Write-Host "Done, with no agent tool fully configured. The reasons are above."' in body
    # Cursor's pasted rules after a change of mode.
    assert "$OmsModeBefore" in body
    assert "file, and these rules changed with the contribution mode. Paste this" in body
    # Codex with its own [mcp_servers.oms] in confirm mode.
    assert ("Confirm mode set no per-tool approval there, so Codex may not ask before "
            "a contribution unless you configure it.") in body
    # TOML keys are case-sensitive, and so is the sh grep.
    assert r'-cmatch "(?m)^\[mcp_servers\.oms\.tools\.$tool\]"' in body
    # A refused mode shows both ways out, confirm first.
    confirm = body.index("`$env:OMS_CONTRIBUTION_MODE = 'confirm'; powershell")
    automatic = body.index("`$env:OMS_CONTRIBUTION_MODE = 'automatic'; powershell")
    assert confirm < automatic


@pytest.mark.parametrize("root_files, variant, other", [
    (None, "CLAUDE.confirm.md", "AGENTS.confirm.md"),
    (["AGENTS.md"], "AGENTS.confirm.md", "CLAUDE.confirm.md"),
])
def test_both_installers_open_the_confirm_copy_of_the_file_they_import(
        root_files: list[str] | None, variant: str, other: str) -> None:
    for body in (render_install_script(MCP_URL, root_files), render_install_ps1(MCP_URL, root_files)):
        assert variant in body
        assert other not in body


def test_both_installers_recognise_a_contribution_block_by_its_heading() -> None:
    """A bundle with no block takes no contributions, so confirm mode installs
    its one file; the installers find the block by the heading it starts
    with, so the heading and the two checks must agree."""
    heading = CONTRIBUTING.splitlines()[1]
    block = "\n".join(_contribution_block("https://oms.example/api/ingest"))
    assert f"\n{heading}\n" in block
    assert f"grep -qxF '{heading}'" in render_install_script(MCP_URL)
    assert f"-ccontains '{heading}'" in render_install_ps1(MCP_URL)


def test_the_readme_describes_both_modes() -> None:
    text = " ".join(render_readme(MCP_URL).split())
    assert ("By default, an agent sends a correction you give it to OMS without asking. "
            "To have it ask first and send only what you approve, run the installer as "
            "`OMS_CONTRIBUTION_MODE=confirm sh install.sh`") in text
    assert "`$env:OMS_CONTRIBUTION_MODE = 'confirm'`" in text
    assert "`OMS_CONTRIBUTION_MODE=automatic`" in text
    assert "`$env:OMS_CONTRIBUTION_MODE = 'automatic'`" in text
    # The one sentence that described automatic mode as the only behaviour.
    assert ("you state one and the agent files it with `log_correction`, asking you "
            "first where the installer ran in confirm mode") in text
    # A bundle with no MCP address takes no contributions, so it offers no mode.
    assert "OMS_CONTRIBUTION_MODE" not in render_readme(None)
