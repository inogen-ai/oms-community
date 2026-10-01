"""Published instructions follow the session-learnings policy and the contribution mode.

The standing contribution block has four variants: automatic or confirm
contribution, each with session learnings on or off. Off removes every
instruction to reflect or to send a self_reflection, and leaves the guidance
for corrections a person gives word for word the same. Confirm mode asks the
person before anything is shared, where automatic mode logs in the same turn.
Confirm copies of the root instruction files are published beside the
automatic ones, under names no harness loads by itself, for an installation
to choose.

Every variant has to fit one byte budget. One harness caps its global rules
file at 6000 bytes and, over the cap, installs no instructions at all, so a
block that grows costs that harness its constraints as well.
"""
import inspect
from pathlib import Path

from pydantic import ValidationError
import pytest

from oms.adapters.memory.store import InMemoryGraphStore
from oms.domain.models import Constraint, Edge, Rule, Skill
from oms.domain.types import EdgeType
from oms.ingestion.contribution import ContributionRequest
from oms.mcp_server.server import TOOL_SCHEMAS
from oms.publish import render
from oms.publish.publisher import Publisher

ENDPOINT = "http://localhost:8000/api/ingest"
# Quoted exactly: the confirm-mode offer and its two choices.
OFFER = ("Should this apply just to this task, or would you like to suggest it "
         "for the team's <skill> guidance?")
CHOICES = ("**Just this time**", "**Review suggestion**")
# Every sentence of the confirm flow that decides what reaches OMS and when.
CONSENT = (
    "When a person gives you a qualifying correction, apply it to the current task, then ask:",
    "Just this time sends nothing to OMS; do not offer it again unless they reopen it.",
    "Review suggestion shows the editable wording, the intended skill, the audience and "
    "any context to be shared; submit only after the person approves that preview.",
    "Silence or cancelling sends nothing.",
    "If they ask to share it, go straight to the preview.",
    # A new UUID, said outright: told only to "give" an id, agents invent
    # natural ones that collide with another person's.
    "Give each approved suggestion a new UUID as its `transaction_id` and reuse it on a "
    "retry; edited wording needs a new preview and a new id.",
    "Approving the tool once is not approval of later suggestions.",
)
LEARNING_APPROVAL = ("Show the person each proposed learning with its context; submit "
                     "only those they approve.")
# Where each way of filing takes the rule. Checked against the schema and the
# helper below, so the sentence cannot drift from what they accept.
WHERE_THE_RULE_GOES = ("the rule in `learning`, not `correction` (the helper takes it as "
                       "`correction`; `log_signal` as `execution_context.learning`)")
CONTEXT_FIELDS = ("session_summary", "project_name", "reuse_case", "learning_evidence")
ROOT_FILES = ("AGENTS.md", "CLAUDE.md")
# The confirm copies' published names, spelled out: an installer choosing
# confirm mode opens these exact files.
VARIANTS = ("AGENTS.confirm.md", "CLAUDE.confirm.md")
# The largest root file in real use leaves the block 3676 bytes under the
# 6000-byte cap; this keeps a margin for the skills list above it to grow.
BUDGET = 3660


def block(*, session_learnings: bool = True, mode: str = "automatic") -> str:
    """The block as `render_claude_md` joins it into a root file."""
    return "\n".join(render._contribution_block(
        ENDPOINT, session_learnings=session_learnings, mode=mode))


def store(graph: InMemoryGraphStore | None = None) -> InMemoryGraphStore:
    """One tenant with a constraint and a skill, so a root file has a head."""
    graph = graph if graph is not None else InMemoryGraphStore()
    graph.upsert_constraint(Constraint(id="c1", body="Honour the retention policy.", tenant_id="acme"))
    graph.upsert_skill(Skill(id="checks", name="Checks", description="Review the publication.",
                             domain="engineering", tenant_id="acme"))
    graph.upsert_rule(Rule(id="check-rule", body="Check the result before publishing.",
                           tenant_id="acme"))
    graph.attach_edge(Edge(type=EdgeType.BELONGS_TO, from_id="check-rule", to_id="checks"))
    return graph


class ConstraintsChangingMidPublish(InMemoryGraphStore):
    """Every read returns a different constraint, as if one were edited while
    a publish ran."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def active_constraints(self, tenant_id: str) -> list[Constraint]:
        self.reads += 1
        return [Constraint(id=f"c{self.reads}", body=f"Constraint as read {self.reads}.",
                           tenant_id=tenant_id)]


def published(root: Path, *names: str) -> dict[str, str]:
    return {name: (root / name).read_text(encoding="utf-8") for name in names}


@pytest.mark.parametrize("mode", ["automatic", "confirm"])
def test_off_omits_every_reflection_instruction(mode):
    off = block(session_learnings=False, mode=mode)
    for phrase in ("## Session learnings", "self_reflection", "Before reporting work finished"):
        assert phrase not in off
    # The correction guidance is intact: Off removes one section and changes
    # nothing above it.
    assert block(mode=mode).startswith(off + "\n\n## Session learnings\n")
    for required in ("durable organisational or repository guidance", "Zero contributions is normal",
                     "MEMORY.md", "`log_correction`", "`contribute_learning(", f"POST {ENDPOINT}",
                     "`query_skill`"):
        assert required in off
    # Scoped to what a person gives. Unscoped, "useful evidenced lessons"
    # would invite the agent's own lessons in as corrections, which a policy
    # refusing self_reflection never sees.
    assert "guidance a person gives you" in off


@pytest.mark.parametrize("mode", ["automatic", "confirm"])
def test_on_carries_the_five_future_use_tests_and_the_context_fields(mode):
    on = block(mode=mode)
    for test in ("recur", "changed action", "observation", "stays true", "adds to the skills"):
        assert test in on
    assert "Normally there is nothing to submit" in on
    assert "At most three" in on
    for field in CONTEXT_FIELDS:
        assert f"`{field}`" in on
    for shape in ('`signal_type: "self_reflection"`', '`source_ref: "session:<your session id>"`'):
        assert shape in on


@pytest.mark.parametrize("mode", ["automatic", "confirm"])
def test_each_way_of_filing_a_learning_is_told_where_the_rule_goes(mode):
    assert WHERE_THE_RULE_GOES in block(mode=mode)
    # What the sentence describes. The thin body (MCP log_correction and
    # HTTP) takes the rule as `learning` and refuses it with a correction
    # beside it; the helper has no `learning` keyword and maps its first
    # argument; log_signal nests the rule.
    ContributionRequest(learning="In this repo, run the linter first.", signal_type="self_reflection")
    with pytest.raises(ValidationError, match="not correction"):
        ContributionRequest(learning="In this repo, run the linter first.",
                            correction="In this repo, run the linter first.",
                            signal_type="self_reflection")
    helper: dict = {}
    exec(compile(render.render_contribute_tool(ENDPOINT), "oms_contribute.py", "exec"), helper)
    parameters = list(inspect.signature(helper["contribute_learning"]).parameters)
    assert parameters[0] == "correction" and "learning" not in parameters


@pytest.mark.parametrize("session_learnings", [True, False])
def test_confirm_mode_replaces_immediate_logging(session_learnings):
    confirm = block(session_learnings=session_learnings, mode="confirm")
    assert f'"{OFFER}"' in confirm
    for choice in CHOICES:
        assert choice in confirm
    for sentence in CONSENT:
        assert sentence in confirm
    assert "in the same turn" not in confirm
    assert "approve it once" not in confirm
    # A proposed learning is shown before it is shared, and only when
    # learnings are asked for at all.
    assert (LEARNING_APPROVAL in confirm) is session_learnings
    automatic = block(session_learnings=session_learnings)
    assert "in the same turn" in automatic and "approve it once" in automatic
    assert OFFER not in automatic and "Review suggestion" not in automatic


def test_every_variant_fits_the_budget():
    for mode in render.CONTRIBUTION_MODES:
        for session_learnings in (True, False):
            size = len(block(session_learnings=session_learnings, mode=mode).encode("utf-8"))
            assert size <= BUDGET, f"{mode}, learnings {session_learnings}: {size} bytes"


def test_an_unknown_mode_is_refused():
    # A misspelt mode must not quietly publish automatic instructions to an
    # installation that asked for confirmation.
    with pytest.raises(ValueError, match="contribution mode"):
        render._contribution_block(ENDPOINT, mode="confirmed")
    with pytest.raises(ValueError, match="contribution mode"):
        render.render_claude_md([], [], contribution_mode="confirmed")


@pytest.mark.parametrize("name, variant", [
    ("AGENTS.md", "AGENTS.confirm.md"), ("CLAUDE.md", "CLAUDE.confirm.md"),
    ("GEMINI.txt", "GEMINI.confirm.txt"), ("rules.v2.md", "rules.v2.confirm.md"),
    ("X", "X.confirm"), (".cursorrules", ".cursorrules.confirm"),
])
def test_a_confirm_copy_is_named_beside_its_root_file(name, variant):
    assert render.confirm_variant_name(name) == variant


@pytest.mark.parametrize("names", [["AGENTS.md", "AGENTS.confirm.md"], ["X", "X.confirm"]])
def test_a_root_file_cannot_be_named_as_another_ones_confirm_copy(names):
    # Two configured names on one file: publish would write one over the
    # other, and an installer choosing a mode would open the wrong one.
    with pytest.raises(ValueError, match="confirm-mode copy"):
        Publisher(InMemoryGraphStore(), contribution_endpoint=ENDPOINT,
                  root_instruction_files=names)


def test_publish_writes_confirm_variants_beside_the_automatic_files(tmp_path):
    graph = store()
    out = tmp_path / "published"
    assert Publisher(graph, contribution_endpoint=ENDPOINT).publish("acme", out).passed
    automatic = published(out, *ROOT_FILES)
    confirm = published(out, *VARIANTS)
    for name, variant in zip(ROOT_FILES, VARIANTS):
        assert "# Contributing learnings" in automatic[name]
        assert OFFER not in automatic[name]
        assert OFFER in confirm[variant]
        # The same constraints and skill index: only the block differs.
        head = automatic[name].partition("\n# Contributing learnings")[0]
        assert confirm[variant].partition("\n# Contributing learnings")[0] == head
    # Siblings, never a same-named file in a subdirectory, which a harness
    # could load as nested instructions beside the automatic file.
    assert not (out / "confirm").exists()
    assert set(VARIANTS).isdisjoint(ROOT_FILES)
    # Installers never copy the Cursor and Windsurf project rules globally,
    # so those stay automatic.
    for rel_path, _ in render.ROOT_RULE_FORMATS.values():
        assert OFFER not in (out / rel_path).read_text(encoding="utf-8")

    # A variant follows the configured root file names and is withdrawn
    # with them.
    Publisher(graph, contribution_endpoint=ENDPOINT,
              root_instruction_files=["AGENTS.md"]).publish("acme", out)
    assert (out / "AGENTS.confirm.md").is_file()
    assert not (out / "CLAUDE.confirm.md").exists()
    # Withdrawn with the endpoint too: a stale copy would carry a dead
    # address to every machine that installed it.
    Publisher(graph).publish("acme", out)
    assert not (out / "AGENTS.confirm.md").exists()

    # With no endpoint, neither file carries a block and no variant is written.
    closed = tmp_path / "closed"
    Publisher(store()).publish("acme", closed)
    for body in published(closed, *ROOT_FILES).values():
        assert "# Contributing learnings" not in body
    assert not any((closed / variant).exists() for variant in VARIANTS)


def test_one_publish_states_one_set_of_constraints(tmp_path):
    # Read once and rendered into every file. A second read could see an
    # edit and give the automatic and confirm files different heads in one
    # publication.
    out = tmp_path / "published"
    Publisher(store(ConstraintsChangingMidPublish()),
              contribution_endpoint=ENDPOINT).publish("acme", out)
    heads = {name: body.partition("\n# Available Skills")[0]
             for name, body in published(out, *ROOT_FILES, *VARIANTS).items()}
    heads["tier2/AGENTS.md"] = published(out, "tier2/AGENTS.md")["tier2/AGENTS.md"].partition(
        "\n# Fetching Skills")[0]
    assert len(set(heads.values())) == 1, heads


def test_session_learnings_off_reaches_every_published_root_file(tmp_path):
    rule_files = [rel_path for rel_path, _ in render.ROOT_RULE_FORMATS.values()]
    names = [*ROOT_FILES, *VARIANTS, *rule_files]
    on, off = tmp_path / "on", tmp_path / "off"
    Publisher(store(), contribution_endpoint=ENDPOINT).publish("acme", on)
    Publisher(store(), contribution_endpoint=ENDPOINT, session_learnings=False).publish("acme", off)
    for name, body in published(off, *names).items():
        assert "# Contributing learnings" in body, name
        assert "## Session learnings" not in body, name
        assert "self_reflection" not in body, name
    # The same publish with the default policy writes the section everywhere.
    for name, body in published(on, *names).items():
        assert "## Session learnings" in body, name


def test_the_core_tool_descriptions_do_not_command_reflection():
    for tool in TOOL_SCHEMAS:
        description = tool["description"]
        for command in ("SESSION LEARNINGS", "same turn", "When you finish a piece of work"):
            assert command not in description, tool["name"]
        # When and whether to submit belongs to the loaded instructions,
        # which differ by installation; one shared server cannot know which.
        assert ("Follow your OMS instructions for when to submit; some installations "
                "ask the person to confirm first.") in description, tool["name"]
