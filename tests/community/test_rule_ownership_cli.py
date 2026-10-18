"""The import command answers a refused second skill with one line and exit code 1.

One rule, one skill: a write that would put a rule in a second skill is
refused by the store and the whole import writes nothing. The web routes give
the person that refusal and its reason. The command line gives it the way it
gives every other refusal, as `Import refused: ...` on stderr with exit code 1,
and never as a traceback."""
from pathlib import Path
from types import SimpleNamespace

from oms.cli import main
from oms.ports.graph_store import RuleOwnershipError
from oms.settings.core import CoreSettings
from oms.sources.service import SourceService
from tests.community.test_github_sources import source_application  # noqa: F401
from tests.community.test_rule_ownership_routes import _reinforce, _skills_of
from tests.community.test_todo_critic import (
    ORIGIN, _files, _import, _seed_skill,
    critic_neo4j_driver, workspace,  # noqa: F401: shared parametrised fixtures
)


def _run_import(monkeypatch, services, root: Path, package: Path) -> int:
    """Run `oms import <package>` against the given services, without a database."""
    import oms.adapters.neo4j.driver as driver_module
    import oms.community.app as composition
    settings = CoreSettings(data_dir=root, tenant_id="acme")
    monkeypatch.setattr(CoreSettings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(driver_module, "build_driver", lambda *args: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(composition, "build_community", lambda *args: services)
    return main(["import", str(package)])


def test_an_import_that_would_give_a_rule_a_second_skill_is_refused_on_the_command_line(
        workspace, monkeypatch, capsys) -> None:
    # Deleting a skill leaves its rules in no skill; a reinforcement puts one
    # in travel; importing the deleted skill's folder again would give the same
    # rule id back to the old skill. Plain import: source reconciliation is off.
    skill_id = _import(workspace)
    store = workspace.services.store
    rule = store.rules_for_skill(skill_id, tenant_id="acme")[0]
    assert workspace.client.delete(f"/api/skills/{skill_id}", headers=ORIGIN).status_code == 200
    _seed_skill(workspace, "travel")
    assert _reinforce(workspace, "moved-rule", rule.id, ["travel"]).status_code == 200
    count = store.get_rule(rule.id).corroboration_count
    package = workspace.root / "input"
    for path, body in _files().items():
        (package / path).parent.mkdir(parents=True, exist_ok=True)
        (package / path).write_bytes(body)

    exit_code = _run_import(monkeypatch, workspace.services, workspace.root, package)

    shown = capsys.readouterr()
    assert exit_code == 1
    assert shown.out == ""
    assert shown.err == (f"Import refused: rule {rule.id} is in travel; one rule belongs to one skill, "
                         f"so it cannot also be in {skill_id}\n")
    assert [row["id"] for row in workspace.client.get("/api/skills").json()] == ["travel"]
    assert _skills_of(workspace, rule.id) == ["travel"]
    assert store.get_rule(rule.id).corroboration_count == count


def test_a_source_import_that_meets_the_refusal_is_refused_on_the_command_line(
        source_application, tmp_path, monkeypatch, capsys) -> None:
    # With source reconciliation on, the command takes the other path. The
    # refusal comes from the service here; the command's answer is under test.
    services, _ = source_application
    reason = "rule rule-1 is in travel; one rule belongs to one skill, so it cannot also be in expenses"

    def refused(*args, **kwargs):
        raise RuleOwnershipError(reason)
    monkeypatch.setattr(SourceService, "submit_local_batch", refused)
    package = tmp_path / "input"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\nname: Expenses\ndescription: Record submitted expenses\n---\n\n## Guidance\n\nKeep original receipts.\n")

    exit_code = _run_import(monkeypatch, services, tmp_path, package)

    shown = capsys.readouterr()
    assert exit_code == 1
    assert (shown.out, shown.err) == ("", f"Import refused: {reason}\n")
