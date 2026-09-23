"use client";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, ArrowUpRight, Check, FolderCheck, ShieldCheck, UserRound, UsersRound, X } from "lucide-react";
import { Button } from "@inogen/oms-ui-core";

// Keep this summary local. The pricing page owns current terms and availability.
const editions = [
  {
    id: "community", name: "Community", label: "Local control", icon: UserRound,
    audience: "For individuals managing their own agent guidance.",
    price: "Free", period: "No time limit",
    terms: "No model key needed. No limits on local skills or rules.",
    billing: "Local processing. No model charges.",
    includes: "Your everyday essentials",
    features: ["Review corrections and edit skills yourself", "Trace rules to their source and restore changes", "Publish to local folders or Git"],
  },
  {
    id: "pro", name: "Pro", label: "Team learning", icon: UsersRound,
    audience: "For teams turning everyday corrections into shared guidance.",
    price: "£199", period: "per month",
    terms: "Founding customers: £149/month for the first 12 months.",
    billing: "BYOK. Model usage billed separately.",
    includes: "Everything in Community, plus",
    features: ["Reconcile corrections and conflicting rules automatically", "Share the right guidance with each contributor", "Keep agents current with scheduled distribution"],
    href: "https://www.inogen.ai/#register", action: "Discuss Pro",
  },
  {
    id: "enterprise", name: "Enterprise", label: "Organisational control", icon: ShieldCheck,
    audience: "For organisations with specific governance and deployment needs.",
    price: "Bespoke", period: "Priced by agreement",
    terms: "A package shaped around your deployment and support needs.",
    billing: "BYOK. Model usage billed separately.",
    includes: "Everything in Pro, plus agreed controls",
    features: ["Give multiple teams their own separate skills", "Set advanced policies and publication approvals", "Run OMS in your infrastructure, by agreement"],
    href: "https://www.inogen.ai/contact", action: "Discuss Enterprise",
  },
];

// Community-owned information only: opening this preview must not navigate
// away from a draft, load commercial services or change runtime capabilities.
export default function EditionPreview({ onClose, topic = "teams", compare = false }: { onClose: () => void; topic?: "teams" | "automation"; compare?: boolean }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const [comparison, setComparison] = useState(compare);
  const topicTitle = topic === "automation" ? "Automatic correction processing" : "People and teams";

  useEffect(() => {
    if (!dialog.current?.open) dialog.current?.showModal();
    heading.current?.focus();
    if (body.current) body.current.scrollTop = 0;
  }, [comparison]);

  const close = () => dialog.current?.close();
  return <dialog ref={dialog} className={`edition-preview${comparison ? " edition-preview--comparison" : ""}`} aria-labelledby="edition-preview-heading" onClose={onClose}
    onKeyDown={(event) => {
      if (event.key !== "Tab") return;
      const controls = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], [tabindex="0"]'));
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === heading.current)) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first?.focus();
      }
    }}>
    <header className="edition-preview__header">
      <div>
        <span className="edition-preview__eyebrow">{comparison ? "OMS editions" : "Available in paid editions"}</span>
        <h2 ref={heading} tabIndex={-1} id="edition-preview-heading">{comparison ? "Compare editions" : topicTitle}</h2>
      </div>
      <button type="button" className="edition-preview__close" aria-label="Close paid features preview" onClick={close}><X size={19} aria-hidden="true" /></button>
    </header>
    <div ref={body} className="edition-preview__body" tabIndex={0} role="region" aria-label={comparison ? "Edition comparison details" : topic === "automation" ? "Paid correction processing" : "Paid team features"}>
      {comparison ? <>
        <div className="edition-preview__hero">
          <p className="edition-preview__headline">Make each correction <span>go further.</span></p>
          <p className="edition-preview__intro">Manage your own skills, help your team learn together, or control how guidance reaches your organisation.</p>
        </div>
        <div className="edition-comparison">
          {editions.map(({ id, name, label, icon: Icon, audience, price, period, terms, billing, includes, features, href, action }) =>
            <section key={id} className="edition-plan" aria-labelledby={`${id}-edition-title`}>
              <div className="edition-plan__label"><Icon size={18} aria-hidden="true" />{label}</div>
              <h3 id={`${id}-edition-title`}>{name}</h3>
              <p className="edition-plan__audience">{audience}</p>
              <div className="edition-plan__price"><strong>{price}</strong><span>{period}</span></div>
              <p className="edition-plan__terms">{terms}</p>
              <p className="edition-plan__billing">{billing}</p>
              <p className="edition-plan__includes">{includes}</p>
              <ul>{features.map(feature => <li key={feature}><Check size={15} aria-hidden="true" /><span>{feature}</span></li>)}</ul>
              {href
                ? <a className="edition-plan__action" href={href} target="_blank" rel="noopener noreferrer" aria-label={`${action} (opens in a new tab)`}>{action}<ArrowUpRight size={15} aria-hidden="true" /></a>
                : <span className="edition-plan__current"><Check size={15} aria-hidden="true" />Your current edition</span>}
            </section>)}
        </div>
        <div className="edition-preview__ownership">
          <FolderCheck size={21} aria-hidden="true" />
          <div><h3>Your skills stay yours, whichever edition you choose.</h3><p>Keep your published files. Installed skills remain usable without a live OMS connection.</p></div>
        </div>
        <p className="edition-preview__terms">Pro and Enterprise use your own model key (BYOK); your provider bills model usage separately. Review policies determine which changes need approval. Paid edition availability and Enterprise scope are confirmed in discussion.</p>
      </> : topic === "automation" ? <>
        <p className="edition-preview__intro">Community lets you choose where a correction belongs and edit the guidance yourself. Paid editions can process that work with models, from the submitted correction to updated skills.</p>
        <ol className="edition-preview__steps">
          <li><h3>Distil a reusable rule</h3><p>Extract durable guidance from the correction, filtering out one-off requests that do not belong in a skill.</p></li>
          <li><h3>Find the relevant skills</h3><p>Use the meaning of the correction and its context to match it to skills in your library.</p></li>
          <li><h3>Reconcile and revise guidance</h3><p>Compare existing rules for duplicates or contradictions, and prepare revisions to affected passages so the correction can become part of the skill.</p></li>
          <li><h3>Apply updates or request review</h3><p>Apply eligible changes automatically. Send changes that need a decision to the review inbox, with processing progress and history available.</p></li>
        </ol>
        <ProcessingConditions />
      </> : <>
        <p className="edition-preview__intro">Give people access to the guidance relevant to their work, and choose how their contributions are reviewed.</p>
        <ul className="edition-preview__features">
          <li><UserRound size={22} aria-hidden="true" /><div><h3>Know who contributed</h3><p>Connect corrections to contributor identities and manage enrolment and access.</p></div></li>
          <li><UsersRound size={22} aria-hidden="true" /><div><h3>Give teams the right access</h3><p>Set which guidance each team can read.</p></div></li>
          <li><ShieldCheck size={22} aria-hidden="true" /><div><h3>Set trust and review policies</h3><p>Choose how contributions from different people are handled, with review where your policies require it.</p></div></li>
        </ul>
        <p className="edition-preview__note">Your Community workspace already supports editing, history, local or Git publishing, and keeping installed agents up to date.</p>
      </>}
    </div>
    <footer className="edition-preview__footer">
      {comparison
        ? <Button variant="secondary" onClick={() => setComparison(false)}><ArrowLeft size={15} aria-hidden="true" />{topicTitle}</Button>
        : <Button variant="secondary" onClick={() => setComparison(true)}>Compare editions<ArrowRight size={15} aria-hidden="true" /></Button>}
      <Button variant="secondary" onClick={close}>Back to workspace</Button>
      {comparison && <a className="edition-preview__pricing" href="https://www.inogen.ai/pricing" target="_blank" rel="noopener noreferrer" aria-label="Full comparison and pricing on inogen.ai (opens in a new tab)">Full comparison &amp; pricing<ArrowUpRight size={16} aria-hidden="true" /></a>}
    </footer>
  </dialog>;
}

function ProcessingConditions() {
  return <div className="edition-preview__note">
    <h3>How paid processing works</h3>
    <p>Automatic updates depend on your configured policies, confidence thresholds and safety checks. Some corrections need review, may be held or may not produce a rule; upgrading does not bypass those checks.</p>
    <p>Model-assisted features require a valid licence and a configured model provider. Any model usage charges depend on your provider account. The current provider integration uses OpenRouter.</p>
  </div>;
}
