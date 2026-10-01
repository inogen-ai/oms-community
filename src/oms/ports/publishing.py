"""Explicit rendering seams used by publication composition roots.

Fragments are trusted build-time source supplied by a renderer implementation,
never request input. The shared renderer owns the installer and contribution
transport; extensions fill only the named lifecycle positions below.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ShellInstallFragments:
    """Trusted sh placed at named positions of the POSIX installer.

    Every fragment but `refresh_success` runs in the installer after its
    preconditions have settled the installation's contribution mode, and may
    read two variables they set:

    - `OMS_CONTRIBUTION_MODE_RESOLVED`: `automatic` or `confirm`, already
      validated. From OMS_CONTRIBUTION_MODE when it is set and not empty,
      else the choice the machine recorded in `$OMS_DIR/contribution-mode`,
      else `automatic`.
    - `OMS_ROOT_FILE`: the name, inside the bundle, of the root instruction
      file for that mode: the file the installer imports, or its confirm copy
      (`confirm_variant_name`). It is known to exist.

    `refresh_success` is different: it is text inside the refresh script the
    installer writes, not code the installer runs. There, an unescaped
    reference to either variable is fixed when the installer writes the
    script, and an escaped one is unbound when the script runs under `set -u`.
    It needs neither: the refresh re-runs the installer, which settles the
    mode again.

    `root_source` defaults to `$SRC/$OMS_ROOT_FILE`. A fragment that replaces
    it must give the same mode's instructions, or the harnesses installed in
    one run would disagree about whether to ask before sharing.
    """

    before_links: str = ""
    root_source: str | None = None
    link_initialiser: str = ""
    skip_skill: str = ""
    link_report: str = '  echo "  $ls_label: linked $ls_n skills into $ls_dir"'
    refresh_success: str = '    : > "\\$STATUS"'
    credential_setup: str | None = None
    credential_notice: str = ""


@dataclass(frozen=True)
class PowerShellInstallFragments:
    """Trusted PowerShell placed at named positions of the Windows installer.

    The same contract as `ShellInstallFragments`, under PowerShell names:
    `$OmsContributionMode` holds `automatic` or `confirm` and `$OmsRootFile`
    the bundle's root instruction file for that mode. `root_source` defaults
    to `$SRC/$OmsRootFile` and `root_import` to `@$SRC/$OmsRootFile`. The
    same exception applies: `refresh_success` is text inside the refresh
    script's here-string, where an unescaped reference is fixed when the
    installer writes it and an escaped one is undefined when it runs.
    """

    before_links: str = ""
    root_source: str | None = None
    root_import: str | None = None
    link_initialiser: str = ""
    skip_skill: str = ""
    link_report: str = '    Write-Host "  ${Label}: linked $n skills into $SkillsDir"'
    refresh_success: str = '        [System.IO.File]::WriteAllText(`$status, "", `$utf8)'
    credential_setup: str | None = None
    credential_notice: str = ""


@dataclass(frozen=True)
class ContributionFragments:
    description: tuple[str, ...] = ()
    request_parameter: str = ""
    request_documentation: tuple[str, ...] = (
        '    """POST JSON using the current local credential, when configured."""',
    )
    credential_expression: str | None = None
    helpers: tuple[str, ...] = ()
    cli_documentation: tuple[str, ...] = (
        "    # CLI: python oms_contribute.py '<correction>' ['<skill_hint>']",
        "    #                               [--signal-type <type>] [--source-ref <ref>]",
        "    #                               [--session-summary <text>] [--project-name <name>]",
        "    #                               [--reuse-case <text>] [--evidence <text>]",
        "    #                               [--transaction-id <id>]",
        "    # A contribution that is refused or fails exits non-zero.",
    )
    cli_dispatch: tuple[str, ...] = ()
    usage_lines: tuple[str, ...] = ()


@runtime_checkable
class PublishingRenderer(Protocol):
    """Render root support files without owning skill custody or publication."""

    def install_script(self, mcp_url: str | None, *, root_files: Sequence[str],
                       bundle_token: str | None) -> str: ...

    def install_ps1(self, mcp_url: str | None, *, root_files: Sequence[str],
                    bundle_token: str | None) -> str: ...

    def contribute_tool(self, endpoint: str, *, bundle_token: str | None) -> str: ...

    def readme(self, mcp_url: str | None, *, distribution_repo: str | None) -> str: ...
