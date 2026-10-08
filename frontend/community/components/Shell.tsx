"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { communityNavigation } from "@inogen/oms-client";
import { BrandMark, ProductMark, Notice } from "@inogen/oms-ui-core";
import { MobileNavigation } from "@inogen/oms-ui-core/mobile-navigation";
import logo from "@inogen/oms-ui-core/assets/inogen_logo_darkmode.png";
import productLogo from "@inogen/oms-ui-core/assets/oms_logo_darkthemev2.png";
import { BookOpen, FileUp, GitBranch, House, Inbox, ListChecks, LockKeyhole, Network, PanelLeftClose, PanelLeftOpen, Send, Settings } from "lucide-react";
import { useWorkspace } from "@/lib/workspace";
import { useEffect, useRef, useState, type ReactNode } from "react";
import EditionPreview from "./EditionPreview";

const icons = { "/": House, "/skills": BookOpen, "/sources": GitBranch, "/inbox": Inbox, "/rules": ListChecks, "/import": FileUp, "/publish": Send, "/graph": Network, "/settings": Settings };
const PAID_PREVIEW = "#people-and-teams-preview";
const paidLabel = "People and teams, Paid feature preview";

// The shared mobile navigation still receives ordinary destinations. This
// Community adapter renders its one informational item as a labelled button.
function CommunityNavigationLink({ href, children, ...props }: {
  href: string; children: ReactNode; className?: string; onClick?: () => void; "aria-current"?: "page";
}) {
  return href === PAID_PREVIEW
    ? <button {...props} type="button" aria-label={paidLabel} aria-haspopup="dialog" className={`${props.className ?? ""} community-paid-nav`}><LockKeyhole size={17} aria-hidden="true" /><span className="community-paid-nav__label">People and teams</span><span className="community-paid-nav__badge" aria-hidden="true">Paid</span></button>
    : <Link {...props} href={href}>{children}</Link>;
}

export default function Shell({ children }: { children: ReactNode }) {
  const { capabilities, sourceWorkspaceNotice } = useWorkspace();
  const [collapsed, setCollapsed] = useState(false);
  const [preview, setPreview] = useState(false);
  const paidControl = useRef<HTMLButtonElement>(null);
  const mobileNavigation = useRef<HTMLDivElement>(null);
  useEffect(() => { try { setCollapsed(localStorage.getItem("oms-community-sidebar") === "collapsed"); } catch { /* Storage may be disabled. */ } }, []);
  function toggleSidebar() {
    setCollapsed(!collapsed);
    try { localStorage.setItem("oms-community-sidebar", collapsed ? "expanded" : "collapsed"); } catch { /* Keep this session's preference. */ }
  }
  const pathname = usePathname();
  const navigation = communityNavigation(capabilities);
  if (capabilities.github_skill_sources) navigation.splice(2, 0, { href: "/sources", label: "Sources", key: "sources" });
  const workspaceNavigation = navigation.filter(item => !["graph", "settings"].includes(item.key));
  const manageNavigation = navigation.filter(item => ["graph", "settings"].includes(item.key));
  const current = navigation.find((item) => item.href.replace(/\/$/, "") === pathname.replace(/\/$/, ""));
  const navLink = (item: typeof navigation[number]) => {
    const Icon = icons[item.href.replace(/\/$/, "") as keyof typeof icons] || House;
    return <Link key={item.key} href={item.href} aria-label={item.label} title={collapsed ? item.label : undefined} aria-current={current?.key === item.key ? "page" : undefined}><Icon size={17} aria-hidden="true" /><span>{item.label}</span></Link>;
  };
  function closePreview() {
    setPreview(false);
    if (window.matchMedia("(min-width: 721px)").matches) paidControl.current?.focus();
    else mobileNavigation.current?.querySelector<HTMLButtonElement>(".oms-mobile-nav__trigger")?.focus();
  }
  return <div className={`workspace${collapsed ? " workspace--collapsed" : ""}`}>
    <a className="skip" href="#content">Skip to content</a>
    <aside className="sidebar">
      <button className="sidebar-toggle" onClick={toggleSidebar} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-expanded={!collapsed} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}>{collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}</button>
      <Link href="/" className="brand" aria-label="OMS Community home"><ProductMark logoSrc={productLogo.src} /><span className="edition">Community</span></Link>
      <div className="workspace-label"><span className="status-dot" />Your local workspace</div>
      <nav className="community-desktop-nav" aria-label="Main navigation">
        {workspaceNavigation.map(navLink)}
        <div className="community-nav-group">Manage</div>
        <button ref={paidControl} type="button" className="community-paid-nav" onClick={() => setPreview(true)} aria-haspopup="dialog" aria-label={paidLabel} title={collapsed ? paidLabel : undefined}><LockKeyhole size={17} aria-hidden="true" /><span className="community-paid-nav__label">People and teams</span><span className="community-paid-nav__badge" aria-hidden="true">Paid</span></button>
        {manageNavigation.map(navLink)}
      </nav>
      <div ref={mobileNavigation} className="community-mobile-nav-wrap"><MobileNavigation className="community-mobile-nav"
        items={[...workspaceNavigation.map(item => ({ id: item.key, label: item.label, href: item.href })),
          { id: "paid-people", label: "People and teams · Paid", href: PAID_PREVIEW },
          ...manageNavigation.map(item => ({ id: item.key, label: item.label, href: item.href }))]}
        onSelect={(id) => { if (id === "paid-people") setPreview(true); }}
        current={current?.key} linkComponent={CommunityNavigationLink} label="Pages"
        navigationLabel="Main navigation" wideQuery="(min-width: 721px)" /></div>
      <div className="sidebar-brand"><BrandMark logoSrc={logo.src} /></div>
    </aside>
    <div className="content-wrap"><header className="topbar"><span>Workspace <span className="breadcrumb-divider">/</span> <strong>{current?.label || "OMS"}</strong></span><span className="local-badge">Local · Community</span></header><main id="content">{sourceWorkspaceNotice && <Notice>{sourceWorkspaceNotice}</Notice>}{children}</main></div>
    {preview && <EditionPreview onClose={closePreview} />}
  </div>;
}

export function PageHeading({ title, description, children, headingRef }: { title: string; description?: string; children?: ReactNode; headingRef?: React.Ref<HTMLHeadingElement> }) {
  return <div className="page-heading"><div><h1 ref={headingRef} tabIndex={headingRef ? -1 : undefined}>{title}</h1>{description && <p className="subheading">{description}</p>}</div>{children}</div>;
}
