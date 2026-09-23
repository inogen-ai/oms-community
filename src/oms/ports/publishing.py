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
        "    # An unsuccessful contribution exits non-zero.",
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
