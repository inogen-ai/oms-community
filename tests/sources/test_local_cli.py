from types import SimpleNamespace

import pytest

from oms.sources.errors import SourceConflict
from oms.sources.local_import import import_local_path
from tests.sources.test_local_upload import uploads_for


def test_cli_requires_explicit_existing_target_and_shares_the_local_stream(source_world, tmp_path):
    world = source_world
    path = tmp_path / "input"
    path.mkdir()
    document = path / "SKILL.md"
    document.write_bytes(world.blobs.get(world.package("a", "Initial CLI content").manifest[0].blob_ref))
    importer = uploads_for(world, tmp_path)._importer
    first = import_local_path(world.service, importer, world.context, path)
    assert first.state == "complete"
    origin = world.sources.get_local_stream(world.skill).origin
    document.write_bytes(world.blobs.get(world.package("b", "Edited CLI content").manifest[0].blob_ref))
    refused = import_local_path(world.service, importer, world.context, path)
    assert refused.state == "failed" and world.description() == "Initial CLI content"
    result = import_local_path(world.service, importer, world.context, document, targets=("expenses",))
    assert result.state == "complete" and world.description() == "Edited CLI content"
    assert world.sources.get_local_stream(world.skill).origin == origin
    with pytest.raises(SourceConflict, match="source reconciliation required"):
        importer.import_directory(path, "acme")


def test_public_cli_reports_noninteractive_refusal_as_unsuccessful(source_world, tmp_path, monkeypatch, capsys):
    from oms.cli import main
    from oms.settings.core import CoreSettings
    import oms.adapters.neo4j.driver as driver_module
    import oms.community.app as composition
    world = source_world
    world.install()
    path = tmp_path / "input"
    path.mkdir()
    (path / "SKILL.md").write_bytes(world.blobs.get(world.package("c", "Incoming local text").manifest[0].blob_ref))
    importer = uploads_for(world, tmp_path)._importer
    world.policy.context = lambda: world.context
    settings = CoreSettings(data_dir=tmp_path, tenant_id="acme")
    monkeypatch.setattr(CoreSettings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(driver_module, "build_driver", lambda *args: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(composition, "build_community", lambda *args: SimpleNamespace(sources=world.service, importer=importer))
    assert main(["import", str(path), "--target", "expenses"]) == 1
    assert '"state": "awaiting_review"' in capsys.readouterr().out
    assert world.description() == "First description"
