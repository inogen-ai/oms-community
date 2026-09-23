import test from "node:test";
import assert from "node:assert/strict";
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
  for (const name of ["client", "ui-core"]) {
    const manifest = JSON.parse(readFileSync(resolve(root, "../packages", name, "package.json"), "utf8"));
    assert.equal(manifest.version, "1.2.0");
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
