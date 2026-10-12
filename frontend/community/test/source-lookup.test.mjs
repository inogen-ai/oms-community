import test from "node:test";
import assert from "node:assert/strict";
import { createElement as h } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import * as ui from "@inogen/oms-ui-core";

function nodes(node) {
  if (Array.isArray(node)) return node.flatMap(nodes);
  if (!node || typeof node !== "object") return [];
  if (typeof node.type === "function") return nodes(node.type(node.props));
  return [node, ...nodes(node.props?.children)];
}
function text(node) {
  if (Array.isArray(node)) return node.map(text).join("");
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node !== "object") return String(node);
  if (typeof node.type === "function") return text(node.type(node.props));
  return text(node.props?.children);
}
const call = (name, props) => ui[name](props);
const button = (tree, label) => nodes(tree).find(node => node.type === "button" && text(node) === label);
const labelled = (tree, label) => nodes(tree).find(node => node.type === "label" && text(node).startsWith(label));
const form = (value, props = {}) => call("SourceLookupForm", { value: { ...ui.sourceLookupValue(), ...value }, onChange() {}, onSubmit() {}, ...props });

test("lookup body omits empty ref, folder and profile and trims what it sends", () => {
  assert.deepEqual(ui.sourceLookupValue("https://github.com/o/r"), { url: "https://github.com/o/r", kind: "", name: "", path: "", profile: "" });
  assert.deepEqual(ui.sourceLookupBody({ ...ui.sourceLookupValue(" https://github.com/o/r "), name: "ignored without a kind" }), { url: "https://github.com/o/r" });
  assert.deepEqual(ui.sourceLookupBody({ url: "u", kind: "tag", name: " v1 ", path: " tools/a ", profile: " work " }),
    { url: "u", ref_kind: "tag", ref_name: "v1", package_path: "tools/a", credential_profile_id: "work" });
});

test("lookup form shows the name field only for a chosen kind and carries the agreed option text", () => {
  assert.equal(labelled(form({}), "Branch, tag or commit"), undefined);
  assert.ok(labelled(form({ kind: "tag", name: "v1" }), "Branch, tag or commit"));
  const select = nodes(form({})).find(node => node.type === "select");
  assert.deepEqual(select.props.children.map(option => [option.props.value, text(option)]), [
    ["", "Default branch (recommended)"], ["branch", "A branch: follow new commits on it"],
    ["tag", "A tag: stay on this tag; if the tag is moved, the next check picks it up"], ["commit", "A commit: pin exactly this commit; it never updates"]]);
  assert.ok(labelled(form({}), "What to follow"));
  assert.ok(labelled(form({}), "GitHub repository or skill folder URL"));
});

test("lookup form opens Advanced when it holds a value, never closes it, and blocks incomplete submits", () => {
  const advanced = tree => nodes(tree).find(node => node.type === "details");
  const render = (element, value) => advanced(form(value)).props.ref(element);
  // Opens on the first value, is not forced open again after the user closes it while the value persists,
  // opens again when a value returns after being cleared, and no render ever sets open to false.
  const element = { open: false }, writes = [];
  Object.defineProperty(element, "open", { get: () => element._open ?? false, set: value => { writes.push(value); element._open = value; } });
  render(element, {}); assert.equal(element.open, false);
  render(element, { kind: "tag", name: "v1" }); assert.equal(element.open, true);
  element.open = false; writes.length = 0;
  render(element, { kind: "tag", name: "v1", url: "u" }); render(element, { kind: "tag", name: "v1", url: "ur" });
  assert.equal(element.open, false); assert.deepEqual(writes, []);
  render(element, {}); assert.equal(element.open, false);
  render(element, { path: "tools" }); assert.equal(element.open, true);
  assert.deepEqual(writes, [true]);
  // Two instances do not share state.
  const first = { open: false }, second = { open: false };
  render(first, { profile: "work" }); render(second, { profile: "work" });
  assert.deepEqual([first.open, second.open], [true, true]);
  // A user-opened section stays open when the form value is empty, and no `open` prop is passed.
  const user = { open: true }; render(user, {}); assert.equal(user.open, true);
  assert.equal("open" in advanced(form({ kind: "branch" })).props, false);
  assert.doesNotThrow(() => advanced(form({})).props.ref(null));
  assert.equal(button(form({}), "Discover packages").props.disabled, true);
  assert.equal(button(form({ url: "u" }), "Discover packages").props.disabled, false);
  assert.equal(button(form({ url: "u", kind: "tag" }), "Discover packages").props.disabled, true);
  assert.equal(button(form({ url: "u", kind: "tag", name: " " }), "Discover packages").props.disabled, true);
  assert.equal(button(form({ url: "u", kind: "tag", name: "v1" }, { submitLabel: "Discover skill folders" }), "Discover skill folders").props.disabled, false);
  assert.ok(button(form({ url: "u" }, { busy: true }), "Discovering…").props.disabled);
});

test("lookup form submits the built body and reports edits as a whole value", () => {
  let sent, changed, prevented = false;
  const tree = form({ url: " u ", kind: "commit", name: "abc" }, { onSubmit: body => { sent = body; }, onChange: value => { changed = value; } });
  nodes(tree).find(node => node.type === "form").props.onSubmit({ preventDefault() { prevented = true; } });
  assert.ok(prevented); assert.deepEqual(sent, { url: "u", ref_kind: "commit", ref_name: "abc" });
  nodes(tree).find(node => node.type === "input" && node.props.type === "url").props.onChange({ target: { value: "v" } });
  assert.deepEqual(changed, { url: "v", kind: "commit", name: "abc", path: "", profile: "" });
});

test("the three profile modes render their agreed copy and an unset mode renders no profile row", () => {
  const text_ = text(form({}, { profile: { mode: "text" } }));
  assert.match(text_, /Access profile for a private repository \(optional\)/);
  assert.match(text_, /If this OMS server has an access profile configured for private repositories, enter its id here\. The profile holds the GitHub token on the server; the token never appears in OMS\./);
  const select = text(form({}, { profile: { mode: "select", profiles: ["work"] } }));
  assert.match(select, /Private repository access: choose a profile/); assert.match(select, /Public repository/); assert.match(select, /work/);
  const none = "Importing from a private repository needs an access profile. Ask your administrator.";
  assert.ok(text(form({}, { profile: { mode: "none" } })).includes(none));
  assert.ok(text(form({}, { profile: { mode: "select", profiles: [] } })).includes(none));
  const absent = text(form({}));
  for (const fragment of ["Access profile", "Private repository access", "private repositories"]) assert.ok(!absent.includes(fragment));
});

test("the pending notice uses the import wording for imports and the request wording otherwise", () => {
  let checked = 0;
  const imported = call("SourcePendingNotice", { subject: "import", onCheck: () => { checked++; } });
  assert.ok(text(imported).includes("A previous import did not confirm. Your browser sent the request but did not get the answer. Check its result before trying again; checking does not start a new import."));
  assert.ok(text(call("SourcePendingNotice", { onCheck() {} })).includes("A previous request did not confirm. Your browser sent it but did not get the answer. Check its result before trying again; checking does not start a new request."));
  button(imported, "Check result").props.onClick(); assert.equal(checked, 1);
  assert.equal(button(call("SourcePendingNotice", { onCheck() {}, busy: true }), "Check result").props.disabled, true);
  for (const path of ["/api/skill-source-discoveries", "/api/skill-source-installations?x=1", "/api/skill-local-imports", "/api/skill-local-imports?a=b"]) assert.equal(ui.sourcePendingSubject(path), "import");
  assert.equal(ui.sourcePendingSubject("/api/skill-sources/s/check"), "request");
});

test("the change confirmation words remove and relocate, lists skills as inert text and guards busy", () => {
  const skills = [{ id: "a", name: "Alpha" }, { id: "b", name: "<script>alert(1)</script>" }];
  let confirmed = 0, cancelled = 0;
  const props = { skills, onConfirm: () => { confirmed++; }, onCancel: () => { cancelled++; } };
  const remove = call("SourceChangeConfirmation", { ...props, action: "remove" });
  assert.ok(text(remove).includes("Warning: This action will remove the source from all skills that are part of this repository. Do you still want to remove it?"));
  const move = call("SourceChangeConfirmation", { ...props, action: "relocate", destination: "https://github.com/new/home" });
  assert.ok(text(move).includes("Warning: This action will move the source for all skills that are part of this repository to https://github.com/new/home. Do you still want to move it?"));
  assert.match(text(move), /Affected skills \(2\)/);
  const html = renderToStaticMarkup(h(ui.SourceChangeConfirmation, { ...props, action: "remove" }));
  assert.ok(!html.includes("<script>")); assert.match(html, /&lt;script&gt;/);
  assert.match(html, /role="alertdialog"/); assert.match(html, /aria-label="Remove source confirmation"/);
  assert.match(renderToStaticMarkup(h(ui.SourceChangeConfirmation, { ...props, action: "relocate", destination: "x" })), /aria-label="Change repository URL confirmation"/);
  button(remove, "Yes").props.onClick(); button(remove, "No").props.onClick();
  assert.deepEqual([confirmed, cancelled], [1, 1]);
  assert.equal(button(remove, "Yes").props.className.includes("danger"), true);
  assert.equal(button(move, "Yes").props.className.includes("primary"), true);
  nodes(remove)[0].props.onKeyDown({ key: "Escape", stopPropagation() {} }); assert.equal(cancelled, 2);
  const busy = call("SourceChangeConfirmation", { ...props, action: "remove", busy: true });
  assert.ok(button(busy, "Yes").props.disabled && button(busy, "No").props.disabled);
  nodes(busy)[0].props.onKeyDown({ key: "Escape", stopPropagation() {} }); assert.equal(cancelled, 2);
});

test("the GitHub mark escapes its title, keeps clear space and is decorative unless titled", () => {
  const titled = renderToStaticMarkup(h(ui.GitHubMark, { title: "GitHub: https://github.com/o/r?a=1&b=<2>" }));
  assert.match(titled, /alt="GitHub: https:\/\/github\.com\/o\/r\?a=1&amp;b=&lt;2&gt;"/);
  assert.match(titled, /title="GitHub: https:\/\/github\.com\/o\/r\?a=1&amp;b=&lt;2&gt;"/);
  assert.match(titled, /width="16"/); assert.match(titled, /height="16"/); assert.match(titled, /padding:4px/);
  assert.match(titled, /src="[^"]*\/GitHub_Invertocat_White\.svg"/); assert.match(titled, /class="oms-github-mark"/);
  const large = renderToStaticMarkup(h(ui.GitHubMark, { title: "GitHub", size: 24 }));
  assert.match(large, /width="25"/); assert.match(large, /height="24"/); assert.match(large, /padding:6px/);
  const decorative = renderToStaticMarkup(h(ui.GitHubMark));
  assert.match(decorative, /alt=""/); assert.match(decorative, /aria-hidden="true"/); assert.doesNotMatch(decorative, /title=/);
});

test("the display URL drops a trailing .git and nothing else", () => {
  assert.equal(ui.sourceDisplayUrl("https://github.com/o/r.git"), "https://github.com/o/r");
  assert.equal(ui.sourceDisplayUrl("https://github.com/o/r"), "https://github.com/o/r");
  assert.equal(ui.sourceDisplayUrl("https://github.com/o/r.github"), "https://github.com/o/r.github");
});
