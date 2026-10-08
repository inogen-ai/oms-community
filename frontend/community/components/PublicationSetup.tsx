"use client";
import { useState, type FormEvent } from "react";
import type { CoreSettings } from "@inogen/oms-client";
import { Button, Notice, Panel } from "@inogen/oms-ui-core";
import { Feedback, useAction, useWorkspace } from "@/lib/workspace";
import { CommandBlock, GitPublisherAccess, GitReaderAccess, gitInstallCommand, shellQuote } from "./GitPublicationSetup";
import HostPublicationFolder from "./HostPublicationFolder";

export const settingText = (settings: CoreSettings, key: string, fallback = "") => typeof settings[key] === "string" ? String(settings[key]) : fallback;
export type PublicationResult = { output: string; skills: number | string[]; pushed?: boolean };

/** The address a Git bundle would tell agents to post corrections to. Git
 * publication refuses a loopback address, so say so here, before the person
 * creates a token and logs the publisher in, rather than at the final step. */
export function ContributionAddress({ settings }: { settings: CoreSettings }) {
  const address = settingText(settings, "public_url");
  if (!address) return <p className="muted">Contribution is off, so the bundle carries no address for corrections.</p>;
  if (settings.public_url_is_loopback !== true) return <p className="muted">Agents that clone this repository will send corrections to <code>{address}</code>.</p>;
  return <Notice kind="info"><strong>Set a reachable address before you publish to Git.</strong> Agents that clone this repository would be told to send corrections to <code>{address}</code>. That address only works on this computer, so Git publication is refused. On the computer that runs OMS, set <code>OMS_PUBLIC_URL</code> to an address other computers can reach, restart OMS, then return here. With the Docker launcher, add <code>OMS_PUBLIC_URL=https://your-address</code> to <code>.env.local</code> beside the database password. The Docker stack also binds its ports to this computer only; see the Community README before opening them.</Notice>;
}

export function PublicationDestination({ settings }: { settings: CoreSettings }) {
  const git = settings.publication_target === "git";
  return <dl className="details"><div><dt>Destination</dt><dd>{git ? "Git repository" : "Local folder"}</dd></div><div><dt>{git ? "Repository" : "Folder on your computer"}</dt><dd>{git ? settingText(settings, "publication_git_url", "Not configured") : settingText(settings, "publish_host_path", "Host folder is not mapped")}</dd></div>{git && <div><dt>Branch</dt><dd>{settingText(settings, "publication_git_branch", "main")}</dd></div>}{settings.publish_in_container === true && !git && <div><dt>Folder inside Docker</dt><dd>{settingText(settings, "publish_root")}</dd></div>}</dl>;
}

export function PublicationInstall({ settings, published = false }: { settings: CoreSettings; published?: boolean }) {
  const git = settings.publication_target === "git";
  const localPath = settingText(settings, "publish_host_path");
  const remote = settingText(settings, "publication_git_url");
  if (settings.publication_ready === false || (git ? !remote : !localPath)) {
    return <Notice kind="info">{git ? "Choose a Git repository in Set up publication before connecting your agents." : "Connect a host folder in Set up publication before installing. The installer needs the folder on your computer."}</Notice>;
  }
  const install = git
    ? gitInstallCommand(remote, settingText(settings, "publication_git_branch", "main"))
    : `sh ${shellQuote(`${localPath}/install.sh`)}`;
  return <section aria-label="Agent installation">
    <h3>{git ? "Connect your agents to the published bundle" : "Install on your computer"}</h3>
    {!published && <p className="muted">If you have already published to this destination, you can rerun this command to reconnect an agent. Otherwise, publish the workspace first.</p>}
    {git && <GitReaderAccess settings={settings} />}
    <p>{git ? "Run this in a terminal on the computer where your agents run." : "Run this in a terminal on the computer hosting your publication folder, as the user who runs your agents."} The installer detects supported agents and updates their user settings.</p>
    <CommandBlock key={install} label="Install command" command={install} />
    {git && <p className="muted">Already cloned this repository? Run its existing <code>install.sh</code> instead of cloning it again. The clone command stops if its destination already exists.</p>}
    <details><summary>Automatic updates and agent setup</summary>
      <p>{git ? "The installer schedules a Git pull and refresh every three hours. For an immediate update, run sh ~/.oms/oms-refresh.sh." : "The installer checks for completed local publications every minute, updating skill links and copied instructions automatically. Git is not required."}</p>
      <p className="muted">Check the installer’s final message to confirm scheduling succeeded. Restart or reload an agent session if it has already cached its guidance. Cursor's global User Rules still require pasting the staged text shown by the installer.</p>
    </details>
  </section>;
}

export default function PublicationSetup({ settings, onSaved, onHostEditing, onPublished }: { settings: CoreSettings; onSaved?: (value: CoreSettings) => void; onHostEditing?: (value: boolean) => void; onPublished?: (value: PublicationResult) => void }) {
  const { api } = useWorkspace();
  const action = useAction();
  const [step, setStep] = useState(0);
  const [config, setConfig] = useState(settings);
  const [target, setTarget] = useState(settingText(settings, "publication_target", "local"));
  const [folder, setFolder] = useState(settingText(settings, "publication_folder"));
  const [remote, setRemote] = useState(settingText(settings, "publication_git_url"));
  const [branch, setBranch] = useState(settingText(settings, "publication_git_branch", "main"));
  const [result, setResult] = useState<PublicationResult | null>(null);
  const [changingHost, setChangingHost] = useState(false);
  function hostEditing(value: boolean) { setChangingHost(value); onHostEditing?.(value); }
  function chooseTarget(value: string) { setTarget(value); hostEditing(false); }
  const git = config.publication_target === "git";
  const localPath = settingText(config, "publish_host_path");
  async function save(event: FormEvent) {
    event.preventDefault();
    const ok = await action.run(async () => {
      const updated = await api.updateSettings({ publication_target: target, publication_folder: folder,
        publication_git_url: remote.trim(), publication_git_branch: branch.trim() });
      setConfig(updated); onSaved?.(updated); setResult(null);
    }, "Publication destination saved.");
    if (ok) setStep(1);
  }
  return <Panel title="Set up publication"><p className="muted">Choose where guidance is published, then connect the agents on your computer.</p>
    <ol aria-label="Publication setup steps" className="actions" style={{ paddingLeft: 20, marginBottom: 24 }}>{["Destination", target === "git" ? "Git access" : "Prepare", "Publish", "Install"].map((label, index) => <li key={label} aria-current={step === index ? "step" : undefined} style={{ paddingRight: 18, color: step === index ? "var(--text)" : "var(--text-dim)" }}>{label}</li>)}</ol>
    <Feedback action={action} />
    {step === 3 && git && result?.pushed === false && <Notice kind="info">The bundle was already up to date, so no push was needed. This run did not verify write permission.</Notice>}
    {step === 0 && <form onSubmit={(event) => void save(event)} className="form-grid">
      <fieldset><legend>Where should your skills go?</legend><div className="checkbox-grid"><label className="check-label"><input type="radio" name="publication-target" value="local" checked={target === "local"} onChange={() => chooseTarget("local")} />A folder on this computer</label><label className="check-label"><input type="radio" name="publication-target" value="git" checked={target === "git"} onChange={() => chooseTarget("git")} />A Git repository</label></div></fieldset>
      {target === "local" ? <>{config.publish_in_container === true && <HostPublicationFolder settings={config} onEditing={hostEditing} onChanged={updated => { setConfig(updated); onSaved?.(updated); setResult(null); }} />}
        <label>Subfolder (optional)<input value={folder} placeholder="Leave empty to publish directly in the folder above" onChange={(event) => setFolder(event.target.value)} aria-describedby="publication-folder-help" /></label><p id="publication-folder-help" className="muted">Publish directly in <code>{settingText(config, "publish_host_root") || settingText(config, "publish_base")}</code>, or add a subfolder such as <code>my-skills</code>.</p></> : <><label>Git repository URL<input type="text" required placeholder="https://github.com/your-account/oms-skills.git" value={remote} onChange={(event) => setRemote(event.target.value)} /></label><label>Branch<input required value={branch} onChange={(event) => setBranch(event.target.value)} /></label><ContributionAddress settings={config} /><p className="muted">Use a dedicated repository for generated guidance. Enter an HTTPS or SSH URL without a token or password. Git credentials are configured on the Community server.</p></>}
      <Button type="submit" disabled={action.busy || (target === "local" && changingHost)}>{action.busy ? "Saving…" : "Save and continue"}</Button>
    </form>}
    {step === 1 && <><PublicationDestination settings={config} />{git ? <GitPublisherAccess settings={config} onUseHttps={url => { setRemote(url); setStep(0); }} /> : <><h3>Keep the folder available to your agents</h3><p>OMS writes the generated bundle here on every publication. Your agents link to this folder, so keep it in place and run the installer on the computer where the agents run.</p>{!localPath && <Notice kind="info">Map a host directory before continuing. See the Docker publication instructions in the Community README.</Notice>}</>}
      <div className="actions"><Button variant="secondary" onClick={() => setStep(0)}>Back</Button><Button onClick={() => setStep(2)} disabled={!git && !localPath}>Continue to publish</Button></div></>}
    {step === 2 && <><PublicationDestination settings={config} /><p>Publish the approved skills, constraints and supporting files. Pending corrections remain in the inbox.</p>{git && <p>OMS will create the publication commit and push to <strong>{settingText(config, "publication_git_branch", "main")}</strong>. Pushing a new commit checks write access. An empty repository is ready to use; you do not need to create its first commit yourself.</p>}{git && action.error && <p>Check that the token selects this repository, has <strong>Contents: Read and write</strong>, has not expired and has any required organisation approval. Return to Git access to replace it, then retry. Branch protection may also prevent a direct push.</p>}<div className="actions"><Button variant="secondary" disabled={action.busy} onClick={() => setStep(1)}>{git ? "Back to Git access" : "Back"}</Button><Button disabled={action.busy} onClick={() => void action.run(async () => { const value = await api.publish(); setResult(value); onPublished?.(value); setStep(3); }, "Workspace published.")}>{action.busy ? "Publishing…" : git ? "Publish and push" : "Publish bundle"}</Button></div></>}
    {step === 3 && <><PublicationInstall settings={config} published /><div className="actions"><Button variant="secondary" onClick={() => setStep(0)}>Change destination</Button></div>{result && <p className="muted">Published {Array.isArray(result.skills) ? result.skills.length : result.skills} skills.</p>}</>}
  </Panel>;
}
