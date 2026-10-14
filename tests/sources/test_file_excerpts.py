import json

from oms.sources.api import file_response
from oms.sources.models import CheckFinding


def preview(body, side="upstream", lines=(), path="big.js"):
    return json.loads(file_response(path, side, body, preview=True, excerpt_lines=lines).body)


def numbered(count, width=0):
    return "\n".join(f"line {n}" + "x" * width for n in range(1, count + 1)).encode()


def test_flagged_lines_beyond_the_preview_get_merged_and_separate_excerpts():
    body = numbered(4000)
    result = preview(body, lines=[1286, 1300, 3267])
    texts = body.decode().split("\n")
    assert [(e["start_line"], len(e["lines"])) for e in result["excerpts"]] == [(1266, 55), (3247, 41)]
    for excerpt in result["excerpts"]:
        start = excerpt["start_line"]
        assert excerpt["lines"] == texts[start - 1:start - 1 + len(excerpt["lines"])]
        assert excerpt["cut_lines"] == []
    assert result["excerpts"][0]["lines"][0] == "line 1266" and result["excerpts"][0]["lines"][-1] == "line 1320"


def test_touching_windows_merge_and_a_gap_keeps_them_apart():
    touching = preview(numbered(4000), lines=[1100, 1141])
    assert [e["start_line"] for e in touching["excerpts"]] == [1080]
    apart = preview(numbered(4000), lines=[1100, 1142])
    assert [e["start_line"] for e in apart["excerpts"]] == [1080, 1122]


def test_a_flagged_line_inside_the_preview_gets_no_excerpt_and_one_beyond_it_does():
    inside_only = preview(numbered(4000), lines=[500])
    assert inside_only["excerpts"] == []
    both = preview(numbered(4000), lines=[500, 2000])
    assert [e["start_line"] for e in both["excerpts"]] == [1980]
    assert "line 500" in both["text"]


def test_a_line_the_preview_shows_only_in_part_is_not_held():
    body = numbered(200, width=1000)
    result = preview(body, lines=[10])
    held = result["text"].count("\n")
    assert result["truncated"] and held < 200
    result = preview(body, lines=[held + 1])
    assert result["excerpts"] and result["excerpts"][0]["start_line"] <= held + 1


def test_at_most_twenty_excerpts_are_kept_by_start_line():
    body = numbered(4000)
    result = preview(body, lines=[1100 + 100 * n for n in range(25)])
    assert len(result["excerpts"]) == 20
    starts = [e["start_line"] for e in result["excerpts"]]
    assert starts == sorted(starts) and starts[0] == 1080


def test_total_excerpt_lines_are_bounded_and_the_first_flagged_lines_are_covered():
    flagged = list(range(1100, 10000, 40))
    result = preview(numbered(10000), lines=flagged)
    covered = {n for e in result["excerpts"] for n in range(e["start_line"], e["start_line"] + len(e["lines"]))}
    assert 0 < sum(len(e["lines"]) for e in result["excerpts"]) <= 820
    assert len(result["excerpts"]) <= 20
    assert all(n in covered for n in flagged[:20])
    assert flagged[-1] not in covered


def test_a_window_never_repeats_lines_the_preview_holds():
    body = numbered(4000)
    result = preview(body, lines=[1005])
    held = result["text"].count("\n")
    assert result["excerpts"][0]["start_line"] == held + 1 == 1001


def test_a_long_line_is_cut_and_listed():
    rows = numbered(2000).decode().split("\n")
    rows[1499] = "y" * 5000
    result = preview("\n".join(rows).encode(), lines=[1500])
    excerpt = result["excerpts"][0]
    assert excerpt["lines"][1500 - excerpt["start_line"]] == "y" * 2000
    assert excerpt["cut_lines"] == [1500]


def test_line_numbers_count_only_line_feeds():
    rows = ["line %d" % n for n in range(1, 1300)]
    rows[1199] = "a\r"             # a CRLF ending is one break
    rows[1249] = "c\rd"            # a lone CR is no break
    body = "\n".join(rows).encode()
    result = preview(body, lines=[1250])
    text = body.decode().split("\n")
    excerpt = result["excerpts"][0]
    assert excerpt["lines"] == text[excerpt["start_line"] - 1:excerpt["start_line"] - 1 + len(excerpt["lines"])]
    assert "c\rd" in excerpt["lines"]
    assert excerpt["lines"][1250 - excerpt["start_line"]] == "c\rd"


def test_an_excerpt_line_of_a_crlf_file_has_no_carriage_return():
    # The main preview drops a CRLF line's carriage return, so the excerpts do too.
    body = "\r\n".join(f"line {n}" for n in range(1, 1300)).encode()
    excerpt = preview(body, lines=[1250])["excerpts"][0]
    assert excerpt["lines"] == [f"line {n}" for n in range(excerpt["start_line"], excerpt["start_line"] + 41)]
    # Cut at 2,000 characters after the carriage return is gone: a line of exactly 2,000 is whole.
    rows = [f"line {n}" for n in range(1, 1300)]
    rows[1249] = "z" * 2000
    excerpt = preview("\r\n".join(rows).encode(), lines=[1250])["excerpts"][0]
    assert excerpt["lines"][1250 - excerpt["start_line"]] == "z" * 2000 and excerpt["cut_lines"] == []


def test_other_sides_binary_files_and_downloads_have_no_excerpts():
    body = numbered(4000)
    for side in ("base", "local", "discovery"):
        assert preview(body, side=side, lines=[2000])["excerpts"] == []
    assert preview(b"\x00" + body, lines=[2000])["excerpts"] == []
    assert preview(body, lines=[])["excerpts"] == []
    assert preview(numbered(900), lines=[800])["excerpts"] == []


def test_the_update_route_passes_the_updates_own_lines_for_that_path(source_world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from oms.sources.api import source_routes
    from oms.sources.reads import SourceReads

    world = source_world
    app = FastAPI()
    reads = SourceReads(world.service, world.blobs)
    app.include_router(source_routes(world.service, reads, lambda request, action: world.context).router)
    client = TestClient(app)
    update = world.open_update().update
    body = numbered(4000)
    findings = (CheckFinding(path="SKILL.md", line=1286, category="email"),
                CheckFinding(path="other.md", line=3267, category="email"),
                CheckFinding(path="SKILL.md", category="email"))
    generation = update.generation + 1
    plan = update.plan.model_copy(update={"check_findings": findings, "fingerprint":
        update.plan.fingerprint.model_copy(update={"update_generation": generation})})
    reads.update_file = lambda ctx, update_id, side, path: body
    world.sources.put_update(update.model_copy(update={"plan": plan, "generation": generation}))
    route = f"/api/skill-updates/{update.update_id}/files"
    upstream = client.get(route, params={"side": "upstream", "path": "SKILL.md"}).json()
    assert [e["start_line"] for e in upstream["excerpts"]] == [1266]
    base = client.get(route, params={"side": "base", "path": "SKILL.md"}).json()
    assert base["excerpts"] == []
