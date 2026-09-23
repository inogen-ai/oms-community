"""Disposable real HTTP fixture: installed core wheel, memory graph, file blobs."""
import json
import os
import shlex
from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

import oms
import uvicorn
from oms.community.app import build_memory
from oms.web.api import create_app

origin = Path(oms.__file__).resolve()
if "site-packages" not in origin.parts:
    raise RuntimeError("The browser test must use an installed core wheel, without source-path injection")

root = Path(os.environ["OMS_BROWSER_TEST_DATA"]).resolve()
root.mkdir(parents=True, exist_ok=True)
(root / "installed-core.json").write_text(json.dumps({"origin": str(origin)}))
reference = b"# Expense policy\r\n\r\nKeep the original receipt bytes.\r\n"
script = f"#!/bin/sh\nprintf 'unexpected execution' > {shlex.quote(str(root / 'script-must-not-run'))}\n".encode()
document = """---
name: Expense Review
description: Review expense claims carefully.
---

# Expense Review

Keep clear records for every expense claim.

## Process

Use the expense review checklist for each claim.

## Instructions

* Keep original receipts with each claim.

## References

See [the policy](references/policy.md).
"""
for filename, text in (("skills.zip", document), ("skills-updated.zip", document.replace(
        "Use the expense review checklist for each claim.", "Use the full expense review checklist for each claim."))):
    with ZipFile(root / filename, "w") as archive:
        archive.writestr("expense-review/SKILL.md", text)
        archive.writestr("expense-review/references/policy.md", reference)
        archive.writestr("expense-review/scripts/check.sh", script)
(root / "reference.original").write_bytes(reference)
(root / "script.original").write_bytes(script)

services = build_memory(root / "state", tenant="acme")
port = int(os.environ.get("OMS_COMMUNITY_API_PORT", "4317"))
ui_port = int(os.environ.get("OMS_COMMUNITY_UI_PORT", "4318"))
services = replace(services, settings=replace(services.settings, port=port,
    allowed_origins=(f"http://127.0.0.1:{ui_port}", f"http://localhost:{ui_port}")))
uvicorn.run(create_app(services), host="127.0.0.1", port=port, log_level="warning")
