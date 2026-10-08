"use client";
import { useEffect, useRef, useState } from "react";
import { Panel, SourceLookupForm, SourcePendingNotice, sourceLookupValue, type SourceLookupBody, type SourcePendingSubject } from "@inogen/oms-ui-core";
import { StaleSourceResponseError, type SourceDiscovery, type SourceOperation } from "@inogen/oms-client";
import { message, useWorkspace } from "@/lib/workspace";
import { useSourceResource } from "@/lib/source-state";

export default function SourceDiscoveryForm({ onDiscovered, initialUrl = "", subject = "request" }: {
  onDiscovered: (value: SourceDiscovery | null) => void; initialUrl?: string; subject?: SourcePendingSubject;
}) {
  const { sources, refreshSources } = useWorkspace();
  const [value, setValue] = useState(sourceLookupValue(initialUrl));
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, [sources]);
  const pending = useSourceResource(() => sources!.requests.pending(), [sources]);
  const previous = pending.data?.find(row => row.path === "/api/skill-source-discoveries");
  // Discovering and checking a receipt share one outcome: a discovery to show, or a refusal to report.
  async function settle(start: () => Promise<SourceDiscovery | SourceOperation | undefined>) {
    if (!sources || busy) return;
    setBusy(true); setError("");
    try {
      const response = await start();
      if (!response || !("discovery_id" in response) || typeof response.discovery_id !== "string") throw new Error("The discovery response is unavailable.");
      const result = "packages" in response ? response as SourceDiscovery : await sources.client.discovery(response.discovery_id);
      if (active.current) onDiscovered(result);
    } catch (failure) { if (active.current && !(failure instanceof StaleSourceResponseError)) setError(message(failure)); }
    finally { if (active.current) { setBusy(false); refreshSources(); } }
  }
  return <Panel title="Discover a repository">
    {previous && <SourcePendingNotice subject={subject} busy={busy} onCheck={() => void settle(() => sources!.requests.recover(previous.key))} />}
    <SourceLookupForm value={value} busy={busy} error={error} profile={{ mode: "text" }} submitLabel="Discover packages"
      onChange={next => { setValue(next); onDiscovered(null); }}
      onSubmit={(body: SourceLookupBody) => void settle(() => sources!.client.discover(body, crypto.randomUUID()))} />
  </Panel>;
}
