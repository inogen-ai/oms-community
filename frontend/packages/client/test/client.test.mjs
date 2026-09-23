import test from "node:test";
import assert from "node:assert/strict";
import { createClient, communityNavigation, HttpError, parseCapabilities } from "../src/index.js";

const capabilities = {
  edition: "community", api_contract_version: "1.1", schema_version: 1,
  manual_learning: true, semantic_compilation: false, multi_user_identity: false,
  team_scoping: false, personal_mutes: false, contributor_portal: false,
  redaction_vault: false, model_settings: false, scheduled_publish: false,
  managed_publish: false, graph_query_console: false, advanced_review: false, usage_analytics: false,
};

test("capabilities validate current and previous API minor versions", () => {
  for (const version of ["1.0", "1.1"]) assert.equal(parseCapabilities({ ...capabilities, api_contract_version: version }).manual_learning, true);
  for (const changed of [{ api_contract_version: "2.0" }, { schema_version: "1" }, { manual_learning: "true" }, { edition: "unknown" }]) {
    assert.throws(() => parseCapabilities({ ...capabilities, ...changed }));
  }
  const missing = { ...capabilities }; delete missing.manual_learning;
  assert.throws(() => parseCapabilities(missing));
});

test("manual inbox navigation follows the constructed capability", () => {
  assert.equal(communityNavigation(capabilities).length, 8);
  assert.ok(!communityNavigation({ ...capabilities, manual_learning: false }).some((item) => item.key === "inbox"));
});

test("transport reports structured and FastAPI errors including empty HTTP reason phrases", async () => {
  for (const [body, expected] of [[{ error: { message: "Review already decided", code: "conflict" } }, "Review already decided"], [{ detail: "Skill no longer exists" }, "Skill no longer exists"], [{}, "GET /api/skills failed with HTTP 409"]]) {
    const client = createClient({ fetch: async () => new Response(JSON.stringify(body), { status: 409 }) });
    await assert.rejects(client.skills(), (error) => error instanceof HttpError && error.status === 409 && error.message === expected);
  }
});

test("browser request policy preserves caller headers and defaults with late-bound fetch", async () => {
  const original = globalThis.fetch;
  const client = createClient({ baseUrl: "https://api.example.test", requestPolicy: "browser" });
  const requests = [];
  try {
    globalThis.fetch = async (url, init) => { requests.push({ url, init }); return Response.json({}); };
    await client.request("/api/health");
    const init = { method: "POST", headers: { Authorization: "Bearer explicit-fixture" }, body: "{}" };
    await client.request("/api/skills", init);
    assert.equal(requests[0].init, undefined);
    assert.equal(requests[1].init, init);
    assert.equal(requests[1].url, "https://api.example.test/api/skills");
  } finally { globalThis.fetch = original; }
});

test("transport never forwards cookies or follows API redirects", async () => {
  let seen;
  const client = createClient({ baseUrl: "http://127.0.0.1:4317/", fetch: async (url, init) => { seen = { url, init }; return Response.json([]); } });
  await client.skills();
  assert.equal(seen.url, "http://127.0.0.1:4317/api/skills");
  assert.equal(seen.init.credentials, "omit");
  assert.equal(seen.init.redirect, "error");
  assert.equal(seen.init.cache, "no-store");
  for (const baseUrl of ["file:///tmp/a", "https://secret:token@example.test", "https://example.test?token=secret", "https://example.test/#x"]) assert.throws(() => createClient({ baseUrl }));
  await assert.rejects(client.request("https://elsewhere.example.test"));
});

test("manual decisions and section saves carry explicit JSON without tenant guessing", async () => {
  const requests = [];
  const client = createClient({ fetch: async (url, init) => { requests.push({ url, init }); return Response.json({}); } });
  await client.decide("transaction 1", { action: "reinforce", body: "Keep receipts", skill_ids: ["expenses"], rule_id: "rule-1" });
  await client.updateSection("expense review", "section/intro", "Preserve this paragraph.");
  assert.equal(requests[0].url, "/api/review/transaction%201/decision");
  assert.deepEqual(JSON.parse(requests[0].init.body), { action: "reinforce", body: "Keep receipts", skill_ids: ["expenses"], rule_id: "rule-1" });
  assert.equal(requests[1].url, "/api/skills/expense%20review/sections/section%2Fintro");
  assert.equal(requests[1].init.method, "PUT");
  assert.deepEqual(JSON.parse(requests[1].init.body), { text: "Preserve this paragraph." });
});

test("ZIP import uses multipart boundaries supplied by the browser", async () => {
  let request;
  const client = createClient({ fetch: async (url, init) => { request = { url, init }; return Response.json({ skills: 1 }); } });
  await client.importArchive(new File(["fixture"], "skills.zip", { type: "application/zip" }));
  assert.equal(request.url, "/api/import");
  assert.ok(request.init.body instanceof FormData);
  assert.equal(request.init.body.get("file").name, "skills.zip");
  assert.equal(request.init.headers, undefined);
});

test("skill deletion is scoped to the encoded skill and keeps the local request policy", async () => {
  let seen;
  const report = { deleted: "expense review", name: "Expense review", rules_detached: 2 };
  const client = createClient({ baseUrl: "http://127.0.0.1:4317", fetch: async (url, init) => { seen = { url, init }; return Response.json(report); } });
  assert.deepEqual(await client.deleteSkill("expense review"), report);
  assert.equal(seen.url, "http://127.0.0.1:4317/api/skills/expense%20review");
  assert.equal(seen.init.method, "DELETE");
  assert.equal(seen.init.body, undefined);
  assert.equal(seen.init.credentials, "omit");
  assert.equal(seen.init.redirect, "error");
});
