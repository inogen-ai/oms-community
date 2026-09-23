"use client";
import { createContext, useContext, useEffect, useState, type DependencyList, type ReactNode } from "react";
import { createClient, type Capabilities, type CommunityClient } from "@inogen/oms-client";
import { Notice } from "@inogen/oms-ui-core";

const Context = createContext<{ api: CommunityClient; capabilities: Capabilities } | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [api, setApi] = useState<CommunityClient | null>(null);
  const [apiUrl, setApiUrl] = useState("");
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    async function open() {
      const response = await fetch("/oms-config.json", { cache: "no-store", credentials: "omit", redirect: "error" });
      if (!response.ok) throw new Error("The local application configuration could not be loaded.");
      const config = await response.json();
      if (typeof config.api_url !== "string") throw new Error("The local application configuration needs an api_url string.");
      const baseUrl = config.api_url || process.env.NEXT_PUBLIC_OMS_API_URL || "";
      const client = createClient({ baseUrl });
      if (active) setApiUrl(baseUrl);
      const value = await client.capabilities();
      if (value.edition !== "community") throw new Error("Connect this application to an OMS Community server.");
      if (active) { setApi(client); setCapabilities(value); }
    }
    void open().catch((failure) => { if (active) setError(message(failure)); });
    return () => { active = false; };
  }, []);
  if (error) return <main className="startup"><h1>Connect to your workspace</h1><Notice kind="error">{error}</Notice><p>Check that your local Community API is running {apiUrl ? <>at <code>{apiUrl}</code></> : "at this application’s address"} and permits this application’s origin.</p><button onClick={() => window.location.reload()}>Try again</button></main>;
  if (!capabilities || !api) return <main className="startup" role="status">Opening your local workspace…</main>;
  return <Context.Provider value={{ api, capabilities }}>{children}</Context.Provider>;
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
