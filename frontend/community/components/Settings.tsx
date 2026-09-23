"use client";
import { useState, type FormEvent } from "react";
import type { CoreSettings } from "@inogen/oms-client";
import { Button, Panel } from "@inogen/oms-ui-core";
import { Feedback, ResourceStatus, useAction, useResource, useWorkspace } from "@/lib/workspace";
import { PageHeading } from "./Shell";
import PublicationSetup from "./PublicationSetup";
import { AboutEdition } from "./EditionInformation";

const editable = new Set(["body_budget", "skill_history_keep", "root_instruction_files"]);
const labels: Record<string, string> = { body_budget: "Maximum rules in the main skill file", skill_history_keep: "Versions to retain per skill" };

export default function Settings() {
  const { api } = useWorkspace();
  const resource = useResource(() => api.settings(), [api]);
  return <><PageHeading title="Workspace settings" /><ResourceStatus resource={resource} /><div className="settings-layout">{resource.data && <><PublicationSetup settings={resource.data} /><SettingsForm key={JSON.stringify(resource.data)} settings={resource.data} refresh={resource.refresh} /></>}<AboutEdition /></div></>;
}

function SettingsForm({ settings, refresh }: { settings: CoreSettings; refresh: () => void }) {
  const { api } = useWorkspace();
  const action = useAction();
  const [draft, setDraft] = useState<CoreSettings>({});
  function save(event: FormEvent) { event.preventDefault(); void action.run(() => api.updateSettings(draft), "Settings saved.", refresh); }
  return <Panel title="Bundle content"><Feedback action={action} /><form className="form-grid" onSubmit={save}>{Object.entries(settings).filter(([key]) => editable.has(key)).map(([key, value]) => {
    const displayed = draft[key] ?? value;
    if (key === "root_instruction_files") {
      const chosen = Array.isArray(displayed) ? displayed : [];
      return <fieldset key={key}><legend>Root instruction files</legend><div className="checkbox-grid">{["AGENTS.md", "CLAUDE.md"].map((name) => <label className="check-label" key={name}><input type="checkbox" checked={chosen.includes(name)} onChange={(event) => setDraft({ ...draft, [key]: event.target.checked ? [...chosen, name] : chosen.filter((item) => item !== name) })} />{name}</label>)}</div></fieldset>;
    }
    return <div key={key}><label>{labels[key]}<input type="number" min={key === "body_budget" ? 20 : 1} max={10000} step={1} required aria-describedby={key === "body_budget" ? "body-budget-help" : undefined} value={String(displayed ?? "")} onChange={(event) => setDraft({ ...draft, [key]: Number(event.target.value) })} /></label>{key === "body_budget" && <p id="body-budget-help" className="muted">Counts rules, not words. For skills published as flat rule lists, additional rules move to a linked reference file. Imported skills with preserved sections and prose are not capped by this setting. Applies on the next publish.</p>}</div>;
  })}{Object.keys(settings).some((key) => editable.has(key)) ? <Button type="submit" disabled={action.busy || !Object.keys(draft).length}>Save settings</Button> : <p className="muted">Publication settings are configured on the local server.</p>}</form><p className="muted">These settings take effect on the next publication.</p></Panel>;
}
