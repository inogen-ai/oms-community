"use client";
import { useState } from "react";
import Link from "next/link";
import { Button, SkillPackagePicker } from "@inogen/oms-ui-core";
import TrackedSkillImport from "./TrackedSkillImport";
import { Feedback, useAction, useWorkspace } from "@/lib/workspace";

/** Used from both the skills library and the import page. */
export default function SkillImport({ onImported }: { onImported?: () => void }) {
  const { capabilities } = useWorkspace();
  return capabilities.github_skill_sources ? <TrackedSkillImport onImported={onImported} /> : <LegacySkillImport onImported={onImported} />;
}

function LegacySkillImport({ onImported }: { onImported?: () => void }) {
  const { api } = useWorkspace();
  const [file, setFile] = useState<File | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [directory, setDirectory] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const action = useAction();
  if (hidden) return <Button variant="secondary" onClick={() => setHidden(false)}>Upload more skills</Button>;
  return <>
    <p className="muted">Choose a folder or ZIP containing SKILL.md files, references and scripts. Up to 25 MB; folders may contain up to 1,000 files.</p>
    <div className="actions" role="group" aria-label="Upload format"><Button variant={!directory ? "primary" : "secondary"} aria-pressed={!directory} onClick={() => { setDirectory(false); setFiles([]); setResult(null); }}>ZIP package</Button><Button variant={directory ? "primary" : "secondary"} aria-pressed={directory} onClick={() => { setDirectory(true); setFile(null); setResult(null); }}>Folder</Button></div>
    <form onSubmit={(event) => {
      event.preventDefault();
      if ((file || files.length) && !action.busy) void action.run(async () => {
        setResult(directory ? await api.importDirectory(files) : await api.importArchive(file!));
        setFile(null); setFiles([]);
      }, "Skill package imported.", onImported);
    }}>
      <SkillPackagePicker key={String(directory)} directory={directory} disabled={action.busy} label={directory ? "Choose a skill folder" : "Choose a ZIP package"} inputLabel={directory ? "Skill folder" : "Skill ZIP package"}
        onChooseDirectory={(next) => { setFiles(next); setResult(null); }}
        onChoose={(next) => { setFile(next); setResult(null); }}>
        <span className="muted">{files.length ? `${files.length} files selected` : file ? `${file.name} · ${(file.size / 1024).toFixed(1)} KB` : "Your original files stay together in this workspace."}</span>
      </SkillPackagePicker>
      <Button type="submit" disabled={(!file && !files.length) || action.busy}>{action.busy ? "Importing…" : "Import package"}</Button>
    </form>
    <Feedback action={action} />
    {result && <div className="import-result">
      <p>Check <Link href="/import">import review</Link> for changes that need a decision. <Link href="/skills">Open your skills library</Link>.</p>
      <details><summary>Import details</summary><pre className="small-json" aria-label="Import result">{JSON.stringify(result, null, 2)}</pre></details>
      <Button variant="secondary" onClick={() => setHidden(true)}>Hide upload</Button>
    </div>}
  </>;
}
