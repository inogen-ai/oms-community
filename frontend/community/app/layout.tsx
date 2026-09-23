import type { Metadata } from "next";
import type { ReactNode } from "react";
import "@fontsource-variable/inter/index.css";
import "@fontsource/open-sans/400.css";
import "@fontsource/open-sans/600.css";
import "@fontsource/open-sans/700.css";
import "@inogen/oms-ui-core/theme.css";
import "@inogen/oms-ui-core/styles.css";
import "@inogen/oms-ui-core/mobile-navigation.css";
import "./globals.css";
import Shell from "@/components/Shell";
import { WorkspaceProvider } from "@/lib/workspace";

export const metadata: Metadata = { title: "OMS Community", description: "Import, improve and publish your agent skills locally." };
export default function RootLayout({ children }: { children: ReactNode }) {
  return <html lang="en"><body><WorkspaceProvider><Shell>{children}</Shell></WorkspaceProvider></body></html>;
}
