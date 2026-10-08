"use client";
import { useEffect, useRef, useState, type DependencyList } from "react";
import { HttpError, StaleSourceResponseError, type SourceOperation, type PendingSourceRequest } from "@inogen/oms-client";
import { message, useWorkspace } from "./workspace";

export function useSourceResource<T>(load: () => Promise<T>, dependencies: DependencyList) {
  const { sourceRevision } = useWorkspace();
  const [revision, setRevision] = useState(0);
  const requestIdentity = [...dependencies, sourceRevision, revision];
  const same = (left: DependencyList, right: DependencyList) => left.length === right.length && left.every((value, index) => Object.is(value, right[index]));
  const [state, setState] = useState<{ identity: DependencyList; requestIdentity: DependencyList; data: T | null; error: string; loading: boolean }>({ identity: dependencies, requestIdentity, data: null, error: "", loading: true });
  const current = same(dependencies, state.identity);
  useEffect(() => {
    let active = true;
    setState(previous => ({ identity: dependencies, requestIdentity, data: same(dependencies, previous.identity) ? previous.data : null, error: "", loading: true }));
    void load().then(data => { if (active) setState({ identity: dependencies, requestIdentity, data, error: "", loading: false }); }).catch(error => {
      if (active && !(error instanceof StaleSourceResponseError)) setState({ identity: dependencies, requestIdentity, data: null, error: message(error), loading: false });
    });
    return () => { active = false; };
  // The caller supplies resource identity; refresh counters never change its workspace.
  }, requestIdentity);
  return { data: current ? state.data : null, error: current ? state.error : "", loading: !current || !same(requestIdentity, state.requestIdentity) || state.loading,
    refresh: () => setRevision(value => value + 1) };
}

function operationPath(identity: string): string | null {
  let verb: string, id: unknown;
  try { [verb, id] = JSON.parse(identity); } catch { return null; }
  if (verb === "install") return "/api/skill-source-installations";
  if (verb === "local-import") return "/api/skill-local-imports";
  if (verb === "create-source") return "/api/skill-sources";
  if (verb === "bulk") return "/api/skill-updates/bulk-apply";
  if (typeof id !== "string") return null;
  const segment = encodeURIComponent(id);
  if (["draft", "apply", "skip", "adopt", "recheck", "undo"].includes(verb)) return `/api/skill-updates/${segment}/${verb}`;
  if (["check", "schedule", "remove", "relocate"].includes(verb)) return `/api/skill-sources/${segment}/${verb}`;
  if (["link", "relink", "retarget", "unlink", "automation"].includes(verb)) return `/api/skills/${segment}/source-binding/${verb}`;
  return null;
}

export function useSourceAction() {
  const { sources, refreshSources } = useWorkspace();
  const mounted = useRef(true), running = useRef(false), currentSources = useRef(sources);
  currentSources.current = sources;
  const current = () => mounted.current && currentSources.current === sources;
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [operation, setOperation] = useState<SourceOperation | null>(null);
  const [recoveryKey, setRecoveryKey] = useState<string | null>(null);
  const [canRetryOriginalInput, setCanRetryOriginalInput] = useState(false);
  const [originalInputKey, setOriginalInputKey] = useState<string | null>(null);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, [sources]);
  async function retainFailure(failure: unknown, key: string | null) {
    if (!sources || !current() || failure instanceof StaleSourceResponseError) return;
    let pending: PendingSourceRequest | undefined, unresolved = key;
    try { pending = (await sources.requests.pending()).find(row => row.key === key); unresolved = pending?.key ?? null; }
    catch { /* Keep the key when recovery status is unavailable. */ }
    if (!current()) return;
    setRecoveryKey(unresolved);
    setCanRetryOriginalInput(!!pending?.requires_body && !pending.operation_id);
    setError(message(failure) + (failure instanceof HttpError && failure.retry_after_seconds !== null ? ` Try again after ${failure.retry_after_seconds} seconds.` : ""));
  }
  function accept(result: SourceOperation) {
    if (!result || typeof result.operation_id !== "string" || typeof result.committed !== "boolean" || !Array.isArray(result.outcomes)
      || !["fetching", "planning", "awaiting_review", "applying", "complete", "blocked", "failed"].includes(result.state)
      || (result.committed && ["fetching", "planning", "applying"].includes(result.state))) throw new Error("The server returned an invalid source operation. Recover the original request before retrying.");
    if (!current()) return null;
    setOperation(result);
    if (["complete", "awaiting_review", "blocked", "failed"].includes(result.state)) { setRecoveryKey(null); setCanRetryOriginalInput(false); }
    return result;
  }
  async function run(job: (key: string) => Promise<SourceOperation>, identity: string) {
    if (running.current || !sources) return null;
    running.current = true; setBusy(true); setError(""); setOperation(null);
    const explicitKey = originalInputKey;
    setOriginalInputKey(null);
    let key: string | null = null;
    try {
      const pending = await sources.requests.pending();
      if (!current()) return null;
      const path = operationPath(identity);
      const previous = pending.filter(row => row.path.split("?")[0] === path);
      const original = previous.find(row => row.key === explicitKey && row.requires_body && !row.operation_id);
      if (previous.length && !original) {
        key = previous[0].key;
        throw new Error("A previous attempt needs recovery. Resolve it below before submitting your current input.");
      }
      key = original?.key ?? crypto.randomUUID();
      setRecoveryKey(key);
      return accept(await job(key));
    } catch (failure) { await retainFailure(failure, key); return null; }
    finally { running.current = false; if (current()) { setBusy(false); refreshSources(); } }
  }
  async function recover() {
    if (running.current || !sources || !recoveryKey) return;
    running.current = true; setBusy(true); setError(""); setOriginalInputKey(null);
    try {
      const result = await sources.requests.recover(recoveryKey);
      if (result && "state" in result) accept(result);
    } catch (failure) { await retainFailure(failure, recoveryKey); }
    finally { running.current = false; if (current()) { setBusy(false); refreshSources(); } }
  }
  function selectOriginalInputRetry(selected: boolean) {
    setOriginalInputKey(selected && canRetryOriginalInput ? recoveryKey : null);
  }
  return { busy, error, operation, run, recover, recoveryKey, canRetryOriginalInput,
    retryOriginalInput: !!recoveryKey && originalInputKey === recoveryKey, selectOriginalInputRetry };
}
