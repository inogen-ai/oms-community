/** Public transport only. No installation secrets or edition-specific endpoints. */
export class HttpError extends Error {
  constructor(message, status, code = "request_failed") {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.code = code;
  }
}

const capabilityNames = [
  "manual_learning", "semantic_compilation", "multi_user_identity", "team_scoping",
  "personal_mutes", "contributor_portal", "redaction_vault", "model_settings",
  "scheduled_publish", "managed_publish", "graph_query_console", "advanced_review",
  "usage_analytics",
];

export function parseCapabilities(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)
      || !["community", "pro", "enterprise"].includes(value.edition)
      || !["1.0", "1.1"].includes(value.api_contract_version)
      || value.schema_version !== 1) {
    throw new Error("This server uses an unsupported OMS API contract. Install matching application and API versions.");
  }
  const parsed = {
    edition: value.edition, api_contract_version: value.api_contract_version,
    schema_version: value.schema_version,
  };
  for (const name of capabilityNames) {
    if (typeof value[name] !== "boolean") {
      throw new Error(`The server capability document is incomplete: ${name}.`);
    }
    parsed[name] = value[name];
  }
  return Object.freeze(parsed);
}

export const communityNavigation = (capabilities) => [
  { href: "/", label: "Overview", key: "dashboard" },
  { href: "/skills", label: "Skills", key: "skills" },
  ...(capabilities.manual_learning ? [{ href: "/inbox", label: "Correction inbox", key: "inbox" }] : []),
  { href: "/rules", label: "Constraints", key: "rules" },
  { href: "/import", label: "Import", key: "import" },
  { href: "/publish", label: "Preview & publish", key: "publish" },
  { href: "/graph", label: "Graph", key: "graph" },
  { href: "/settings", label: "Settings", key: "settings" },
];

export function createClient({ baseUrl = "", fetch: fetcher, requestPolicy = "local" } = {}) {
  if (!["local", "browser"].includes(requestPolicy)) throw new Error("Unknown OMS request policy.");
  const base = baseUrl.replace(/\/+$/, "");
  if (base) {
    const url = new URL(base);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      throw new Error("The OMS API URL must be HTTP or HTTPS without credentials, query or fragment.");
    }
  }
  async function request(path, init) {
    // Callers cannot turn this shared transport into an arbitrary URL fetch.
    if (!path.startsWith("/api/") || path.startsWith("//") || path.includes("\\")) {
      throw new Error("An OMS API request must use an absolute /api/ path.");
    }
    // Local mode never uses ambient browser credentials. Other applications
    // can explicitly retain the browser's request defaults and their own init.
    const options = requestPolicy === "local"
      ? { ...init, credentials: "omit", redirect: "error", cache: "no-store" }
      : init;
    const response = await (fetcher ?? globalThis.fetch)(`${base}${path}`, options);
    if (!response.ok) {
      let error;
      try { error = await response.json(); } catch { /* A proxy may return text. */ }
      const detail = typeof error?.detail === "string" ? error.detail : null;
      throw new HttpError(
        error?.error?.message || detail || response.statusText
          || `${init?.method || "GET"} ${base}${path} failed with HTTP ${response.status}`,
        response.status, error?.error?.code || "request_failed",
      );
    }
    return response.status === 204 ? undefined : response.json();
  }
  const id = (value) => encodeURIComponent(value);
  const send = (path, method, body) => request(path, {
    method, ...(body === undefined ? {} : {
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),
  });
  return {
    request,
    capabilities: async () => parseCapabilities(await request("/api/capabilities")),
    health: () => request("/api/health"),
    contribute: (body) => send("/api/ingest", "POST", body),
    skills: () => request("/api/skills"),
    recentSkillChanges: () => request("/api/skill-changes"),
    createSkill: (body) => send("/api/skills", "POST", body),
    skill: (skillId) => request(`/api/skills/${id(skillId)}`),
    updateSkill: (skillId, body) => send(`/api/skills/${id(skillId)}`, "PATCH", body),
    deleteSkill: (skillId) => send(`/api/skills/${id(skillId)}`, "DELETE"),
    document: (skillId) => request(`/api/skills/${id(skillId)}/document`),
    saveDocument: (skillId, body) => send(`/api/skills/${id(skillId)}/document`, "PUT", body),
    versions: (skillId) => request(`/api/skills/${id(skillId)}/versions`),
    compareVersion: (skillId, versionId) => request(`/api/skills/${id(skillId)}/versions/${id(versionId)}/compare`),
    restoreVersion: (skillId, versionId, body) => send(`/api/skills/${id(skillId)}/versions/${id(versionId)}/restore`, "POST", body),
    file: (skillId, path) => request(`/api/skills/${id(skillId)}/files?path=${id(path)}`),
    updateSection: (skillId, sectionId, text) => send(`/api/skills/${id(skillId)}/sections/${id(sectionId)}`, "PUT", { text }),
    review: () => request("/api/review"),
    decide: (transactionId, body) => send(`/api/review/${id(transactionId)}/decision`, "POST", body),
    rules: () => request("/api/rules"),
    updateRule: (ruleId, body) => send(`/api/rules/${id(ruleId)}`, "PATCH", body),
    constraints: () => request("/api/constraints"),
    createConstraint: (body) => send("/api/constraints", "POST", { body }),
    importArchive: (file) => {
      const form = new FormData();
      form.append("file", file);
      return request("/api/import", { method: "POST", body: form });
    },
    importDirectory: (files) => {
      const form = new FormData();
      for (const file of files) form.append("files", file, file.webkitRelativePath || file.name);
      return request("/api/import/directory", { method: "POST", body: form });
    },
    preview: (skillId) => request(`/api/publish/preview?skill_id=${id(skillId)}`),
    publish: () => send("/api/publish", "POST"),
    graph: (query = "", skillId) => request(`/api/graph?q=${id(query)}${skillId ? `&skill_id=${id(skillId)}` : ""}`),
    node: (nodeId) => request(`/api/graph/nodes/${id(nodeId)}`),
    neighbours: (nodeId, offset = 0) => request(`/api/graph/nodes/${id(nodeId)}/neighbours?offset=${offset}`),
    settings: () => request("/api/settings"),
    updateSettings: (body) => send("/api/settings", "PATCH", body),
  };
}
