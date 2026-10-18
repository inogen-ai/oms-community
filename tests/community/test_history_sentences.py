"""Every cause a skill version can carry reads as a sentence in the history view.

The console words a version's cause from the `CAUSES` table in
`frontend/packages/ui-core/src/history.js`. A cause missing from that table
falls back to its own name with the underscores taken out ("Rule split"),
which reads as a code and not as something that happened. This keeps the
enum and the table in step, so a new cause cannot ship without its sentence."""
import re
from pathlib import Path

from oms.domain.types import SkillVersionCause

HISTORY = Path(__file__).resolve().parents[2] / "frontend" / "packages" / "ui-core" / "src" / "history.js"


def _sentences() -> dict[str, str]:
    """The `CAUSES` table of history.js, read as text: each cause and its sentence."""
    table = re.search(r"const CAUSES = \{(.*?)\n\};", HISTORY.read_text(encoding="utf-8"), re.S)
    assert table is not None, "history.js no longer declares a CAUSES table; update this test"
    return dict(re.findall(r'\b([a-z_]+): "([^"]+)"', table.group(1)))


def test_the_table_is_read_whole() -> None:
    # Guards the reader above: an older cause must be found, or an empty
    # result would make the next test fail for the wrong reason.
    sentences = _sentences()
    assert sentences["source_update"] == "applied a source update"
    assert sentences["created"] == "created this skill"


def test_every_version_cause_has_a_history_sentence() -> None:
    sentences = _sentences()
    missing = sorted(cause.value for cause in SkillVersionCause if cause.value not in sentences)
    assert missing == [], f"history.js has no sentence for: {', '.join(missing)}"
