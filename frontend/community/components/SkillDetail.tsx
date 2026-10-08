"use client";
import { useEffect, useId, useState } from "react";
import type { Skill, SkillDeletion, SkillDocument, SkillSourceSummary } from "@inogen/oms-client";
import { AutoTextarea, Badge, Button, GitHubMark, SkillTextEditor, EmptyState, Markdown, Notice, Panel, SkillDeleteConfirmation, SkillMarkdown, VersionComparison, type VersionComparisonView } from "@inogen/oms-ui-core";
import { HistorySummary } from "@inogen/oms-ui-core/history";
import { ArrowLeft, FileText, History, Pencil, Trash2, Volume2, VolumeX } from "lucide-react";
import { Feedback, ResourceStatus, useAction, useResource, useWorkspace } from "@/lib/workspace";
import { PageHeading } from "./Shell";
import SkillGitHubPanel, { skillGitHubPanelId } from "./SkillGitHubPanel";
import SourceSkillHistory from "./SourceSkillHistory";
import type { SkillDraft, UpdateDraft } from "./Skills";

const tabs = ["Preview", "Markdown source", "Edit document", "Files", "History"] as const;
type Tab = typeof tabs[number];

export default function SkillDetail({ id, source, domains, draft, onDraftChange, onBack, onDeleted }: {
  id: string; source?: SkillSourceSummary | null; domains: string[]; draft: SkillDraft; onDraftChange: UpdateDraft; onBack: () => void; onDeleted: (result: SkillDeletion) => void;
}) {
  const { api, sourceRevision } = useWorkspace();
  const resource = useResource(() => api.skill(id), [api, id, sourceRevision]);
  return <><Button variant="secondary" onClick={onBack}><ArrowLeft size={15} />All skills</Button><ResourceStatus resource={resource} />{resource.data?.id === id && <SkillEditor skill={resource.data} source={source} domains={domains} refresh={resource.refresh} draft={draft} onDraftChange={onDraftChange} onDeleted={onDeleted} />}</>;
}

function SkillEditor({ skill, source, domains, draft, onDraftChange, refresh, onDeleted }: {
  skill: Skill; source?: SkillSourceSummary | null; domains: string[]; draft: SkillDraft; onDraftChange: UpdateDraft; refresh: () => void; onDeleted: (result: SkillDeletion) => void;
}) {
  const { api, capabilities } = useWorkspace();
  const [tab, setTab] = useState<Tab>("Preview");
  const [editing, setEditing] = useState(false);
  const [github, setGithub] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  const action = useAction();
  const domainId = useId();
  const tabId = useId();
  const details = draft.details ?? { name: skill.name, description: skill.description || "", domain: skill.domain || "general" };
  const muted = skill.publish_enabled === false;
  const dirty = Boolean(draft.details || Object.keys(draft.sections ?? {}).length);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);
  return <>
    <div className="skill-overline"><Badge>{skill.domain || "general"}</Badge><span>{muted ? "Muted · excluded from publication" : "Included in publication"}</span>{dirty && <span className="collection-unsaved">Unsaved edits</span>}</div>
    <PageHeading title={skill.name} description={skill.description || "Instructions and context for your agents."}><div className="actions"><Button variant="secondary" aria-expanded={editing} onClick={() => setEditing(!editing)}><Pencil size={14} />Edit details</Button><Button variant="secondary" disabled={action.busy} onClick={() => void action.run(() => api.updateSkill(skill.id, { publish_enabled: muted }), muted ? "Skill included in the next publication." : "Skill muted. Publish to update your agents.", refresh)}>{muted ? <Volume2 size={14} /> : <VolumeX size={14} />}{muted ? "Unmute skill" : "Mute skill"}</Button><Button id={`${tabId}-delete`} variant="secondary" disabled={action.busy} aria-expanded={deleting !== null} onClick={() => setDeleting("")}><Trash2 size={14} />Delete skill</Button>{capabilities.github_skill_sources && source !== undefined && <Button variant="secondary" aria-expanded={github} aria-controls={github ? skillGitHubPanelId(skill.id) : undefined} onClick={() => setGithub(!github)}><GitHubMark />{source ? "GitHub" : "Link to GitHub repository"}</Button>}</div></PageHeading>
    <Feedback action={action} />
    {deleting !== null && <SkillDeleteConfirmation name={skill.name} confirmation={deleting} onConfirmationChange={setDeleting} busy={action.busy} hasUnsavedChanges={dirty} onCancel={() => { setDeleting(null); document.getElementById(`${tabId}-delete`)?.focus(); }} onConfirm={() => {
      if (deleting !== skill.name || action.busy) return;
      void action.run(async () => { const result = await api.deleteSkill(skill.id); onDeleted(result); }, "Skill deleted.");
    }} />}
    {editing && <Panel title="Skill details"><form className="form-grid" onSubmit={(event) => { event.preventDefault(); void action.run(() => api.updateSkill(skill.id, details), "Skill details saved.", () => { onDraftChange(current => ({ ...current, details: undefined })); setEditing(false); refresh(); }); }}>
      <label>Name<input required maxLength={200} value={details.name} disabled={action.busy} onChange={event => onDraftChange(current => ({ ...current, details: { ...details, name: event.target.value } }))} /></label>
      <label>Description<AutoTextarea rows={4} maxLength={10000} value={details.description} disabled={action.busy} onChange={event => onDraftChange(current => ({ ...current, details: { ...details, description: event.target.value } }))} /></label>
      <label>Domain<input required maxLength={200} list={domainId} value={details.domain} disabled={action.busy} onChange={event => onDraftChange(current => ({ ...current, details: { ...details, domain: event.target.value } }))} /><datalist id={domainId}>{domains.map(domain => <option key={domain} value={domain} />)}</datalist></label><p className="muted">Choose an existing domain or type a new one. Imports without an explicit domain use “general”.</p>
      <div className="actions"><Button type="submit" disabled={action.busy}>Save details</Button><Button variant="secondary" disabled={action.busy} onClick={() => { onDraftChange(current => ({ ...current, details: undefined })); setEditing(false); }}>Discard detail edits</Button></div>
    </form></Panel>}
    {capabilities.github_skill_sources && github && source !== undefined && <SkillGitHubPanel skill={skill} source={source} dirty={dirty} />}
    <div className="document-tabs" role="tablist" aria-label="Skill views">{tabs.map((name, index) => <button key={name} id={`${tabId}-${index}`} role="tab" aria-selected={tab === name} aria-controls={`${tabId}-panel`} tabIndex={tab === name ? 0 : -1} onClick={() => setTab(name)} onKeyDown={event => {
      let next = index;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else return;
      event.preventDefault(); setTab(tabs[next]); document.getElementById(`${tabId}-${next}`)?.focus();
    }}>{name}{name === "Files" ? ` (${skill.artefacts?.length ?? 0})` : ""}</button>)}</div>
    <section id={`${tabId}-panel`} role="tabpanel" aria-labelledby={`${tabId}-${tabs.indexOf(tab)}`} tabIndex={0}>
      {tab === "Preview" && <Panel className="skill-document"><SkillMarkdown renderImages={false} body={skill.body || "This skill has no guidance yet. Add a correction in the inbox."} /></Panel>}
      {tab === "Markdown source" && <Panel title="Markdown source"><pre className="document">{skill.body || "No guidance yet."}</pre></Panel>}
      {tab === "Edit document" && <DocumentEditor skill={skill} draft={draft} onDraftChange={onDraftChange} refresh={refresh} />}
      {tab === "Files" && <SkillFiles skill={skill} />}
      {tab === "History" && <><SkillHistory skillId={skill.id} dirty={dirty} onRestored={refresh} />{capabilities.github_skill_sources && source === null && <SourceSkillHistory skillId={skill.id} dirty={dirty} />}</>}
    </section>
  </>;
}

function DocumentEditor({ skill, draft, onDraftChange, refresh }: { skill: Skill; draft: SkillDraft; onDraftChange: UpdateDraft; refresh: () => void }) {
  const { api } = useWorkspace();
  const resource = useResource(() => api.document(skill.id), [api, skill.id, skill.body]);
  const action = useAction();
  const values = draft.sections ?? {};
  const dirtyCount = Object.keys(values).length;
  const parts = dirtyCount && draft.parts ? draft.parts : resource.data?.parts ?? [];
  const stale = Boolean(dirtyCount && draft.revision !== resource.data?.revision);
  const rules = new Map(skill.rules?.map(rule => [rule.id, rule]) ?? []);
  const inactive = (skill.rules ?? []).filter(rule => ["retired", "retracted", "superseded"].includes(rule.status ?? ""));
  function afterChange() { resource.refresh(); refresh(); }
  return <Panel title="Edit document"><p className="muted">Edit the text directly, including related passages elsewhere in the skill. Use Find in skill to jump between mentions, then save your changes together.</p><ResourceStatus resource={resource} /><Feedback action={action} />
    {stale && <Notice kind="error">The document changed while you were editing. Your draft is kept below. Copy any text you need, then discard the draft to load the latest version.</Notice>}
    {resource.data && <><div className="document-save"><span className="muted">{dirtyCount} unsaved {dirtyCount === 1 ? "change" : "changes"}</span><Button disabled={!dirtyCount || stale || action.busy || resource.loading} onClick={() => void action.run(() => api.saveDocument(skill.id, { revision: draft.revision!, parts: Object.entries(values).map(([anchor, text]) => ({ anchor, text })) }), "Document saved.", () => { onDraftChange(current => ({ ...current, sections: undefined, revision: undefined, parts: undefined })); afterChange(); })}>Save document</Button>{dirtyCount > 0 && <Button variant="secondary" disabled={action.busy} onClick={() => onDraftChange(current => ({ ...current, sections: undefined, revision: undefined, parts: undefined }))}>Discard document edits</Button>}</div>
      <SkillTextEditor parts={parts} values={values} disabled={action.busy} onChange={(anchor, text) => onDraftChange(current => {
        const sections = { ...current.sections };
        if (text === parts.find(part => part.anchor === anchor)?.edit_text) delete sections[anchor];
        else sections[anchor] = text;
        return { ...current, sections, revision: Object.keys(sections).length ? current.revision ?? resource.data!.revision : undefined, parts: Object.keys(sections).length ? current.parts ?? resource.data!.parts : undefined };
      })} actionsForPart={part => {
        const rule = part.source_id ? rules.get(part.source_id) : null;
        return rule ? <div className="rule-evidence"><span className="muted">{rule.corroboration_count ?? 1} supporting {rule.corroboration_count === 1 ? "correction" : "corrections"}</span><Button variant="secondary" disabled={action.busy || dirtyCount > 0} onClick={() => void action.run(() => api.updateRule(rule.id, { action: "retract" }), "Rule retracted.", afterChange)}>Retract rule</Button>{Boolean(rule.transaction_ids?.length) && <details><summary>Correction evidence</summary><p className="muted">Corrections: {rule.transaction_ids!.join(", ")}</p></details>}</div> : null;
      }} />
    </>}
    {inactive.length > 0 && <details><summary>Inactive rules ({inactive.length})</summary>{inactive.map(rule => <article key={rule.id} className="record"><Markdown inert body={rule.body} /><Button variant="secondary" disabled={action.busy || dirtyCount > 0} onClick={() => void action.run(() => api.updateRule(rule.id, { action: "restore" }), "Rule restored.", afterChange)}>Restore rule</Button></article>)}</details>}
  </Panel>;
}

function SkillFiles({ skill }: { skill: Skill }) {
  const [path, setPath] = useState<string | null>(null);
  const files = (skill.artefacts ?? []).map(file => typeof file === "string" ? file : file.path);
  if (!files.length) return <Panel><EmptyState title="No supporting files">References and scripts appear here when you import a skill folder.</EmptyState></Panel>;
  return <div className="file-workspace"><Panel title="References & scripts"><ul className="file-picker">{files.map(file => <li key={file}><button aria-pressed={path === file} onClick={() => setPath(file)}><FileText size={15} /><span>{file}</span></button></li>)}</ul><p className="muted">Files remain part of the published skill. Previewing a script never runs it.</p></Panel>{path ? <FileContent key={path} skillId={skill.id} path={path} /> : <Panel><EmptyState title="Select a file">Inspect its contents before publishing.</EmptyState></Panel>}</div>;
}

function FileContent({ skillId, path }: { skillId: string; path: string }) {
  const { api } = useWorkspace();
  const resource = useResource(() => api.file(skillId, path), [api, skillId, path]);
  const [source, setSource] = useState(false);
  return <Panel title={path}><ResourceStatus resource={resource} />{resource.data && <><p className="muted">{resource.data.size.toLocaleString()} bytes</p>{resource.data.text === null ? <p>{resource.data.reason}</p> : <>{/\.md$/i.test(path) && <Button variant="secondary" aria-pressed={source} onClick={() => setSource(!source)}>{source ? "Preview Markdown" : "Show source"}</Button>}{/\.md$/i.test(path) && !source ? <Markdown inert body={resource.data.text} /> : <pre className="document file-source">{resource.data.text}</pre>}</>}</>}</Panel>;
}

function SkillHistory({ skillId, dirty, onRestored }: { skillId: string; dirty: boolean; onRestored: () => void }) {
  const { api, sourceRevision } = useWorkspace();
  const versions = useResource(() => api.versions(skillId), [api, skillId, sourceRevision]);
  const [selected, setSelected] = useState({ id: "", request: 0 });
  const [view, setView] = useState<VersionComparisonView>("overview");
  return <div className="history-workspace"><Panel title="Saved versions"><ResourceStatus resource={versions} /><p className="muted">Choose a version to compare with the current document. Restore only the changes you want.</p><ol className="version-picker">{versions.data?.map(version => <li key={version.id}><button data-version-id={version.id} aria-pressed={selected.id === version.id} onClick={() => setSelected(current => ({ id: version.id, request: current.request + 1 }))}><History size={15} /><HistorySummary entry={version} people={new Map([["local-operator", "Local operator"]])} /></button></li>)}</ol>{versions.data?.length === 0 && <EmptyState title="No saved versions yet" />}</Panel>{selected.id ? <HistoryComparison key={`${selected.id}:${selected.request}`} skillId={skillId} versionId={selected.id} dirty={dirty} view={view} onViewChange={setView} onRestored={() => { versions.refresh(); onRestored(); }} /> : <Panel><EmptyState title="Choose a saved version">Compare wording, review changes, and bring back selected sections or rules.</EmptyState></Panel>}</div>;
}

function HistoryComparison({ skillId, versionId, dirty, view, onViewChange, onRestored }: { skillId: string; versionId: string; dirty: boolean; view: VersionComparisonView; onViewChange: (view: VersionComparisonView) => void; onRestored: () => void }) {
  const { api } = useWorkspace();
  const resource = useResource(() => api.compareVersion(skillId, versionId), [api, skillId, versionId]);
  const [selected, setSelected] = useState<string[]>([]);
  const [preview, setPreview] = useState(false);
  const action = useAction();
  const unavailable = action.busy || resource.loading || Boolean(resource.error);
  return <Panel title="Compare with current document"><ResourceStatus resource={resource} />{resource.error && <p className="muted">Select a saved version again to retry.</p>}<Feedback action={action} />{resource.data && <>
    <div className="actions">
      <div className="oms-collection-view" role="group" aria-label="Comparison view">
        {(["overview", "detailed"] as const).map(mode => <button key={mode} type="button" aria-pressed={!preview && view === mode} onClick={() => { onViewChange(mode); setPreview(false); }}>{mode === "overview" ? "Overview" : "Detailed"}</button>)}
      </div>
      <Button variant="secondary" aria-pressed={preview} onClick={() => setPreview(!preview)}>{preview ? "Show changes" : "Read saved version"}</Button>
    </div>
    {dirty && <Notice>Save or discard your unsaved edits before restoring history.</Notice>}
    <p className="muted">Unselected wording stays as it is. Shared rules also change in the other skills that use them.</p>
    {preview ? <SkillMarkdown renderImages={false} body={resource.data.markdown} /> : <>
      {view === "overview" && resource.data.rows.some(row => row.state !== "same") && <p className="muted">Highlights show the exact text removed from the saved version or added in the current document.</p>}
      {view === "detailed" && <p className="muted">Changes if you restore this version: − current text, + saved text. Unchanged text is shown for context.</p>}
      {resource.data.rows.every(row => row.state === "same") && <EmptyState title="No document changes">This version has the same published wording as the current document.</EmptyState>}
      <VersionComparison view={view} rows={resource.data.rows} selected={selected} disabled={unavailable} onSelect={(anchor, checked) => setSelected(current => checked ? [...current, anchor] : current.filter(value => value !== anchor))} />
    </>}
    <div className="actions history-restore"><Button disabled={!selected.length || dirty || unavailable} onClick={() => void action.run(() => api.restoreVersion(skillId, versionId, { revision: resource.data!.revision, anchors: selected }), "Selected changes restored as a new version.", () => { setSelected([]); resource.refresh(); onRestored(); })}>Restore {selected.length ? `${selected.length} selected ${selected.length === 1 ? "change" : "changes"}` : "selected changes"}</Button></div>
  </>}</Panel>;
}
