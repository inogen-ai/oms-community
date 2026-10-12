from functools import wraps
import json
from time import perf_counter

from oms.domain.models import Skill
from oms.sources.reads import SourceReads


def test_workspace_metadata_does_not_load_skill_contents(source_world, monkeypatch, tmp_path):
    world = source_world
    for index in range(40):
        world.store.upsert_skill(Skill(id=f"library-{index}", name=f"Library {index}",
            domain="finance", description="Existing local content", tenant_id="acme"))
    original = type(world.store).sections_for_skill
    calls = 0

    @wraps(original)
    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)
    monkeypatch.setattr(type(world.store), "sections_for_skill", counted)
    reads = SourceReads(world.service, world.blobs)
    durations, identities = [], []
    for _ in range(3):
        start = perf_counter()
        identities.append(reads.workspace(world.context))
        durations.append(perf_counter() - start)
    (tmp_path / "metadata-query-cost.json").write_text(json.dumps({
        "adapter": type(world.store).__name__, "skills": 40, "content_queries": calls,
        "seconds": durations}, indent=2))
    assert len(set(identities)) == 1
    assert calls == 0, "Workspace identity must not load unrelated skill sections"
