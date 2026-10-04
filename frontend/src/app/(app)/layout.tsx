"use client";
import { Suspense, type ReactNode } from "react";
import { AppShell } from "@/components/shell/AppShell";

export default function AppLayout({ children }: { children: ReactNode }) {
  // pages read the query string (deep links such as /planning/orders?q=WO-1): the boundary keeps
  // the static shell prerendered while the page itself renders on the client
  return (
    <AppShell>
      <Suspense fallback={null}>{children}</Suspense>
    </AppShell>
  );
}
