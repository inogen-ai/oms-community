"""Where each agent harness reads its global skills and instructions.

Every path here was verified against the harness's own documentation on
2026-08-07, and none was inferred from a sibling's layout: the five agree on
almost nothing. Skills live under `~/.claude`, `~/.agents`, `~/.codeium/
windsurf`, `~/.gemini/config` and `~/.cursor` respectively; instructions are a
`CLAUDE.md`, an `AGENTS.md`, a `memories/global_rules.md`, another `AGENTS.md`
in a different directory, and - for Cursor - no file at all. A guessed path
produces a file nothing reads, which is the failure this installer keeps
having to be rescued from: it looks exactly like success.

The `$HOME` and `$CLAUDE_DIR` prefixes are shell variable references, expanded
by the generated installer rather than here. Keeping them symbolic is what lets
CLAUDE_CONFIG_DIR keep overriding the Claude row, and what lets the installer's
tests point an entire run at a temporary directory.
"""
from dataclasses import dataclass
from typing import Literal

# How a harness takes the organisational instructions.
#   import - a reference to the bundle file; the harness resolves it at load
#            time, so the daily pull updates the content with no rewrite here.
#            Claude Code's `@path` syntax, and nothing else supports it.
#   copy   - the content itself, between markers. Refreshed by the daily
#            re-run of the installer rather than by the harness.
#   manual - the harness has no global instructions file. The installer stages
#            the text and tells the human where to paste it.
Style = Literal["import", "copy", "manual"]


@dataclass(frozen=True)
class Harness:
    key: str
    label: str
    # Existence of this directory means the tool is installed, and it is also
    # the root everything for this harness is written under. The installer
    # never creates it speculatively: a directory conjured for an absent tool
    # is litter, and every later run would read it back as evidence the tool
    # is there.
    detect: str
    # A command whose presence on PATH also proves the tool is installed.
    # Needed because the config directory is created by the tool's first run,
    # not by its installation: on a machine where Claude Code has been
    # installed but never opened, directory-existence alone would skip the
    # primary target. Only declared where the command name is known; a guess
    # here silently disables a harness or enables it on the wrong machine.
    cli: str | None
    skills_dir: str | None
    instructions: str | None
    style: Style
    # Printed after a skip reason: what this particular tool's user can do
    # about it, which differs enough between them to be worth carrying here.
    hint: str | None = None
    # Hard limit on the instructions file, where the harness imposes one.
    max_chars: int | None = None
    # An environment variable whose being set is itself evidence the tool is
    # installed and where it lives. CLAUDE_CONFIG_DIR is exported by Claude
    # Code sessions and honoured ahead of $HOME, so a value pointing at a
    # directory that cannot be created is a real way to arrive here - and
    # reporting that as "no agent tool found" sends somebody looking for
    # software they have already installed.
    detect_env: str | None = None
    # A file this harness reads in preference to `instructions`. Where one
    # exists on the machine, writing `instructions` is a no-op the installer
    # cannot detect by looking at its own work, so it says so instead of
    # reporting a success that did not happen.
    overridden_by: str | None = None
    # Other harnesses' skills directories that this one also reads. Used to
    # avoid stacking a redundant copy in front of a tool that is already
    # served by a compatibility path.
    also_reads: tuple[str, ...] = ()
    # The harness's global MCP configuration, which is what puts the
    # log_correction tool in front of it. None where the harness is configured
    # another way (Claude Code has a CLI for it).
    mcp_config: str | None = None
    # "json" or "toml". They need different writers, and neither may replace
    # the file: it holds servers the user configured.
    mcp_format: str | None = None
    # The key this harness's JSON uses for a remote server's address. Cursor
    # takes `url`; Windsurf and Antigravity take `serverUrl` and document that
    # `url` is not supported, so one shape for all three leaves two of them
    # ignoring the entry in silence.
    mcp_url_key: str | None = None


HARNESSES: tuple[Harness, ...] = (
    Harness(
        # Detected at $CLAUDE_DIR, not $HOME/.claude: CLAUDE_CONFIG_DIR moves
        # the whole directory, and testing one path while writing to another
        # would detect a machine that is not the one being written to.
        key="claude", label="Claude Code", detect="$CLAUDE_DIR", cli="claude",
        skills_dir="$CLAUDE_DIR/skills",
        instructions="$CLAUDE_DIR/CLAUDE.md", style="import",
        detect_env="CLAUDE_CONFIG_DIR",
        hint="Set CLAUDE_CONFIG_DIR to install elsewhere.",
    ),
    Harness(
        key="codex", label="Codex", detect="$HOME/.codex", cli="codex",
        # The documented user-level path is the shared one. ~/.codex/skills
        # exists too but holds Codex's own bundled skills under .system.
        skills_dir="$HOME/.agents/skills",
        # Codex prefers AGENTS.override.md when it exists and reads only the
        # first non-empty file at this level, so writing here can be a no-op.
        # The installer says so rather than reporting a success it cannot see.
        instructions="$HOME/.codex/AGENTS.md", style="copy",
        overridden_by="$HOME/.codex/AGENTS.override.md",
        mcp_config="$HOME/.codex/config.toml", mcp_format="toml",
    ),
    Harness(
        key="windsurf", label="Windsurf", detect="$HOME/.codeium/windsurf",
        cli=None,
        skills_dir="$HOME/.codeium/windsurf/skills",
        instructions="$HOME/.codeium/windsurf/memories/global_rules.md",
        style="copy", max_chars=6000,
        mcp_config="$HOME/.codeium/windsurf/mcp_config.json",
        mcp_format="json", mcp_url_key="serverUrl",
    ),
    Harness(
        key="antigravity", label="Antigravity", detect="$HOME/.gemini",
        cli=None,
        skills_dir="$HOME/.gemini/config/skills",
        # AGENTS.md rather than the GEMINI.md alongside it: GEMINI.md wins on
        # conflict and Gemini CLI writes the same file, so the two tools
        # overwrite each other there.
        instructions="$HOME/.gemini/AGENTS.md", style="copy",
        # Shared by the 2.0 IDE, the CLI and the SDK.
        mcp_config="$HOME/.gemini/config/mcp_config.json",
        mcp_format="json", mcp_url_key="serverUrl",
    ),
    Harness(
        key="cursor", label="Cursor", detect="$HOME/.cursor", cli=None,
        skills_dir="$HOME/.cursor/skills",
        instructions=None, style="manual",
        also_reads=("$CLAUDE_DIR/skills", "$HOME/.agents/skills"),
        mcp_config="$HOME/.cursor/mcp.json",
        mcp_format="json", mcp_url_key="url",
    ),
)


def by_key(key: str) -> Harness:
    for h in HARNESSES:
        if h.key == key:
            return h
    raise KeyError(key)
