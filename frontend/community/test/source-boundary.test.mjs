import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
function files(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => entry.isDirectory() ? files(resolve(directory, entry.name)) : [resolve(directory, entry.name)]);
}

test("Community source imports only its own app, framework and public packages", () => {
  const source = ["app", "components", "lib"].flatMap((dir) => files(resolve(root, dir)));
  const imported = [];
  for (const path of source.filter((path) => /\.[jt]sx?$/.test(path))) {
    const text = readFileSync(path, "utf8");
    for (const match of text.matchAll(/(?:from\s+|import\s*\()(["'])([^"']+)\1/g)) imported.push([path, match[2]]);
  }
  assert.ok(imported.length > 20, "source fence inspected the real application");
  for (const [path, module] of imported) assert.ok(module.startsWith("./") || module.startsWith("@/") || ["react", "next", "next/link", "next/navigation", "@inogen/oms-client", "@inogen/oms-ui-core", "@inogen/oms-ui-core/graph", "@inogen/oms-ui-core/history", "@inogen/oms-ui-core/mobile-navigation", "@inogen/oms-ui-core/assets/inogen_logo_darkmode.png", "@inogen/oms-ui-core/assets/oms_logo_darkthemev2.png", "lucide-react"].includes(module), `${path} imports ${module}`);
});

test("public packages pack only declared source and documentation", () => {
  const release = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8")).version;
  for (const name of ["client", "ui-core"]) {
    const manifest = JSON.parse(readFileSync(resolve(root, "../packages", name, "package.json"), "utf8"));
    assert.equal(manifest.version, release);
    assert.deepEqual(manifest.files, ["src", "README.md", "LICENSE", "NOTICE"]);
    assert.equal(manifest.license, "SUL-1.0");
    assert.equal(manifest.private, undefined);
  }
});

test("browser fixture rejects a controlled non-Community endpoint mutation", async () => {
  const { allowedRequest } = await import("../e2e/route-contract.mjs");
  assert.equal(allowedRequest("GET", "/api/skills"), true);
  assert.equal(allowedRequest("POST", "/api/review/correction-1/decision"), true);
  assert.equal(allowedRequest("POST", "/api/import-review/change-1/decision"), true);
  assert.equal(allowedRequest("PATCH", "/api/constraints/constraint-1"), true);
  assert.equal(allowedRequest("DELETE", "/api/skills/example"), true);
  assert.equal(allowedRequest("DELETE", "/api/skills"), false);
  assert.equal(allowedRequest("POST", "/api/cypher"), false);
  assert.equal(allowedRequest("GET", "/api/people"), false);
  assert.equal(allowedRequest("GET", "/api/skills/example/mutes"), false);
  assert.equal(allowedRequest("POST", "/api/skills/example/consistency-scan"), false);
});

test("ui-core exports no path to the GitHub logo files; only the component binds them", () => {
  const manifest = JSON.parse(readFileSync(resolve(root, "../packages/ui-core/package.json"), "utf8"));
  assert.doesNotMatch(JSON.stringify(manifest.exports), /Invertocat/i);
  // GitHub's official files, byte for byte (hashes from the logo pack).
  const hashes = { White: "ccd84c89b1056345608fc3489357f8acc7397e49a3cdc2d418b6c8016911d47b", Black: "693d7abe6f899646cc2e96856723b45e95f71885a54910b2749f6decdf7e1ee1" };
  for (const [colour, hash] of Object.entries(hashes))
    assert.equal(createHash("sha256").update(readFileSync(resolve(root, `../packages/ui-core/src/assets/GitHub_Invertocat_${colour}.svg`))).digest("hex"), hash);
});
