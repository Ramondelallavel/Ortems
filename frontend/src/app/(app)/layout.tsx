"use client";
import { usePathname } from "next/navigation";
import { Suspense, type ReactNode } from "react";
import { AppShell } from "@/components/shell/AppShell";
import { RouteBoundary } from "@/components/shell/RouteBoundary";

export default function AppLayout({ children }: { children: ReactNode }) {
  const path = usePathname();
  // pages read the query string (deep links such as /planning/orders?q=WO-1): the boundary keeps
  // the static shell prerendered while the page itself renders on the client
  return (
    <AppShell>
      <RouteBoundary resetKey={path}>
        <Suspense fallback={null}>{children}</Suspense>
      </RouteBoundary>
    </AppShell>
  );
}
