"use client";
import { useState } from "react";
import type { CoreSettings } from "@inogen/oms-client";
import { Button, Notice } from "@inogen/oms-ui-core";

export const shellQuote = (value: string) => "'" + value.replaceAll("'", "'\\''") + "'";
const text = (settings: CoreSettings, key: string) => typeof settings[key] === "string" ? String(settings[key]) : "";

export function githubRepository(remote: string) {
  const path = remote.match(/^(?:https:\/\/github\.com\/|ssh:\/\/git@github\.com\/|git@github\.com:)([\w.-]+)\/([\w.-]+)\/?$/i);
  if (!path) return null;
  const owner = path[1], repository = path[2].replace(/\.git$/, "");
  return { owner, repository, url: `https://github.com/${owner}/${repository}.git` };
}

export function tokenLink(remote: string, access: "read" | "write") {
  const repo = githubRepository(remote);
  const query = new URLSearchParams({ name: `OMS Community ${access === "write" ? "publisher" : "reader"}`, contents: access });
  if (repo) query.set("target_name", repo.owner);
  return `https://github.com/settings/personal-access-tokens/new?${query}`;
}

export function gitInstallCommand(remote: string, branch: string) {
  const directory = remote.replace(/^[a-z]+:\/\//, "").replace(/^git@/, "").replace(/\.git\/?$/, "").replace(/[^a-zA-Z0-9._-]+/g, "-").slice(0, 180);
  const checkout = `"$HOME/.oms/bundles/${directory}"`;
  return `mkdir -p "$HOME/.oms/bundles" &&\ngit clone --branch ${shellQuote(branch)} -- ${shellQuote(remote)} ${checkout} &&\nsh ${checkout.slice(0, -1)}/install.sh"`;
}

export function CommandBlock({ label, command }: { label: string; command: string }) {
  const [copied, setCopied] = useState("");
  async function copy() {
    try { await navigator.clipboard.writeText(command); setCopied("Command copied."); }
    catch { setCopied("Select and copy the command above."); }
  }
  return <div className="publication-command"><pre className="small-json" aria-label={label}><code>{command}</code></pre><Button variant="secondary" onClick={() => void copy()}>Copy {label.toLowerCase()}</Button>{copied && <p role="status">{copied}</p>}</div>;
}

export function GitPublisherAccess({ settings, onUseHttps }: { settings: CoreSettings; onUseHttps: (url: string) => void }) {
  const remote = text(settings, "publication_git_url");
  const repo = githubRepository(remote);
  const docker = settings.publish_in_container === true;
  const [manualContainer, setManualContainer] = useState("");
  const detected = text(settings, "publish_container_id");
  const container = /^[a-f0-9]{12,64}$/.test(detected) ? detected : manualContainer;
  const ready = !docker || /^[a-f0-9]{12,64}$/.test(container);
  const prefix = docker ? `docker exec -it ${shellQuote(container)} ` : "";
  const command = `${prefix}oms git-login --repository ${shellQuote(remote)}`;
  const https = remote.startsWith("https://");
  return <section aria-label="Publisher Git access" className="publication-access">
    <h3>Connect OMS to your repository</h3>
    <p>Your repository address is saved. Now let OMS push updates to it. You will connect your agents after the first publication.</p>
    {https ? <><h4>1. Create a publishing token</h4>
      {repo ? <><p>The repository must already exist; an empty repository is fine. Open GitHub using the link below. It preselects the owner and write permission; you still need to select the repository.</p>
        <a href={tokenLink(remote, "write")} target="_blank" rel="noreferrer">Create a GitHub publishing token ↗</a>
        <dl className="details"><div><dt>Resource owner</dt><dd>{repo.owner}</dd></div><div><dt>Repository access</dt><dd><strong>Only select repositories</strong> → {repo.repository}</dd></div><div><dt>Repository permissions</dt><dd><strong>Contents: Read and write</strong></dd></div></dl>
        <p>Choose an expiration date, select <strong>Generate token</strong>, then copy the token for the next step. If your organisation requires approval, wait for approval before publishing.</p></> : <p>Create an HTTPS access token with your Git provider, restricted to this repository and allowed to read and write its contents. Copy it for the next step.</p>}
      <h4>2. Connect the Community publisher</h4></> : <><h4>Use your existing SSH connection</h4><p>The publisher needs its own SSH key with write access to this repository and a verified host key for your Git provider.</p>{repo && <p><Button variant="secondary" onClick={() => onUseHttps(repo.url)}>Use guided HTTPS setup</Button></p>}</>}
    <p>{docker ? "Run the command below in Terminal on the computer running Docker. It connects to the API container serving this workspace; you do not need to change directory." : "Run the command below on the machine running Community, as the same operating-system user and in the same Python environment as the OMS server."}</p>
    {docker && !detected && <><Notice kind="info">This container uses a custom hostname. Find this workspace’s API container in Docker, then enter its container ID below.</Notice><CommandBlock label="Container lookup" command={'docker ps --format "table {{.ID}}\\t{{.Names}}\\t{{.Ports}}"'} /><label>API container ID<input value={manualContainer} onChange={event => setManualContainer(event.target.value.trim())} placeholder="The API container for this workspace" /></label></>}
    {ready && <CommandBlock label={https ? "Publisher login command" : "Publisher read check"} command={https ? command : `${command} --check`} />}
    {https && <><ol><li>At <strong>Git username</strong>, enter your Git account username{repo ? " (your personal username, even when the repository belongs to an organisation)" : ""}.</li><li>At <strong>Repository access token</strong>, paste the token and press Enter. The token stays hidden while you type or paste.</li><li>Wait for <strong>Git credentials saved for this repository</strong> and <strong>Repository read check passed</strong>.</li></ol>
      <p className="muted">{docker ? "The token is saved unencrypted with owner-only file permissions in this workspace’s persistent Docker git-config volume." : "The token is saved unencrypted with owner-only file permissions in the server user’s home directory."} It is never saved in browser settings or the published repository. Repeat this command to replace an expired or revoked token.</p></>}
    <h4>{https ? "3. " : ""}Continue to publication</h4><p>The read check does not verify write permission; public repositories can be read without a valid token. Continue below, then choose <strong>Publish and push</strong> to create the bundle and verify write access.</p>
    {https && ready && <details><summary>Already configured Git access?</summary><p>Keep your existing credential helper or SSH setup. For this HTTPS address, check the publisher’s existing access without entering a new token:</p><CommandBlock label="Publisher read check" command={`${command} --check`} /></details>}
  </section>;
}

export function GitReaderAccess({ settings }: { settings: CoreSettings }) {
  const [visibility, setVisibility] = useState("private");
  const remote = text(settings, "publication_git_url");
  const github = githubRepository(remote);
  const https = remote.startsWith("https://");
  return <section aria-label="Computer Git access" className="publication-access">
    <h4>1. Check access from your agent’s computer</h4>
    <p>Run these commands on each computer where your agents run. This is a separate Git connection from the publisher in Docker.</p>
    <fieldset><legend>Who can read your repository?</legend><div className="checkbox-grid"><label className="check-label"><input type="radio" name="repository-visibility" checked={visibility === "private"} onChange={() => setVisibility("private")} />Private repository</label><label className="check-label"><input type="radio" name="repository-visibility" checked={visibility === "public"} onChange={() => setVisibility("public")} />Public repository</label></div></fieldset>
    <p>{visibility === "public" && https ? "No Git login or token is needed to download a public repository over HTTPS." : "If you already use this repository from this computer, your existing Git login may be enough. This connection only needs read access; do not copy the publisher’s write token out of Docker."}</p>
    <CommandBlock label="Computer read check" command={`git ls-remote -- ${shellQuote(remote)}`} />
    <p className="muted">Success lists commit IDs and branch names without an error. {visibility === "public" && https ? "If access is refused, check the URL and whether the repository is actually private." : "If access is refused, use the sign-in instructions below, then repeat this check."}</p>
    {(visibility === "private" || !https) && <details><summary>Help this computer sign in</summary>
      {github && https ? <><p>If you use GitHub CLI, sign in with an account that can read <strong>{github.owner}/{github.repository}</strong>, then configure Git to use that login. <a href="https://docs.github.com/en/get-started/git-basics/caching-your-github-credentials-in-git" target="_blank" rel="noreferrer">Install GitHub CLI or a credential manager ↗</a></p><CommandBlock label="Computer GitHub login" command="gh auth login --hostname github.com --git-protocol https --web &&\ngh auth setup-git --hostname github.com" />
        <p>Alternatively, create a separate <a href={tokenLink(remote, "read")} target="_blank" rel="noreferrer">read-only token</a>: select <strong>Only select repositories → {github.repository}</strong> and <strong>Contents: Read-only</strong>. Save it with your computer’s Git credential manager so scheduled pulls can run without prompts.</p></> : <p>For SSH, add this computer’s public SSH key to an account or deploy key that can read the repository, and verify your provider’s SSH host fingerprint. For HTTPS, sign in through your provider’s Git credential manager using read access. Repeat the read check above before installing.{github && <> <a href="https://docs.github.com/en/authentication/connecting-to-github-with-ssh" target="_blank" rel="noreferrer">GitHub SSH setup instructions ↗</a></>}</p>}
    </details>}
    <h4>2. Clone the bundle and install it</h4>
  </section>;
}
