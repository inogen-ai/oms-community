"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button, Notice, Panel } from "@inogen/oms-ui-core";
import { StaleSourceResponseError, type SourceDiscovery, type DiscoverSourceBody } from "@inogen/oms-client";
import { message, useWorkspace } from "@/lib/workspace";
import { useSourceResource } from "@/lib/source-state";

export default function SourceDiscoveryForm({ onDiscovered, initialUrl = "" }: { onDiscovered: (value: SourceDiscovery | null) => void; initialUrl?: string }) {
  const { sources, sourceRevision, refreshSources } = useWorkspace();
  const [url, setUrl] = useState(initialUrl), [profile, setProfile] = useState("");
  const [kind, setKind] = useState<"" | "branch" | "tag" | "commit">(""), [name, setName] = useState(""), [path, setPath] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [newRequest, setNewRequest] = useState(false);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, [sources]);
  const pending = useSourceResource(() => sources!.requests.pending(), [sources]);
  const previous = pending.data?.find(row => row.path === "/api/skill-source-discoveries");
  function changed(change: () => void) { change(); onDiscovered(null); }
  async function discover(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!sources || busy) return;
    const body: DiscoverSourceBody = { url: url.trim(), ...(profile.trim() ? { credential_profile_id: profile.trim() } : {}),
      ...(kind ? { ref_kind: kind, ref_name: name.trim() } : {}), ...(path.trim() ? { package_path: path.trim() } : {}) };
    setBusy(true); setError("");
    try {
      const response = previous && !newRequest ? await sources.requests.recover(previous.key, { body: JSON.stringify(body) }) : await sources.client.discover(body, crypto.randomUUID());
      if (!response || !("discovery_id" in response) || typeof response.discovery_id !== "string") throw new Error("The discovery response is unavailable.");
      const result = "packages" in response ? response : await sources.client.discovery(response.discovery_id);
      if (active.current) { onDiscovered(result); setNewRequest(false); }
    } catch (failure) { if (active.current && !(failure instanceof StaleSourceResponseError)) setError(message(failure)); }
    finally { if (active.current) { setBusy(false); refreshSources(); } }
  }
  return <Panel title="Discover a repository"><form className="form-grid" onSubmit={event => void discover(event)}>
    <label>GitHub repository or skill folder URL<input type="url" required value={url} disabled={busy} onChange={event => changed(() => setUrl(event.target.value))} /></label>
    <label>Ref type<select value={kind} disabled={busy} onChange={event => changed(() => setKind(event.target.value as typeof kind))}><option value="">Default branch</option><option value="branch">Branch</option><option value="tag">Pinned tag</option><option value="commit">Exact commit</option></select></label>
    {kind && <label>Ref name or commit<input required value={name} disabled={busy} onChange={event => changed(() => setName(event.target.value))} /></label>}
    <label>Skill folder (optional)<input value={path} disabled={busy} onChange={event => changed(() => setPath(event.target.value))} /></label>
    <label>Credential profile ID (optional)<input value={profile} disabled={busy} onChange={event => changed(() => setProfile(event.target.value))} autoComplete="off" /></label>
    <p className="muted">Use a configured profile ID for private repositories. Credentials stay on the server.</p>
    {previous && <label className="oms-source-check"><input type="checkbox" checked={newRequest} disabled={busy} onChange={event => setNewRequest(event.target.checked)} />Start a new discovery instead of recovering the previous request</label>}
    {error && <Notice kind="error">{error}</Notice>}
    <Button type="submit" disabled={busy || !url.trim() || (!!kind && !name.trim())}>{busy ? "Discovering…" : previous && !newRequest ? "Recover discovery" : "Discover packages"}</Button>
  </form></Panel>;
}
