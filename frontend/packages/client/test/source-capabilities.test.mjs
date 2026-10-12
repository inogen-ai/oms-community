import test from "node:test";
import assert from "node:assert/strict";
import { parseCapabilities } from "../src/index.js";
const base = {
  edition: "community", manual_learning: true, semantic_compilation: false,
  multi_user_identity: false, team_scoping: false, personal_mutes: false,
  contributor_portal: false, redaction_vault: false, model_settings: false,
  scheduled_publish: false, managed_publish: false, graph_query_console: false,
  advanced_review: false, usage_analytics: false,
};
test("only supported legacy contracts may omit the source capability", () => {
  for (const api_contract_version of ["1.0", "1.1"]) {
    assert.equal(parseCapabilities({ ...base, api_contract_version, schema_version: 1 }).github_skill_sources, false);
  }
  for (const github_skill_sources of [true, false]) {
    const parsed = parseCapabilities({ ...base, api_contract_version: "1.2", schema_version: 2, github_skill_sources });
    assert.equal(parsed.github_skill_sources, github_skill_sources);
    assert.ok(Object.isFrozen(parsed));
  }
});
test("future versions, mixed schema versions and malformed flags fail closed", () => {
  for (const [api_contract_version, schema_version] of [["1.2", 1], ["1.1", 2], ["1.3", 2], ["2.0", 2], ["1.2", "2"]]) {
    assert.throws(() => parseCapabilities({ ...base, api_contract_version, schema_version, github_skill_sources: true }));
  }
  for (const github_skill_sources of [undefined, null, 1, "true"]) {
    assert.throws(() => parseCapabilities({ ...base, api_contract_version: "1.2", schema_version: 2, github_skill_sources }));
  }
  assert.throws(() => parseCapabilities({ ...base, api_contract_version: "1.1", schema_version: 1, github_skill_sources: "false" }));
});
