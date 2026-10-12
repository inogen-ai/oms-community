from pathlib import Path

import pytest

from tests.sources.test_publication_echo import published_case


@pytest.mark.parametrize("local_change", [None, "bytes", "mode"])
def test_normal_publish_removes_only_unchanged_owned_stale_files(tmp_path, local_change):
    services, output = published_case(tmp_path)
    skill = services.store.skills_for_tenant("acme")[0]
    relative = "references/check.txt"
    path = output / "skills" / skill.id / relative
    if local_change == "bytes":
        path.write_bytes(b"local correction")
    elif local_change == "mode":
        path.chmod(0o755)
    services.store.remove_artefact(skill.id, relative, tenant_id="acme")
    services.publisher.publish("acme", output)
    assert path.exists() == (local_change is not None)
    if local_change == "bytes":
        assert path.read_bytes() == b"local correction"
    elif local_change == "mode":
        assert path.stat().st_mode & 0o111


def test_each_destination_keeps_its_own_removal_proof_on_retry(tmp_path):
    services, first = published_case(tmp_path)
    second = tmp_path / "second-destination"
    services.publisher.publish("acme", second, skills_only=True)
    skill = services.store.skills_for_tenant("acme")[0]
    relative = f"skills/{skill.id}/references/check.txt"
    services.store.remove_artefact(skill.id, "references/check.txt", tenant_id="acme")
    services.publisher.publish("acme", first)
    assert not (first / relative).exists() and (second / relative).exists()
    # A failed remote push can restore the last committed generated file.
    (first / relative).parent.mkdir(parents=True, exist_ok=True)
    (first / relative).write_bytes(b"first")
    services.publisher.publish("acme", first)
    assert not (first / relative).exists()
    services.publisher.publish("acme", second, skills_only=True)
    assert not (second / relative).exists()


def test_blob_read_failure_does_not_prune_a_still_owned_file(tmp_path, monkeypatch):
    services, output = published_case(tmp_path)
    skill = services.store.skills_for_tenant("acme")[0]
    target = output / "skills" / skill.id / "references/check.txt"

    def unavailable(reference):
        raise OSError("Custody read failed")
    monkeypatch.setattr(services.publisher._blob_store, "get", unavailable)
    with pytest.raises(OSError):
        services.publisher.publish("acme", output)
    assert target.read_bytes() == b"first"
    assert (output / ".oms-publishing").exists()


def test_retired_skill_with_locally_changed_file_mode_is_preserved(tmp_path):
    services, output = published_case(tmp_path)
    skill = services.store.skills_for_tenant("acme")[0]
    target = output / "skills" / skill.id / "references/check.txt"
    target.chmod(0o755)
    services.store.delete_skill(skill.id, tenant_id="acme")
    services.publisher.publish("acme", output)
    assert target.exists() and target.stat().st_mode & 0o111


def test_another_destination_cannot_adopt_a_local_mode_change(tmp_path):
    services, first = published_case(tmp_path)
    skill = services.store.skills_for_tenant("acme")[0]
    relative = "references/check.txt"
    target = first / "skills" / skill.id / relative
    target.chmod(0o755)
    artefact, _ = services.store.artefacts_for_skill(skill.id, tenant_id="acme")[0]
    artefact.mode = 0o100755
    services.store.upsert_artefact(artefact, skill.id, relative, tenant_id="acme")
    services.publisher.publish("acme", tmp_path / "second", skills_only=True)
    services.store.remove_artefact(skill.id, relative, tenant_id="acme")
    services.publisher.publish("acme", first)
    services.publisher.publish("acme", first)
    assert target.exists() and target.stat().st_mode & 0o111


def test_journal_recovery_cannot_combine_a_new_hash_with_an_old_mode(tmp_path):
    from hashlib import sha256
    import json
    services, output = published_case(tmp_path)
    skill = services.store.skills_for_tenant("acme")[0]
    relative = f"skills/{skill.id}/references/check.txt"
    target = output / relative
    services.store.remove_artefact(skill.id, "references/check.txt", tenant_id="acme")
    target.write_bytes(b"interrupted new bytes")
    target.chmod(0o644)
    temporary = target.with_name(".check.txt.oms-" + "a" * 32)
    (output / ".oms-writes.jsonl").write_text(json.dumps({"path": relative,
        "sha256": sha256(target.read_bytes()).hexdigest(), "mode": 0o100755,
        "temporary": temporary.relative_to(output).as_posix()}) + "\n")
    services.publisher.publish("acme", output)
    services.publisher.publish("acme", output)
    assert target.read_bytes() == b"interrupted new bytes"


def test_cleanup_uses_byte_proof_when_file_modes_are_not_supported(tmp_path, monkeypatch):
    from oms.publish import manifest
    monkeypatch.setattr(manifest, "supports_file_modes", lambda: False)
    services, output = published_case(tmp_path)
    skill = services.store.skills_for_tenant("acme")[0]
    target = output / "skills" / skill.id / "references/check.txt"
    services.store.remove_artefact(skill.id, "references/check.txt", tenant_id="acme")
    services.publisher.publish("acme", output)
    assert not target.exists()


def test_removing_one_occurrence_preserves_another_tenant(mutation_case, tmp_path):
    from oms.domain.models import Artefact, Skill
    from oms.domain.types import ArtefactKind
    store = mutation_case.store
    store.upsert_skill(Skill(id="expenses", name="Other", description="Other", domain="finance", tenant_id="other"))
    for tenant in ("acme", "other"):
        store.upsert_artefact(Artefact(id="same-digest", content_ref="sha256-" + "a" * 64,
            kind=ArtefactKind.OTHER, name="a.txt", size=1, tenant_id=tenant, source_ref="a.txt", mode=0o100644),
            "expenses", "a.txt", tenant_id=tenant)
    store.remove_artefact("expenses", "a.txt", tenant_id="acme")
    assert store.artefacts_for_skill("expenses", tenant_id="acme") == []
    assert len(store.artefacts_for_skill("expenses", tenant_id="other")) == 1


def _legacy_ownership(output):
    import json
    path = output / ".oms-ownership.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({ref: record["sha256"] for ref, record in records.items()}), encoding="utf-8")


@pytest.mark.parametrize("local_change", [None, "bytes"])
def test_legacy_hash_only_ownership_proves_a_stale_file_by_bytes(tmp_path, local_change):
    import json
    services, output = published_case(tmp_path)
    skill = services.store.skills_for_tenant("acme")[0]
    relative = f"skills/{skill.id}/references/check.txt"
    _legacy_ownership(output)
    if local_change == "bytes":
        (output / relative).write_bytes(b"local correction")
    services.store.remove_artefact(skill.id, "references/check.txt", tenant_id="acme")
    services.publisher.publish("acme", output)
    assert (output / relative).exists() == (local_change is not None)
    records = json.loads((output / ".oms-ownership.json").read_text(encoding="utf-8"))
    assert records[f"skills/{skill.id}/SKILL.md"]["mode"] == 0o100644
    if local_change is None:
        assert records[relative]["mode"] == 0o100644
        services.publisher.publish("acme", output)
        assert relative not in json.loads((output / ".oms-ownership.json").read_text(encoding="utf-8"))
    else:
        assert (output / relative).read_bytes() == b"local correction"


def test_legacy_hash_only_ownership_retires_a_superseded_mcp_config(tmp_path, monkeypatch):
    services, output = published_case(tmp_path)
    monkeypatch.setattr(services.publisher, "_mcp_endpoint", "https://oms.example/mcp")
    monkeypatch.setattr(services.publisher, "_bundle_token", "superseded-token")
    services.publisher.publish("acme", output)
    assert (output / ".mcp.json").is_file()
    _legacy_ownership(output)
    monkeypatch.setattr(services.publisher, "_mcp_endpoint", None)
    services.publisher.publish("acme", output)
    assert not (output / ".mcp.json").exists() and not (output / ".cursor" / "mcp.json").exists()


@pytest.mark.parametrize("ledger_mode", ["recorded", "legacy"])
@pytest.mark.parametrize("local_change", [None, "bytes", "mode"])
def test_database_ledger_proves_a_stale_file_with_its_recorded_mode(tmp_path, ledger_mode, local_change):
    from dataclasses import replace
    services, output = published_case(tmp_path)
    skill = services.store.skills_for_tenant("acme")[0]
    relative = f"skills/{skill.id}/references/check.txt"
    (output / ".oms-ownership.json").unlink()
    if ledger_mode == "legacy":
        publication = services.store.get_publication("acme", relative)
        services.store.upsert_publication(replace(publication, mode=None))
    if local_change == "bytes":
        (output / relative).write_bytes(b"local correction")
    elif local_change == "mode":
        (output / relative).chmod(0o755)
    services.store.remove_artefact(skill.id, "references/check.txt", tenant_id="acme")
    services.publisher.publish("acme", output)
    kept = local_change == "bytes" or (local_change == "mode" and ledger_mode == "recorded")
    assert (output / relative).exists() == kept
