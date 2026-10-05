"use client";
import Link from "next/link";
import { Badge, Loading, PageHeader, Panel } from "@/components/ui";
import { useApi } from "@/lib/hooks";
import { useScenarioSelection } from "@/lib/plan";
import { useSession } from "@/lib/session";

type Step = { key: string; title: string; done: boolean | null; detail: string; href: string; action: string };

/** Setup and demo guide. Every step is evaluated from the tenant's real data — nothing is simulated. */
export default function GettingStarted() {
  const { t, plant, can } = useSession();
  const { list } = useScenarioSelection();
  const q = { limit: 1, plant_id: plant?.id };
  const cals = useApi<any>(plant ? "/master-data/calendars" : null, { limit: 1 });
  const machines = useApi<any>(plant ? "/master-data/machines" : null, q);
  const items = useApi<any>(plant ? "/master-data/products" : null, { limit: 1 });
  const routings = useApi<any>(plant ? "/master-data/routings" : null, { limit: 1 });
  const orders = useApi<any>(plant ? "/orders" : null, plant ? { plant_id: plant.id, limit: 1 } : undefined);
  const dq = useApi<any>(plant ? "/data-quality" : null, plant ? { plant_id: plant.id } : undefined);
  if (!plant) return <Loading />;
  const live = list.find((s) => s.is_live);
  const whatIfs = list.filter((s) => !s.is_live && s.head_plan_id);
  const n = (x: any) => (x.data ? x.data.total : null);
  const steps: Step[] = [
    { key: "cal", title: t("Calendars and shifts"), done: n(cals) === null ? null : n(cals) > 0, detail: t("{n} calendars", { n: n(cals) ?? "…" }), href: "/master-data/calendars", action: t("Define shifts, breaks and holidays (or import them)") },
    { key: "res", title: t("Machines and resources"), done: n(machines) === null ? null : n(machines) > 0, detail: t("{n} machines in {plant}", { n: n(machines) ?? "…", plant: plant.code }), href: "/master-data/machines", action: t("Create or import machines, lines, tools and labour pools") },
    { key: "items", title: t("Products, BOMs and routings"), done: n(items) === null ? null : n(items) > 0 && n(routings) > 0, detail: t("{n} products · {m} routings", { n: n(items) ?? "…", m: n(routings) ?? "…" }), href: "/integrations", action: t("Import items, BOMs and routings with the wizard") },
    { key: "orders", title: t("Orders, stock and receipts"), done: n(orders) === null ? null : n(orders) > 0, detail: t("{n} open production orders", { n: n(orders) ?? "…" }), href: "/integrations", action: t("Import production orders, inventory and purchase orders") },
    { key: "dq", title: t("Data quality"), done: dq.data ? dq.data.summary.errors === 0 : null, detail: dq.data ? t("{n} blocking errors · {m} warnings", { n: dq.data.summary.errors, m: dq.data.summary.warnings }) : "…", href: "/admin/data-quality", action: t("Fix blocking errors before planning") },
    { key: "plan", title: t("First optimised plan"), done: live ? !!live.head_plan_id : null, detail: live?.head_plan ? `${live.head_plan.number} · OTIF ${live.head_plan.kpis?.otif ?? "—"} %` : t("no plan yet"), href: "/planning", action: t("Open the Planning Board and press Optimize") },
    { key: "why", title: t("Understand the plan"), done: null, detail: t("Select any bar → Why here? / Constraint explorer / Order chain"), href: "/planning", action: t("Check why a late order is late and what the bottleneck is") },
    { key: "whatif", title: t("Evaluate a what-if"), done: whatIfs.length > 0, detail: t("{n} planned scenarios besides the live plan", { n: whatIfs.length }), href: "/planning/scenarios", action: t("Try a night shift, a rush order or a supplier delay and compare") },
    { key: "publish", title: t("Publish to the shop floor"), done: plant.published_plan_id ? true : live ? false : null, detail: plant.published_plan_id ? t("a plan is published") : t("nothing published yet"), href: "/planning", action: t("Publish, then open the Dispatch list and Operator screen") },
  ];
  const done = steps.filter((s) => s.done).length;
  return (
    <div className="flex flex-col h-full min-h-0">
      <PageHeader title={t("Getting started")} subtitle={`${plant.name} · ${t("{n} of {m} steps done — each step is checked against your real data", { n: done, m: steps.length })}`} />
      <div className="flex-1 overflow-auto mx-scroll p-4 max-w-4xl space-y-3">
        <Panel title={t("From empty plant to published plan")}>
          <ol>
            {steps.map((s, i) => (
              <li key={s.key} className="flex items-start gap-3 px-3 py-2.5 border-b border-gray-100">
                <span className={`w-6 h-6 rounded-full flex items-center justify-center text-[12px] font-semibold shrink-0 ${s.done ? "bg-green-600 text-white" : "bg-gray-100 text-slate-600 border border-gray-300"}`} aria-hidden="true">
                  {s.done ? "✓" : i + 1}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="font-semibold flex items-center gap-2">
                    {s.title} {s.done === true ? <Badge tone="ok">{t("done")}</Badge> : s.done === false ? <Badge tone="warn">{t("to do")}</Badge> : null}
                  </div>
                  <div className="text-[12px] text-slate-600">{s.detail}</div>
                  <div className="text-[12.5px]">{s.action}</div>
                </div>
                <Link className="mx-btn mx-btn-sm shrink-0" href={s.href}>
                  {t("Open")}
                </Link>
              </li>
            ))}
          </ol>
        </Panel>
        <Panel title={t("Demo tour (Monxu Manufacturing, Sevilla)")}>
          <div className="p-3 text-[12.5px] space-y-1.5">
            <p>{t("The demo plant contains deliberate situations to explore:")}</p>
            <ul className="list-disc pl-5 space-y-1">
              <li>
                <b>BRG-6205</b> {t("bearing shortage — orders that cannot be scheduled and why")} (<Link className="text-blue-500 hover:underline" href="/planning/materials">{t("nav.materials")}</Link>).
              </li>
              <li>
                <b>CNC-04</b> {t("maintenance today 14:00–18:00 and")} <b>CNC-03</b> {t("ballscrew replacement next week (hatched on the")} <Link className="text-blue-500 hover:underline" href="/planning">{t("nav.planning")}</Link>).
              </li>
              <li>
                <b>PAINT-01</b> {t("colour changeovers light → dark (setup matrix) and detached setups while material arrives.")}
              </li>
              <li>
                {t("Two prepared what-ifs: a CNC night shift and buying a fifth machining centre")} (<Link className="text-blue-500 hover:underline" href="/planning/scenarios">{t("nav.scenarios")}</Link>) — {t("plan them and compare with the live plan.")}
              </li>
              <li>
                {t("Ask the assistant")} (<kbd className="mx-kbd">Ctrl</kbd>+<kbd className="mx-kbd">J</kbd>): <i>{t("why is WO-10042 late?")}</i>
              </li>
            </ul>
            {can("admin:config") && <p className="text-slate-600">{t("Administrators can reset the demo data from the API (POST /api/v1/demo/reset, disabled in production).")}</p>}
          </div>
        </Panel>
      </div>
    </div>
  );
}
