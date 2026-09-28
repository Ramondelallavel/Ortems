"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { Button, Dialog, Icon, Kbd, Loading, Select } from "@/components/ui";
import { useHotkeys } from "@/lib/hooks";
import { LOCALES, type Locale } from "@/lib/i18n";
import { useSession } from "@/lib/session";
import { AssistantDrawer } from "./Assistant";
import { Logo } from "./Logo";

type NavItem = { href: string; key: string; icon: string; perm?: string };
type NavGroup = { key: string; items: NavItem[] };

const NAV: NavGroup[] = [
  { key: "nav.overview", items: [{ href: "/dashboard", key: "nav.commandCenter", icon: "dashboard" }, { href: "/getting-started", key: "nav.gettingStarted", icon: "check" }] },
  {
    key: "nav.planning",
    items: [
      { href: "/planning", key: "nav.board", icon: "board", perm: "plan:read" },
      { href: "/planning/orders", key: "nav.orders", icon: "orders", perm: "orders:read" },
      { href: "/planning/capacity", key: "nav.capacity", icon: "capacity", perm: "plan:read" },
      { href: "/planning/materials", key: "nav.materials", icon: "materials", perm: "orders:read" },
      { href: "/planning/mps", key: "nav.mps", icon: "mps", perm: "orders:read" },
      { href: "/planning/scenarios", key: "nav.scenarios", icon: "scenarios", perm: "scenario:read" },
      { href: "/planning/alerts", key: "nav.alerts", icon: "alert", perm: "plan:read" },
    ],
  },
  {
    key: "nav.execution",
    items: [
      { href: "/shopfloor/dispatch", key: "nav.dispatch", icon: "dispatch", perm: "plan:read" },
      { href: "/shopfloor/supervisor", key: "nav.supervisor", icon: "eye", perm: "plan:read" },
      { href: "/shopfloor/operator", key: "nav.operator", icon: "wrench", perm: "plan:read" },
    ],
  },
  {
    key: "nav.analytics",
    items: [
      { href: "/analytics", key: "nav.kpis", icon: "analytics", perm: "analytics:read" },
      { href: "/analytics/plan-vs-actual", key: "nav.planVsActual", icon: "target", perm: "analytics:read" },
    ],
  },
  {
    key: "nav.masterData",
    items: [
      { href: "/master-data", key: "nav.masterData", icon: "database", perm: "masterdata:read" },
      { href: "/integrations", key: "nav.integrations", icon: "plug", perm: "integration:import" },
      { href: "/admin/data-quality", key: "nav.dataQuality", icon: "quality", perm: "masterdata:read" },
      { href: "/admin", key: "nav.admin", icon: "settings", perm: "admin:users" },
    ],
  },
];

export function AppShell({ children }: { children: ReactNode }) {
  const { me, loading, plant, setPlantId, t, can, locale, setLocale, logout } = useSession();
  const path = usePathname();
  const router = useRouter();
  const [collapsed, setCollapsed] = useState(false);
  const [assistant, setAssistant] = useState(false);
  const [help, setHelp] = useState(false);
  const [mobileNav, setMobileNav] = useState(false);

  useEffect(() => {
    if (!loading && !me) router.replace(`/login?next=${encodeURIComponent(path)}`);
  }, [loading, me, router, path]);
  useEffect(() => setMobileNav(false), [path]);

  useHotkeys({
    "?": () => setHelp(true),
    "shift+?": () => setHelp(true),
    "g": () => {},
    "alt+1": () => router.push("/dashboard"),
    "alt+2": () => router.push("/planning"),
    "alt+3": () => router.push("/planning/orders"),
    "alt+4": () => router.push("/planning/capacity"),
    "alt+5": () => router.push("/planning/materials"),
    "alt+6": () => router.push("/planning/scenarios"),
    "mod+j": () => setAssistant((a) => !a),
    escape: () => setHelp(false),
  });

  if (loading || !me) return <Loading />;

  const isActive = (href: string) => (href === "/planning" ? path === "/planning" || path.startsWith("/planning/gantt") : path === href || path.startsWith(href + "/"));

  const nav = (
    <nav aria-label="Main" className="flex-1 overflow-y-auto mx-scroll py-2">
      {NAV.map((g) => {
        const items = g.items.filter((i) => !i.perm || can(i.perm));
        if (!items.length) return null;
        return (
          <div key={g.key} className="mb-2">
            {!collapsed && <div className="px-3 pt-1 pb-1 text-[10.5px] uppercase tracking-wider text-white/45 font-semibold">{t(g.key)}</div>}
            {items.map((i) => (
              <Link
                key={i.href}
                href={i.href}
                aria-current={isActive(i.href) ? "page" : undefined}
                title={collapsed ? t(i.key) : undefined}
                className={`flex items-center gap-2.5 h-8 mx-1.5 px-2.5 rounded-[3px] text-[12.5px] ${isActive(i.href) ? "bg-navy-700 text-white" : "text-white/75 hover:bg-navy-900 hover:text-white"}`}
              >
                <Icon name={i.icon} size={16} />
                {!collapsed && <span className="truncate">{t(i.key)}</span>}
              </Link>
            ))}
          </div>
        );
      })}
    </nav>
  );

  return (
    <div className="h-screen flex flex-col overflow-hidden">
      <a href="#main" className="sr-only-focusable absolute z-[70] bg-white p-2">
        Skip to content
      </a>
      <header className="h-11 bg-navy-950 text-white flex items-center gap-3 px-3 shrink-0 border-b border-black/30">
        <button className="md:hidden mx-btn mx-btn-ghost mx-btn-sm text-white" aria-label="Menu" onClick={() => setMobileNav((m) => !m)}>
          <Icon name="menu" />
        </button>
        <Link href="/dashboard" className="flex items-center gap-2 font-semibold tracking-tight">
          <Logo /> <span className="hidden sm:inline">MonxuPlan</span>
        </Link>
        <div className="h-5 w-px bg-white/15 hidden sm:block" />
        <label className="sr-only" htmlFor="plant-select">
          {t("common.plant")}
        </label>
        <select id="plant-select" value={plant?.id || ""} onChange={(e) => setPlantId(e.target.value)} className="h-7 rounded-[3px] bg-navy-900 border border-white/15 text-white text-[12.5px] px-2 max-w-[220px]">
          {me.plants.map((p) => (
            <option key={p.id} value={p.id}>
              {p.code} · {p.name}
            </option>
          ))}
        </select>
        <div className="flex-1" />
        <button onClick={() => setAssistant((a) => !a)} className="h-7 px-2.5 rounded-[3px] border border-white/15 text-[12px] flex items-center gap-1.5 hover:bg-navy-900" aria-pressed={assistant} title="Ctrl/⌘ + J">
          <Icon name="chat" size={14} /> <span className="hidden sm:inline">{t("nav.assistant")}</span>
        </button>
        <button onClick={() => setHelp(true)} className="h-7 w-7 rounded-[3px] border border-white/15 flex items-center justify-center hover:bg-navy-900" aria-label={t("common.shortcuts")} title={t("common.shortcuts") + " (?)"}>
          <Icon name="keyboard" size={14} />
        </button>
        <Select
          ariaLabel={t("common.language")}
          value={locale}
          onChange={(v) => setLocale(v as Locale)}
          options={LOCALES.map((l) => ({ value: l.code, label: l.code.toUpperCase() }))}
          className="!h-7 !bg-navy-900 !text-white !border-white/15 !w-[58px]"
        />
        <div className="hidden sm:flex items-center gap-2 text-[12px] text-white/80">
          <Icon name="user" size={14} />
          <span className="max-w-[140px] truncate" title={me.roles.join(", ")}>
            {me.full_name}
          </span>
        </div>
        <button onClick={logout} className="h-7 w-7 rounded-[3px] flex items-center justify-center hover:bg-navy-900" aria-label={t("common.signOut")} title={t("common.signOut")}>
          <Icon name="logout" size={15} />
        </button>
      </header>
      <div className="flex flex-1 min-h-0">
        <aside className={`${mobileNav ? "flex fixed inset-y-11 left-0 z-40 w-56" : "hidden"} md:flex md:static flex-col bg-navy-950 text-white shrink-0 ${collapsed ? "md:w-[52px]" : "md:w-[208px]"}`}>
          {nav}
          <button className="h-8 text-white/50 hover:text-white text-[11px] border-t border-white/10 hidden md:flex items-center justify-center gap-1" onClick={() => setCollapsed((c) => !c)} aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}>
            <Icon name={collapsed ? "chevronRight" : "chevronLeft"} size={14} />
          </button>
        </aside>
        <main id="main" className="flex-1 min-w-0 min-h-0 flex flex-col overflow-hidden">
          {children}
        </main>
      </div>
      <AssistantDrawer open={assistant} onClose={() => setAssistant(false)} />
      <Dialog open={help} title={t("common.shortcuts")} onClose={() => setHelp(false)} footer={<Button onClick={() => setHelp(false)}>{t("common.close")}</Button>}>
        <table className="mx-table">
          <tbody>
            {[
              [["Alt", "1…6"], "Command Center, Planning Board, Orders, Capacity, Materials, Scenarios"],
              [["Ctrl/⌘", "J"], "Planning assistant"],
              [["Ctrl/⌘", "Z"], "Undo last plan change (Planning Board)"],
              [["Ctrl/⌘", "Y"], "Redo (Planning Board)"],
              [["Ctrl/⌘", "F"], "Find order or operation (Planning Board)"],
              [["+", "−"], "Zoom Gantt in / out"],
              [["←", "→"], "Scroll Gantt in time"],
              [["Enter"], "Open selected row / operation"],
              [["Esc"], "Close panel or dialog, cancel drag"],
              [["?"], "This help"],
            ].map(([keys, desc], i) => (
              <tr key={i}>
                <td className="w-[170px]">
                  {(keys as string[]).map((k, j) => (
                    <span key={j}>
                      {j > 0 && " + "}
                      <Kbd>{k}</Kbd>
                    </span>
                  ))}
                </td>
                <td>{desc as string}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Dialog>
    </div>
  );
}
