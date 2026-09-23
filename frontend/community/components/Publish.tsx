"use client";
import { useState } from "react";
import { Button, EmptyState, Notice, Panel, SkillMarkdown } from "@inogen/oms-ui-core";
import { Feedback, message, ResourceStatus, useAction, useResource, useWorkspace } from "@/lib/workspace";
import { PageHeading } from "./Shell";
import PublicationSetup, { PublicationDestination, PublicationInstall, type PublicationResult } from "./PublicationSetup";
import HostPublicationFolder from "./HostPublicationFolder";

export default function Publish() {
  const { api } = useWorkspace();
  const resource = useResource(() => api.skills(), [api]);
  const settings = useResource(() => api.settings(), [api]);
  const action = useAction();
  const [selected, setSelected] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [published, setPublished] = useState<PublicationResult | null>(null);
  const [showInstall, setShowInstall] = useState(false);
  const [setup, setSetup] = useState(false);
  const [changingHost, setChangingHost] = useState(false);
  function publicationComplete(result: PublicationResult) {
    setPublished(result); setShowInstall(true);
  }
  function destinationChanged() {
    setPublished(null); setShowInstall(false);
  }
  async function loadPreview() {
    setPreviewing(true); setPreviewError("");
    try { const data = await api.preview(selected); setPreview(typeof data === "string" ? data : data.body ?? data.markdown ?? data.text ?? ""); }
    catch (error) { setPreviewError(message(error)); }
    finally { setPreviewing(false); }
  }
  return <><PageHeading title="Preview, then publish" /><ResourceStatus resource={resource} /><Feedback action={action} />
    <div className="two-column"><Panel title="Preview a skill"><label>Skill<select value={selected} onChange={(event) => { setSelected(event.target.value); setPreview(null); }}><option value="">Choose a skill</option>{resource.data?.map((skill) => <option key={skill.id} value={skill.id}>{skill.name}</option>)}</select></label><Button variant="secondary" disabled={!selected || previewing} onClick={() => void loadPreview()}>{previewing ? "Loading preview…" : "Preview skill"}</Button>{previewError && <Notice kind="error">{previewError}</Notice>}{preview !== null ? <div className="preview" aria-label="Published skill preview"><SkillMarkdown renderImages={false} body={preview} /><details className="source-disclosure"><summary>Markdown source</summary><pre className="document">{preview}</pre></details></div> : <div className="compact"><EmptyState title="Choose a skill to preview">The preview is produced by the same renderer as publication.</EmptyState></div>}</Panel>
    <Panel title="Publish the workspace">
      <ResourceStatus resource={settings} />
      {settings.data && (!setup && settings.data.publish_in_container === true && settings.data.publication_target !== "git" ? <>
        <HostPublicationFolder settings={settings.data} onEditing={setChangingHost} onChanged={value => { settings.setData(value); destinationChanged(); }} />
        {settings.data.publication_folder && <p className="muted">Publishing into <code>{String(settings.data.publish_host_path)}</code></p>}
      </> : <PublicationDestination settings={settings.data} />)}
      <p>Write the current approved guidance and supporting files to your configured publication target.</p>
      <p className="muted">Pending and rejected corrections stay out of the published tree. Muted skills are excluded.</p>
      <div className="actions">
        <Button disabled={action.busy || changingHost || !settings.data || settings.data.publication_ready === false} onClick={() => void action.run(async () => { publicationComplete(await api.publish()); setSetup(false); }, "Workspace published.")}>{action.busy ? "Publishing…" : settings.data?.publication_target === "git" ? "Publish and push" : "Publish skills"}</Button>
        <Button variant="secondary" disabled={changingHost} aria-expanded={setup} onClick={() => setSetup(value => !value)}>{setup ? "Hide setup" : "Set up publication"}</Button>
      </div>
      {published && <dl className="details"><div><dt>Output</dt><dd>{published.output}</dd></div><div><dt>Skills</dt><dd>{Array.isArray(published.skills) ? published.skills.length : published.skills}</dd></div></dl>}
      {settings.data && !setup && !changingHost && <details className="publication-install" open={showInstall} onToggle={event => setShowInstall(event.currentTarget.open)}>
        <summary>Install or reconnect agents</summary>
        <PublicationInstall settings={settings.data} published={published !== null} />
      </details>}
    </Panel></div>
    {setup && settings.data && <div style={{ marginTop: 24 }}><PublicationSetup settings={settings.data} onHostEditing={setChangingHost} onSaved={value => { settings.setData(value); destinationChanged(); }} onPublished={publicationComplete} /></div>}
  </>;
}
