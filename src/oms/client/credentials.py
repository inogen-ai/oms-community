"""Ambient credential lookup (identity spec §6).

Standard library only, and deliberately duplicated into the generated shim
rather than imported by it: the published bundle cannot depend on OMS being
installed. `render_contribute_tool` inlines this function's body, and a test
asserts the two agree, which is what stops the transports drifting apart.

Never raises. A missing or unreadable credential means contribute unbound
(§6 step 4), and an exception here would stop an agent contributing at all.
"""
from pathlib import Path

CREDENTIALS_PATH = ".oms/credentials"


def resolve_credential(env: dict | None = None, home: Path | None = None) -> str | None:
    """Ambient credential lookup, in precedence order:

    1. OMS_TOKEN            explicit override: CI, servers, containers
    2. ~/.oms/credentials   the enrolled human (0600), the normal case
    3. None                 contribute unbound; held for review

    Workload identity (CI OIDC, cloud metadata) is step 3 in the spec and is
    not implemented here; it slots in before the None return when a deployment
    needs it.
    """
    import os

    try:
        environ = os.environ if env is None else env
        token = (environ.get("OMS_TOKEN") or "").strip()
        if token:
            return token

        root = Path.home() if home is None else Path(home)
        return (root / CREDENTIALS_PATH).read_text(encoding="utf-8").strip() or None
    except Exception:
        return None
