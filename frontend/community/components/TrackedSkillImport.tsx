"use client";
import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { LocalSourceSelection } from "@inogen/oms-client";
import { Button, GitHubMark, Notice, SkillPackagePicker, SourceStatus } from "@inogen/oms-ui-core";
import { ResourceStatus, message, useWorkspace } from "@/lib/workspace";
import { useSourceAction, useSourceResource } from "@/lib/source-state";
import { updateTarget } from "@/lib/sources";
import { SourceActionFeedback, SourceRecovery } from "./SourceOperations";
import GitHubImport from "./GitHubImport";

interface StagedUpload {
  upload_id: string; filename: string; warnings: string[];
  packages: { package_path: string; revision: string }[];
  skills: { name: string; domain: string; source_ref: string }[];
}
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
function stageResult(value: Record<string, unknown>): StagedUpload {
  if (typeof value.upload_id !== "string" || !value.upload_id || !Array.isArray(value.packages) || !Array.isArray(value.skills)) throw new Error("The server did not return a staged local package.");
  const packages = (value.packages as unknown[]).filter((row): row is StagedUpload["packages"][number] => record(row) && typeof row.package_path === "string" && typeof row.revision === "string");
  const skills = (value.skills as unknown[]).filter((row): row is StagedUpload["skills"][number] => record(row) && typeof row.name === "string" && typeof row.domain === "string" && typeof row.source_ref === "string");
  if (packages.length !== value.packages.length || skills.length !== value.skills.length) throw new Error("The staged package preview is incomplete.");
  return { upload_id: value.upload_id, filename: typeof value.filename === "string" ? value.filename : "Local package", packages, skills, warnings: Array.isArray(value.warnings) ? value.warnings.filter((item): item is string => typeof item === "string") : [] };
}
interface Target { destination: string; localName: string; domain: string }
const skillId = (name: string) => name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

/** `github` adds the third choice, "Add a GitHub source", which swaps the file chooser for the GitHub add flow in place. */
export interface GitHubChoice { active: boolean; discoveryId: string | null; onChange: (active: boolean) => void }

export default function TrackedSkillImport({ onImported, github }: { onImported?: () => void; github?: GitHubChoice }) {
  const { api, sources } = useWorkspace();
  const skills = useSourceResource(() => api.skills(), [api]);
  const [directory, setDirectory] = useState(false), [file, setFile] = useState<File | null>(null), [files, setFiles] = useState<File[]>([]);
  const [stage, setStage] = useState<StagedUpload | null>(null), [targets, setTargets] = useState<Record<string, Target>>({});
  const [attemptStage, setAttemptStage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, [api]);
  const action = useSourceAction();
  const selections: LocalSourceSelection[] = [];
  let invalid = false;
  const seen = new Set<string>();
  for (const pkg of stage?.packages ?? []) {
    const target = targets[pkg.package_path];
    if (!target?.destination) continue;
    const id = target.destination === "new" ? skillId(target.localName) : target.destination;
    if (!id || id.startsWith("repo-") || seen.has(id)) invalid = true;
    seen.add(id);
    if (target.destination === "new") {
      if (!target.domain.trim() || target.localName.length > 200 || target.domain.length > 200 || skills.data?.some(skill => skill.id === id)) invalid = true;
      selections.push({ package_path: pkg.package_path, local_name: target.localName, domain: target.domain });
    } else {
      if (!skills.data?.some(skill => skill.id === id)) invalid = true;
      selections.push({ package_path: pkg.package_path, target_skill_id: id });
    }
  }
  const refused = stage?.upload_id === attemptStage && (action.operation?.state === "blocked" || action.operation?.outcomes.some(row => row.state === "blocked"));
  async function upload() {
    if (busy || (!file && !files.length)) return;
    setBusy(true); setError("");
    try {
      const result = stageResult(directory ? await api.importDirectory(files) : await api.importArchive(file!));
      if (active.current) { setStage(result); setTargets({}); setFile(null); setFiles([]); }
    } catch (failure) { if (active.current) setError(message(failure)); }
    finally { if (active.current) setBusy(false); }
  }
  async function apply() {
    if (!stage || invalid || !selections.length || selections.length > 100) return;
    const body = { upload_id: stage.upload_id, selections };
    setAttemptStage(stage.upload_id);
    const result = await action.run(key => sources!.client.installLocal(body, key), JSON.stringify(["local-import", body]));
    // The server discards a preview once it is applied or queued for review.
    if (result && ["complete", "awaiting_review"].includes(result.state) && active.current) setStage(null);
    if (result) onImported?.();
  }
  const patch = (path: string, next: Partial<Target>) => setTargets(current => ({ ...current, [path]: { ...(current[path] ?? { destination: "", localName: "", domain: "general" }), ...next } }));
  return <><SourceRecovery match={row => row.path.split("?")[0] === "/api/skill-local-imports" && row.key !== action.recoveryKey} /><p>Upload a folder or ZIP for a retained preview, then explicitly choose a new or existing skill for each package. A filename never chooses an existing skill.</p>
    <div className="actions" role="group" aria-label="Upload format"><Button variant={!directory && !github?.active ? "primary" : "secondary"} disabled={busy || action.busy} onClick={() => { setDirectory(false); setFiles([]); setStage(null); github?.onChange(false); }}>ZIP package</Button><Button variant={directory && !github?.active ? "primary" : "secondary"} disabled={busy || action.busy} onClick={() => { setDirectory(true); setFile(null); setStage(null); github?.onChange(false); }}>Folder</Button>{github && <Button variant={github.active ? "primary" : "secondary"} disabled={busy || action.busy} onClick={() => github.onChange(true)}><GitHubMark size={15} />Add a GitHub source</Button>}</div>
    {github?.active ? <GitHubImport discoveryId={github.discoveryId} onInstalled={() => onImported?.()} /> : <>
    <SkillPackagePicker key={String(directory)} directory={directory} disabled={busy || action.busy} label={directory ? "Choose a skill folder" : "Choose a ZIP package"} inputLabel={directory ? "Skill folder" : "Skill ZIP package"}
      onChoose={value => { setFile(value); setStage(null); }} onChooseDirectory={value => { setFiles(value); setStage(null); }}>
      <span>{file?.name || (files.length ? `${files.length} files selected` : "Select a package to preview")}</span>
    </SkillPackagePicker>
    <Button disabled={busy || action.busy || (!file && !files.length)} onClick={() => void upload()}>{busy ? "Staging package…" : "Preview local package"}</Button>
    {error && <Notice kind="error">{error}</Notice>}<SourceActionFeedback action={action} subject="import" />{action.operation?.state === "awaiting_review" && action.operation.outcomes.filter(row => row.update_id).map(row => <p key={row.update_id}><Link href={updateTarget("local", row.update_id!)}>Review the update for {row.skill_id}</Link></p>)}
    {refused && <SourceStatus status="blocked" retryNeeded message="This local attempt was refused. Nothing is queued. Upload again for a fresh comparison after the blocker is resolved." />}
    {stage && <section className="compact" aria-label="Staged local package"><h3>{stage.filename}</h3><ResourceStatus resource={skills} />
      {stage.warnings.map((warning, index) => <Notice key={index}>{warning}</Notice>)}
      {stage.packages.map(pkg => {
        const description = stage.skills.find(item => item.source_ref === `${pkg.package_path ? `${pkg.package_path}/` : ""}SKILL.md`);
        const target = targets[pkg.package_path];
        return <fieldset key={pkg.package_path} disabled={action.busy || skills.loading}><legend>{pkg.package_path || "repository root"}</legend><p>{description?.name || "Skill package"}</p>
          <label>Destination for {pkg.package_path || "repository root"}<select value={target?.destination ?? ""} onChange={event => patch(pkg.package_path, { destination: event.target.value, localName: target?.localName || description?.name || "", domain: target?.domain || description?.domain || "general" })}>
            <option value="">Do not import this package</option><option value="new">Create a new skill</option>{skills.data?.map(skill => <option key={skill.id} value={skill.id}>Update {skill.name} ({skill.id})</option>)}
          </select></label>
          {target?.destination === "new" && <div className="form-grid"><label>Local name for {pkg.package_path}<input required maxLength={200} value={target.localName} onChange={event => patch(pkg.package_path, { localName: event.target.value })} /></label><label>Domain for {pkg.package_path}<input required maxLength={200} value={target.domain} onChange={event => patch(pkg.package_path, { domain: event.target.value })} /></label></div>}
          {target?.destination && target.destination !== "new" && <p>The selected skill keeps its local name and domain. Its local package stream is reconciled independently of GitHub.</p>}
        </fieldset>;
      })}
      {invalid && <Notice kind="error">Choose unique skill targets and valid new names and domains. New skill IDs must not already exist or use the reserved repo- prefix.</Notice>}
      {selections.length > 100 && <Notice>Select at most 100 packages for one local import.</Notice>}
      <Button disabled={busy || action.busy || skills.loading || !!skills.error || invalid || !selections.length || selections.length > 100 || !!refused} onClick={() => void apply()}>Apply selected local packages</Button>
    </section>}
    </>}
  </>;
}
