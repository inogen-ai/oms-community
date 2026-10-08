import test from "node:test";
import assert from "node:assert/strict";
import { sourceUpdateScope } from "@inogen/oms-ui-core";

const card = (update_id, kind) => ({ update_id, plan: { origin: { kind } } });
const open = [card("g1", "github"), card("l1", "local"), card("g2", "github")];

test("an origin filter keeps only that kind of update, and no filter keeps all", () => {
  assert.deepEqual(sourceUpdateScope(open, { origin: "github" }).shown.map(row => row.update_id), ["g1", "g2"]);
  assert.deepEqual(sourceUpdateScope(open, { origin: "local" }).shown.map(row => row.update_id), ["l1"]);
  assert.equal(sourceUpdateScope(open).shown.length, 3);
});

test("each list has its own intro and empty state; the unfiltered list has no intro", () => {
  const github = sourceUpdateScope([], { origin: "github" }), local = sourceUpdateScope([], { origin: "local" });
  assert.equal(github.intro, "After you check a repository, changes it made to your skills appear here for a decision.");
  assert.equal(local.intro, "Changes found when you re-imported a package appear here for a decision.");
  assert.equal(sourceUpdateScope([]).intro, null);
  assert.ok(!github.empty.includes("local package")); assert.ok(local.empty.includes("local package"));
  assert.ok(sourceUpdateScope([]).empty.includes("tracked source") && sourceUpdateScope([]).empty.includes("local package"));
});

test("a selected update that is open in the other list is reported, not hidden or shown twice", () => {
  assert.equal(sourceUpdateScope(open, { origin: "github", selectedId: "l1" }).elsewhere.update_id, "l1");
  assert.equal(sourceUpdateScope(open, { origin: "github", selectedId: "g1" }).elsewhere, undefined);
  assert.equal(sourceUpdateScope(open, { selectedId: "l1" }).elsewhere, undefined);
  assert.equal(sourceUpdateScope(open, { origin: "github", selectedId: "gone" }).elsewhere, undefined);
});
