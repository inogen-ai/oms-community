"use client";
import { useId, useState } from "react";
import type { CoreSettings } from "@inogen/oms-client";
import { Button, Notice } from "@inogen/oms-ui-core";
import { Feedback, useAction, useWorkspace } from "@/lib/workspace";
import { CommandBlock, shellQuote } from "./GitPublicationSetup";

const text = (settings: CoreSettings, key: string) => typeof settings[key] === "string" ? String(settings[key]) : "";
function hostPath(value: string, home: string) {
  const expanded = value.startsWith("~/") && home ? `${home}/${value.slice(2)}` : value;
  if (!expanded.startsWith("/") || /[\u0000-\u001f\u007f]/.test(expanded)) return "";
  const parts: string[] = [];
  for (const part of expanded.split("/")) {
    if (part === "..") parts.pop();
    else if (part && part !== ".") parts.push(part);
  }
  return "/" + parts.join("/");
}

export default function HostPublicationFolder({ settings, onChanged, onEditing }: {
  settings: CoreSettings; onChanged: (value: CoreSettings) => void; onEditing?: (value: boolean) => void;
}) {
  const { api } = useWorkspace();
  const action = useAction();
  const id = useId();
  const [editing, setEditing] = useState(false);
  const [folder, setFolder] = useState("~/.oms_skills");
  const current = text(settings, "publish_host_root");
  const launcher = text(settings, "publish_launcher_dir");
  const project = text(settings, "publish_compose_project");
  const home = text(settings, "publish_host_home");
  const requested = hostPath(folder, home);
  const valid = Boolean(requested && requested !== "/" && requested !== home && requested !== launcher);
  const managed = Boolean(launcher && project);
  const command = `cd ${shellQuote(launcher)} &&\nCOMPOSE_PROJECT_NAME=${shellQuote(project)} ./run-local.sh --publish-dir ${shellQuote(folder)}`;
  function toggle(value: boolean) { setEditing(value); onEditing?.(value); }

  return <section className="publication-folder" aria-label="Host publication folder">
    <div className="publication-folder-heading"><div><span className="muted">Folder on the Docker host</span><p><code>{current || "No host folder mapped"}</code></p></div>
      <Button variant="secondary" aria-expanded={editing} aria-controls={id} onClick={() => toggle(!editing)}>{editing ? "Cancel folder change" : "Change folder"}</Button></div>
    <Feedback action={action} />
    {editing && <div id={id} className="publication-folder-change">
      {managed ? <><label>New host folder<input autoFocus value={folder} onChange={event => setFolder(event.target.value)} placeholder="~/.oms_skills" aria-describedby={`${id}-help`} /></label>
        <p id={`${id}-help`} className="muted">Use a dedicated folder on the machine running Docker. <code>~/</code> means your home folder on that machine.</p>
        {!valid ? <Notice kind="info">Enter an absolute path or a folder beneath ~/.</Notice> : <>
          <p>Run this in a terminal on the Docker host, as your normal user. It remembers the folder and restarts this workspace with the new mount.</p>
          <CommandBlock label="Folder change command" command={command} />
          <Button variant="secondary" disabled={action.busy} onClick={() => void action.run(async () => {
            const updated = await api.settings();
            const actual = text(updated, "publish_host_root");
            if (hostPath(actual, text(updated, "publish_host_home")) !== requested) {
              throw new Error(`OMS is still using ${actual || "an unmapped folder"}. Finish the terminal command, then check again.`);
            }
            onChanged(updated); toggle(false);
          }, "Folder connected. Publish again, then run the installer from the new folder.")}>{action.busy ? "Checking…" : "Check folder connection"}</Button>
        </>}
        <p className="muted">Your skills and review history stay in this workspace. Existing published files stay in their old folder. Publish again and run the new installer to update your agents’ links.</p>
      </> : <Notice kind="info">This mount is managed outside the Community launcher. Change the host folder mapped to <code>/published</code> in your Docker configuration and recreate the API service. With the standard Community setup, run the updated <code>run-local.sh</code> once to enable guided folder changes here.</Notice>}
    </div>}
  </section>;
}
