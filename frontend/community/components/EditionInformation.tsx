"use client";
import { useRef, useState, useSyncExternalStore, type RefObject } from "react";
import { X } from "lucide-react";
import { Panel } from "@inogen/oms-ui-core";
import EditionPreview from "./EditionPreview";

const dismissalKey = "oms-community-automation-tip-dismissed";
const changeEvent = "oms-community-edition-tip-change";
let dismissedInMemory: boolean | null = null;
function isDismissed() {
  if (dismissedInMemory !== null) return dismissedInMemory;
  try { return window.localStorage.getItem(dismissalKey) === "true"; }
  catch { return false; }
}
function subscribe(listener: () => void) {
  window.addEventListener("storage", listener);
  window.addEventListener(changeEvent, listener);
  return () => { window.removeEventListener("storage", listener); window.removeEventListener(changeEvent, listener); };
}
function setDismissed(value: boolean) {
  dismissedInMemory = value;
  try { window.localStorage.setItem(dismissalKey, String(value)); dismissedInMemory = null; } catch { /* Keep the preference for this page session when storage is blocked. */ }
  window.dispatchEvent(new Event(changeEvent));
}
function useTipDismissed() { return useSyncExternalStore(subscribe, isDismissed, () => true); }

function EditionInformationLink({ compare = false, buttonRef }: { compare?: boolean; buttonRef?: RefObject<HTMLButtonElement | null> }) {
  const [open, setOpen] = useState(false);
  const ownTrigger = useRef<HTMLButtonElement>(null);
  const trigger = buttonRef ?? ownTrigger;
  return <><button ref={trigger} type="button" className="edition-info-link" aria-haspopup="dialog" onClick={() => setOpen(true)}>{compare ? "Compare editions" : "Explore automatic processing"}</button>
    {open && <EditionPreview topic="automation" compare={compare} onClose={() => { setOpen(false); trigger.current?.focus(); }} />}</>;
}

export function CorrectionAutomationHint({ onDismiss }: { onDismiss: () => void }) {
  const dismissed = useTipDismissed();
  if (dismissed) return null;
  return <aside className="edition-automation-hint" aria-label="Paid correction automation">
    <p><span className="edition">Paid editions</span> Distil corrections into reusable rules, match them to skills and apply eligible updates automatically. <EditionInformationLink /></p>
    <button type="button" className="edition-hint-dismiss" aria-label="Dismiss paid automation tip" onClick={() => { setDismissed(true); onDismiss(); }}><X size={16} aria-hidden="true" /></button>
  </aside>;
}

export function AboutEdition() {
  const dismissed = useTipDismissed();
  const explore = useRef<HTMLButtonElement>(null);
  return <Panel title="About your edition">
    <p><strong>Community</strong> · Edit, review and publish your skills with manual control.</p>
    <p className="muted">Paid editions add automatic correction processing: distil reusable rules, reconcile existing guidance and incorporate eligible changes into skills. They also add team access and review policies.</p>
    <div className="edition-info-actions"><EditionInformationLink buttonRef={explore} /><EditionInformationLink compare /></div>
    {dismissed && <button type="button" className="edition-info-link edition-info-restore" onClick={() => { setDismissed(false); explore.current?.focus(); }}>Show the correction inbox tip again</button>}
  </Panel>;
}
