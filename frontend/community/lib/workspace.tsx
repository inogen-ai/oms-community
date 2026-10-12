"use client";
import { createContext, useContext, useEffect, useState, useCallback, type DependencyList, type ReactNode } from "react";
import { createClient, createSourcesClient, type Capabilities, type CommunityClient } from "@inogen/oms-client";
import { Notice } from "@inogen/oms-ui-core";
import { createCommunitySources, communitySourceContext, type CommunitySources } from "./sources";

const Context = createContext<{ api: CommunityClient; capabilities: Capabilities; sources: CommunitySources | null; sourceRevision: number; refreshSources: () => void; sourceWorkspaceNotice: string } | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [sources, setSources] = useState<CommunitySources | null>(null);
  const [sourceWorkspaceNotice, setSourceWorkspaceNotice] = useState("");
  const [sourceRevision, setSourceRevision] = useState(0);
  const refreshSources = useCallback(() => setSourceRevision(value => value + 1), []);
  const [api, setApi] = useState<CommunityClient | null>(null);
  const [apiUrl, setApiUrl] = useState("");
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    let sourceService: CommunitySources | null = null;
    let generation = 0;
    async function open() {
      const current = ++generation;
      sourceService?.client.invalidate(); sourceService?.requests.setScope(null); sourceService?.requests.close(); sourceService = null;
      setApi(null); setCapabilities(null); setSources(null); setError("");
      const response = await fetch("/oms-config.json", { cache: "no-store", credentials: "omit", redirect: "error" });
      if (!response.ok) throw new Error("The local application configuration could not be loaded.");
      const config = await response.json();
      if ("tenant_id" in config || "tenant" in config) throw new Error("Community configuration cannot override the local workspace.");
      if (typeof config.api_url !== "string") throw new Error("The local application configuration needs an api_url string.");
      const baseUrl = config.api_url || process.env.NEXT_PUBLIC_OMS_API_URL || "";
      const client = createClient({ baseUrl });
      if (active) setApiUrl(baseUrl);
      const value = await client.capabilities();
      if (value.edition !== "community") throw new Error("Connect this application to an OMS Community server.");
      if (!active || current !== generation) return;
      const workspace = value.github_skill_sources
        ? await createSourcesClient({ ...client, context: communitySourceContext }).workspace() : null;
      if (active && current === generation) {
        sourceService = value.github_skill_sources ? createCommunitySources(client, baseUrl, workspace?.workspace_id, () => {
          if (!active || current !== generation) return;
          setSourceWorkspaceNotice("The workspace changed. Earlier source requests and views were reset; no earlier mutation was retried. Review this workspace before sending another request.");
          void open().catch(failure => { if (active) setError(message(failure)); });
        }) : null;
        setSources(sourceService); setApi(client); setCapabilities(value);
      }
    }
    void open().catch((failure) => { if (active) setError(message(failure)); });
    return () => { active = false; sourceService?.client.invalidate(); sourceService?.requests.close(); };
  }, []);
  if (error) return <main className="startup"><h1>Connect to your workspace</h1><Notice kind="error">{error}</Notice><p>Check that your local Community API is running {apiUrl ? <>at <code>{apiUrl}</code></> : "at this application’s address"} and permits this application’s origin.</p><button onClick={() => window.location.reload()}>Try again</button></main>;
  if (!capabilities || !api) return <main className="startup" role="status">Opening your local workspace…</main>;
  return <Context.Provider key={sources?.scope ?? "community"} value={{ api, capabilities, sources, sourceRevision, refreshSources, sourceWorkspaceNotice }}>{children}</Context.Provider>;
}

export function useWorkspace() {
  const value = useContext(Context);
  if (!value) throw new Error("Workspace has not been loaded.");
  return value;
}

export const message = (error: unknown) => error instanceof Error ? error.message : "The request could not be completed.";

export function useResource<T>(load: () => Promise<T>, dependencies: DependencyList) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    load().then((value) => { if (active) setData(value); })
      .catch((failure) => { if (active) setError(message(failure)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  // The caller explicitly supplies the identity of the resource.
  }, [...dependencies, revision]);
  return { data, error, loading, setData, refresh: () => setRevision((value) => value + 1) };
}

export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  async function run(job: () => Promise<unknown>, text: string, after?: () => void) {
    setBusy(true); setError(""); setSuccess("");
    try { await job(); setSuccess(text); after?.(); return true; }
    catch (failure) { setError(message(failure)); return false; }
    finally { setBusy(false); }
  }
  return { busy, error, success, run };
}

export function Feedback({ action }: { action: ReturnType<typeof useAction> }) {
  return <>{action.error && <Notice kind="error">{action.error}</Notice>}{action.success && <Notice kind="success">{action.success}</Notice>}</>;
}

export function ResourceStatus({ resource }: { resource: { loading: boolean; error: string } }) {
  return <>{resource.loading && <p role="status">Loading…</p>}{resource.error && <Notice kind="error">{resource.error}</Notice>}</>;
}
